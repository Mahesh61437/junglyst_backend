from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.utils.text import slugify
import uuid

class UserRole(models.TextChoices):
    COLLECTOR = 'collector', _('Collector')
    GROWER = 'grower', _('Grower')
    ADMIN = 'admin', _('Admin')

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def delete(self, **kwargs):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save()

    class Meta:
        abstract = True

class User(AbstractUser, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_('email address'), unique=True)
    phone = models.CharField(_('phone number'), max_length=15, unique=True, null=True, blank=True)
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.COLLECTOR)
    
    is_guest = models.BooleanField(default=False)
    is_verified_seller = models.BooleanField(default=False)
    
    avatar_url = models.URLField(max_length=1000, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True, default='Kerala, India')

    # Per-seller commission config — admin-only. Sellers and buyers never see these.
    # Semantics depend on price_is_buyer_final (see ProductVariant.save for the math).
    seller_commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=10.00,
        help_text="Markup % added on top of seller's listed price (toggle OFF) "
                  "or part of total deduction from listed price (toggle ON).",
    )
    buyer_commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=10.00,
        help_text="Deduction % from seller payout (toggle OFF) "
                  "or part of total deduction from listed price (toggle ON).",
    )
    price_is_buyer_final = models.BooleanField(
        default=False,
        help_text="OFF: listed price is the seller's reference; buyer pays L*(1+seller_rate). "
                  "ON: listed price IS what buyer pays; seller payout = L*(1-(seller_rate+buyer_rate)).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        if self.price_is_buyer_final and (self.seller_commission_rate + self.buyer_commission_rate) >= 100:
            raise ValidationError(
                "When price_is_buyer_final is ON, seller_commission_rate + buyer_commission_rate "
                "must be less than 100% — otherwise seller payout goes to zero or negative."
            )

class ShippingType(models.TextChoices):
    PLANT = 'plant', _('Plant / Live Specimen')
    ACCESSORY = 'accessory', _('Accessory / Tool')
    HEAVY = 'heavy', _('Heavy Item (>3kg)')
    FLAT = 'flat', _('Flat Rate Item')

class Category(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True)
    shipping_type = models.CharField(max_length=20, choices=ShippingType.choices, default=ShippingType.PLANT)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=20.00)
    
    meta_title = models.CharField(max_length=200, blank=True, null=True)
    meta_description = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['name']

    def __str__(self):
        return self.name

class SubCategory(SoftDeleteModel):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='subcategories')
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True)

    # Override parent category values; null = inherit from parent Category
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Leave blank to inherit from parent category")
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Leave blank to inherit from parent category")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Subcategories"
        unique_together = ('category', 'name')
        ordering = ['name']

    def __str__(self):
        return f"{self.category.name} > {self.name}"

    @property
    def effective_gst(self):
        return self.gst_percentage if self.gst_percentage is not None else self.category.gst_percentage

    @property
    def effective_commission(self):
        return self.commission_rate if self.commission_rate is not None else self.category.commission_rate


class CategoryShippingRate(models.Model):
    """Weight-based shipping fee tiers per category or subcategory.

    Resolution order: SubCategory rate → Category rate → platform default (light/heavy).
    A rate row applies when: min_weight_grams <= chargeable_weight < max_weight_grams (null = no upper bound).
    """
    category = models.ForeignKey(Category, on_delete=models.CASCADE,
        related_name='shipping_rates', null=True, blank=True)
    sub_category = models.ForeignKey(SubCategory, on_delete=models.CASCADE,
        related_name='shipping_rates', null=True, blank=True)

    min_weight_grams = models.PositiveIntegerField(default=0,
        help_text="Lower bound (inclusive) in grams")
    max_weight_grams = models.PositiveIntegerField(null=True, blank=True,
        help_text="Upper bound (exclusive) in grams; leave blank for 'above max'")
    rate = models.DecimalField(max_digits=8, decimal_places=2,
        help_text="Shipping fee in INR for this weight tier")
    free_above_order_value = models.DecimalField(max_digits=10, decimal_places=2,
        null=True, blank=True,
        help_text="Order subtotal (INR) above which shipping is free; leave blank to disable")

    class Meta:
        ordering = ['min_weight_grams']
        verbose_name = "Category Shipping Rate"
        verbose_name_plural = "Category Shipping Rates"

    def __str__(self):
        scope = self.sub_category or self.category
        upper = f"–{self.max_weight_grams}g" if self.max_weight_grams else "g+"
        return f"{scope} | {self.min_weight_grams}{upper} → ₹{self.rate}"

