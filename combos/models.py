from decimal import Decimal
import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from core.models import SoftDeleteModel, ProductVariant


class Combo(SoftDeleteModel):
    """A curated bundle of multiple product variants sold together.

    Components may span MULTIPLE sellers (e.g. glass from one seller, plants
    from another, lighting + fertilizers from a third). At checkout a combo
    explodes into its component variants, which flow into the existing
    per-seller SubOrder buckets — so each seller still ships their own part.

    Pricing (v1 — no discounts):
      - ``price`` is an optional admin override. When left blank the combo is
        priced at the live sum of its component variant prices.
      - ``shipping_fee`` is a single fixed charge for the whole combo, set by
        admin. Unlike normal orders, combo shipping is NOT split per seller.
    """
    class ComboType(models.TextChoices):
        COMPLETE_BUILD = 'complete_build', 'Complete build'
        PLANTS_ONLY = 'plants_only', 'Plants only'
        HARDSCAPE = 'hardscape', 'Hardscape'
        FERTILIZERS = 'fertilizers', 'Fertilizers'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=140)
    tagline = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True, default='')
    combo_type = models.CharField(
        max_length=20, choices=ComboType.choices, default=ComboType.OTHER,
        db_index=True, help_text="Drives the storefront type-filter chips.",
    )
    is_featured = models.BooleanField(
        default=False, db_index=True,
        help_text="Show in the home page 'Featured Combos' rail.",
    )

    image_url = models.URLField(max_length=1000, blank=True, null=True,
        help_text="Hero image for the combo card and detail page.")

    price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Optional fixed combo price (₹). Leave blank to price at the "
                  "live sum of all component variant prices.",
    )
    shipping_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Fixed shipping charge for the whole combo (₹). Charged once "
                  "per combo, not split per seller.",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    is_draft = models.BooleanField(
        default=False, db_index=True,
        help_text="True = hidden from buyers (work in progress).",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_combos',
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    _SLUG_MAX = 140

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:self._SLUG_MAX] or 'combo'
            qs = Combo.all_objects.exclude(pk=self.pk)
            candidate = base
            counter = 1
            while qs.filter(slug=candidate).exists():
                suffix = f"-{counter}"
                candidate = base[:self._SLUG_MAX - len(suffix)] + suffix
                counter += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    # ── Computed pricing / availability (read from prefetched items) ──────────

    @property
    def original_price(self):
        """Live sum of component variant prices × quantity."""
        return sum(
            (item.variant.price * item.quantity for item in self.items.all()
             if item.variant_id is not None),
            Decimal('0'),
        )

    @property
    def effective_price(self):
        """What the buyer pays for the items (excl. shipping)."""
        return self.price if self.price is not None else self.original_price

    @property
    def total_price(self):
        """Items + fixed combo shipping — the full buyer-facing price."""
        return self.effective_price + (self.shipping_fee or Decimal('0'))

    @property
    def in_stock(self):
        """True only when EVERY component is in stock (i.e. availability == full)."""
        items = list(self.items.all())
        if not items:
            return False
        return all(
            item.variant_id is not None and item.variant.stock >= item.quantity
            for item in items
        )

    @property
    def availability(self):
        """'full' (all in stock) · 'partial' (some, still buyable) · 'oos' (none).

        Buyability rule: a combo is buyable while at least one component is in
        stock; only when nothing is available does it show as sold out.
        """
        items = list(self.items.all())
        if not items:
            return 'oos'
        available = [
            it for it in items
            if it.variant_id is not None and it.variant.stock >= it.quantity
        ]
        if not available:
            return 'oos'
        return 'full' if len(available) == len(items) else 'partial'

    @property
    def max_purchasable(self):
        """How many whole combos can currently be bought given component stock."""
        counts = [
            item.variant.stock // item.quantity
            for item in self.items.all()
            if item.variant_id is not None and item.quantity > 0
        ]
        return min(counts) if counts else 0

    @property
    def seller_ids(self):
        return {
            item.variant.product.seller_id
            for item in self.items.all()
            if item.variant_id is not None
        }

    @property
    def seller_count(self):
        return len(self.seller_ids)


class ComboItem(models.Model):
    """One component of a combo — a specific product variant + quantity.

    Keyed on ``variant`` (not Product) because price, stock, weight, and seller
    all resolve through the variant, mirroring CartItem / OrderItem.
    """
    combo = models.ForeignKey(Combo, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='combo_items')
    quantity = models.PositiveIntegerField(default=1)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order']
        unique_together = ('combo', 'variant')

    @property
    def line_total(self):
        return self.variant.price * self.quantity

    def __str__(self):
        return f"{self.combo.name} · {self.variant} ×{self.quantity}"


class ComboImage(models.Model):
    """Optional gallery image for a combo (the hero lives on Combo.image_url)."""
    combo = models.ForeignKey(Combo, on_delete=models.CASCADE, related_name='images')
    image_url = models.URLField(max_length=1000)
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Image for {self.combo.name}"