class Tag(SoftDeleteModel):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class CategoryComplement(models.Model):
    """
    Admin-defined rule: products in source_category should be complemented by
    products from target_categories (e.g. Aquatic Plants → Fertilizers, CO2, Lighting).
    """
    source_category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name='complement_rules'
    )
    target_categories = models.ManyToManyField(
        Category, related_name='complemented_by',
        help_text="Products from these categories appear as complementary recommendations."
    )
    priority = models.PositiveSmallIntegerField(
        default=0,
        help_text="Lower number = shown first when multiple rules match."
    )

    class Meta:
        ordering = ['priority']
        verbose_name = "Category Complement Rule"
        verbose_name_plural = "Category Complement Rules"

    def __str__(self):
        return f"{self.source_category.name} (priority {self.priority})"


class Product(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=120)
    tagline = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField()

    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='products')
    categories = models.ManyToManyField(Category, related_name='products', blank=True)
    sub_categories = models.ManyToManyField(SubCategory, related_name='products', blank=True)
    tags = models.ManyToManyField(Tag, related_name='products', blank=True)
    
    scientific_name = models.CharField(max_length=255, blank=True, null=True)
    # Range-aware fields: store single value "Easy" or range "Easy to Medium"
    care_level = models.CharField(max_length=50, default='Easy',
        help_text="Single value (Easy) or range (Easy to Advanced)")
    light_requirements = models.CharField(max_length=50, default='Medium',
        help_text="Single value (Low) or range (Low to High)")
    growth_rate = models.CharField(max_length=50, default='Moderate',
        help_text="Single value (Slow) or range (Slow to Fast)")
    
    is_rare = models.BooleanField(default=False, db_index=True)
    origin = models.CharField(max_length=100, blank=True, null=True)
    
    # Aquatic specific
    water_temperature = models.CharField(max_length=50, blank=True, null=True)
    ph_range = models.CharField(max_length=50, blank=True, null=True)
    
    is_active = models.BooleanField(default=True, db_index=True)
    is_draft = models.BooleanField(default=False, db_index=True,
        help_text="True = saved as draft (not visible to buyers). False = published or archived.")
    co2_requirement = models.CharField(max_length=50, choices=[('Low', 'Low'), ('Medium', 'Medium'), ('High', 'High')], default='Low')
    
    view_count = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=5.0, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    _SLUG_MAX = 120

    @staticmethod
    def _truncate_slug(s: str, max_len: int) -> str:
        """Trim to max_len at a word boundary (last '-' before limit)."""
        if len(s) <= max_len:
            return s
        cut = s[:max_len]
        boundary = cut.rfind('-')
        return cut[:boundary] if boundary > 0 else cut

    def save(self, *args, **kwargs):
        if not self.slug:
            base = self._truncate_slug(slugify(self.name), self._SLUG_MAX)
            qs = Product.all_objects.exclude(pk=self.pk)

            slug = base
            if qs.filter(slug=slug).exists():
                try:
                    store_slug = self.seller.seller_profile.slug
                    slug = self._truncate_slug(f"{base}-{store_slug}", self._SLUG_MAX)
                except Exception:
                    pass

            counter = 1
            candidate = slug
            while qs.filter(slug=candidate).exists():
                suffix = f"-{counter}"
                candidate = self._truncate_slug(slug, self._SLUG_MAX - len(suffix)) + suffix
                counter += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class VariantType(models.TextChoices):
    PLANT      = 'Plant',          _('Plant')
    RHIZOME    = 'Rhizome',        _('Rhizome')
    POT        = 'Pot',            _('Pot')
    CLUMP      = 'Clump',          _('Clump')
    TISSUE_CULTURE = 'Tissue Culture', _('Tissue Culture')
    CUTTING    = 'Cutting',        _('Cutting')
    BUNCH      = 'Bunch',          _('Bunch')
    MAT        = 'Mat',            _('Mat')
    CUP        = 'Cup',            _('Cup')
    EMERSED    = 'Emersed',        _('Emersed')
    SUBMERGED  = 'Submerged',      _('Submerged')
    SEEDLING   = 'Seedling',       _('Seedling')
    BULB       = 'Bulb',           _('Bulb')
    CORM       = 'Corm',           _('Corm')
    DRY_START  = 'Dry Start',      _('Dry Start')
    COLONY     = 'Colony',         _('Colony')
    PAIR       = 'Pair',           _('Pair')
    TRIO       = 'Trio',           _('Trio')
    OTHER      = 'Other',          _('Other')

class ProductVariant(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=100, default='Standard')
    variant_type = models.CharField(
        max_length=50,
        choices=VariantType.choices,
        default=VariantType.PLANT,
        help_text="What form is this variant? e.g. Rhizome, Clump, Tissue Culture"
    )
    
    sku = models.CharField(max_length=100, unique=True, null=True, blank=True)
    
    # Financial Breakdown
    base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Seller's payout expectation")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="GST percentage (e.g. 18.00)")
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=10.00, help_text="Platform commission percentage")
    
    # Final Buyer Price (calculated)
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Final price shown to buyers")
    
    compare_at_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    weight = models.DecimalField(max_digits=10, decimal_places=3, help_text="Weight in Kilograms", default=0.5)
    length = models.DecimalField(max_digits=10, decimal_places=2, help_text="Box length in cm", default=10.0)
    width = models.DecimalField(max_digits=10, decimal_places=2, help_text="Box breadth in cm", default=10.0)
    height = models.DecimalField(max_digits=10, decimal_places=2, help_text="Box height in cm", default=10.0)

    # Shipping classification fields (SHIP-001)
    class ItemCategory(models.TextChoices):
        LIGHT = 'light', 'Light Item'
        HEAVY = 'heavy', 'Heavy Item'
        HYBRID = 'hybrid', 'Hybrid (Light + Heavy)'

    item_category = models.CharField(
        max_length=10,
        choices=ItemCategory.choices,
        default=ItemCategory.LIGHT,
        help_text="Light: plants/moss/isopods. Heavy: rocks/substrate/hardscape. Hybrid: mixed items.",
    )
    packed_weight_grams = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Actual weight when packed for shipping (grams, 1–30000)",
    )

    @property
    def chargeable_weight(self):
        """Returns chargeable weight in grams: max(packed, volumetric)."""
        if not self.packed_weight_grams:
            return None
        from decimal import Decimal
        vol = (Decimal(str(self.length)) * Decimal(str(self.width)) * Decimal(str(self.height)) / Decimal('5000')) * Decimal('1000')
        return max(self.packed_weight_grams, int(vol))

    stock = models.IntegerField(default=0)
    
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def _resolve_commission(self):
        """Return (seller_rate, buyer_rate, price_is_buyer_final) for this variant.

        Resolution chain (most specific wins):
          1. Per-seller override (User.seller_commission_rate / buyer_commission_rate / price_is_buyer_final)
          2. Category default (Category.commission_rate, used as seller_rate; buyer_rate defaults to 10)
          3. Platform default (10 / 10 / False)
        """
        from decimal import Decimal
        seller = getattr(self.product, 'seller', None)
        if seller is not None:
            return (
                Decimal(str(seller.seller_commission_rate)),
                Decimal(str(seller.buyer_commission_rate)),
                bool(seller.price_is_buyer_final),
            )
        category = self.product.categories.first()
        if category is not None:
            return (
                Decimal(str(category.commission_rate)),
                Decimal('10'),
                False,
            )
        return (Decimal('10'), Decimal('10'), False)

    def save(self, *args, **kwargs):
        # Compute buyer-facing price from the seller-configured commission model.
        # See User.seller_commission_rate / buyer_commission_rate / price_is_buyer_final.
        #
        # Toggle OFF (default): L is the seller's reference price.
        #   buyer pays = L * (1 + seller_rate/100)
        #   seller payout (informational) = L * (1 - buyer_rate/100)
        #
        # Toggle ON: L IS what the buyer pays (admin-set per seller).
        #   buyer pays = L
        #   seller payout = L * (1 - (seller_rate + buyer_rate)/100)
        #
        # GST is no longer added on top — it's absorbed into the same listed price.
        from decimal import Decimal
        L = Decimal(str(self.base_price))
        s, b, buyer_final = self._resolve_commission()

        if buyer_final:
            self.price = L
        else:
            self.price = L * (Decimal('1') + s / Decimal('100'))

        # commission_rate column retained for backward compatibility; store the
        # effective seller-side rate so legacy readers don't break.
        self.commission_rate = s
        super().save(*args, **kwargs)

    @property
    def seller_payout(self):
        """What the seller receives for one unit sale (Decimal). Computed, not stored."""
        from decimal import Decimal
        L = Decimal(str(self.base_price))
        s, b, buyer_final = self._resolve_commission()
        if buyer_final:
            return L * (Decimal('1') - (s + b) / Decimal('100'))
        return L * (Decimal('1') - b / Decimal('100'))

    def __str__(self):
        return f"{self.product.name} - {self.name}"

class ProductImage(SoftDeleteModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True, blank=True, related_name='images')
    image_url = models.URLField(max_length=1000)
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Image for {self.product.name}"

class ProductReview(SoftDeleteModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='reviews')
    author = models.CharField(max_length=120)
    comment = models.TextField(blank=True, null=True)
    plants = models.PositiveSmallIntegerField(default=0)
    packaging = models.PositiveSmallIntegerField(default=0)
    responsiveness = models.PositiveSmallIntegerField(default=0)
    image = models.ImageField(upload_to='reviews/', blank=True, null=True, help_text="Upload an image of the plant/product")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Review for {self.product.name} by {self.author}"

class WishlistItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wishlist_items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='wishlisted_by')
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'product')
        ordering = ['-added_at']

    def __str__(self):
        return f"{self.user.email} → {self.product.name}"

class Configuration(models.Model):
    """
    Flexible configuration model for storing system-wide settings.
    
    Examples:
    - {"commission_rates": {"seller": 10, "platform": 5}}
    - {"feature_flags": {"new_checkout": true, "payment_gateway": "razorpay"}}
    - {"email_settings": {"sender": "noreply@junglyst.com", "smtp_host": "..."}}
    """
    name = models.CharField(max_length=255, unique=True, 
                           help_text="Unique identifier for this configuration")
    data = models.JSONField(default=dict, blank=True,
                           help_text="JSON configuration data")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuration"
        verbose_name_plural = "Configurations"
        ordering = ['name']

    def __str__(self):
        return self.name

class BugReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='bug_reports')
    contact_info = models.CharField(max_length=255, null=True, blank=True, help_text="Email or Phone for guest users")
    description = models.TextField()
    images = models.JSONField(default=list, blank=True, help_text="List of Firebase image URLs")
    status = models.CharField(max_length=20, choices=[('unresolved', 'Unresolved'), ('resolved', 'Resolved')], default='unresolved')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status_label = dict(self._meta.get_field('status').choices).get(self.status, self.status)
        return f"BugReport {self.id} - {status_label}"
