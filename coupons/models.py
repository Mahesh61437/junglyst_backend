import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import User, Category, SoftDeleteModel


class DiscountType(models.TextChoices):
    PERCENT = 'percent', _('Percentage off')
    FIXED = 'fixed', _('Fixed amount off')


class FundedBy(models.TextChoices):
    PLATFORM = 'platform', _('Platform')   # seller payout untouched
    SELLER = 'seller', _('Seller')         # discount deducted from seller payout


class Coupon(SoftDeleteModel):
    """
    A discount code with eligibility rules.

    Eligibility (all must hold):
      • code is active and inside its validity window
      • whole-cart subtotal >= min_cart_value
      • at least ONE cart line matches the scope: sold by `seller` (if set)
        AND in one of `categories` (if any set)
      • usage limits (total / per user) not exhausted

    The discount is computed over the *matching* lines only, so a seller-funded
    coupon never eats into another seller's payout. Empty seller + empty
    categories = every line in the cart matches (a plain cart-value coupon).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True,
                            help_text="Buyers type this at checkout. Stored upper-case.")
    name = models.CharField(max_length=120, help_text="Short label shown to the buyer, e.g. '10% off aquatic plants'")
    description = models.TextField(blank=True)

    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENT)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2,
                                         help_text="Percent (e.g. 10 = 10%) or fixed ₹ amount, per discount_type")
    max_discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                              help_text="Cap on the ₹ discount. Leave blank for no cap.")
    funded_by = models.CharField(max_length=10, choices=FundedBy.choices, default=FundedBy.PLATFORM,
                                 help_text="Who absorbs the discount. 'Seller' reduces the seller's payout.")

    # ── Scope ────────────────────────────────────────────────────────────────
    seller = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='coupons', limit_choices_to={'is_verified_seller': True},
                               help_text="Only this seller's products qualify. Blank = any seller.")
    categories = models.ManyToManyField(Category, blank=True, related_name='coupons',
                                        help_text="Only products in these categories qualify. Blank = any category.")
    min_cart_value = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                         help_text="Minimum whole-cart subtotal (₹) before the coupon applies.")

    # ── Validity / limits ────────────────────────────────────────────────────
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True, help_text="Blank = never expires")
    is_active = models.BooleanField(default=True)
    usage_limit_total = models.PositiveIntegerField(null=True, blank=True, help_text="Blank = unlimited")
    usage_limit_per_user = models.PositiveIntegerField(null=True, blank=True, help_text="Blank = unlimited")

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='created_coupons')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        super().clean()
        self.code = (self.code or '').strip().upper()
        if self.discount_type == DiscountType.PERCENT and not (0 < self.discount_value <= 100):
            raise ValidationError({'discount_value': 'Percentage must be between 0 and 100.'})
        if self.discount_type == DiscountType.FIXED and self.discount_value <= 0:
            raise ValidationError({'discount_value': 'Fixed discount must be greater than 0.'})
        if self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError({'valid_until': 'Must be after valid_from.'})
        if self.funded_by == FundedBy.SELLER and self.seller_id is None:
            raise ValidationError({'funded_by': 'A seller-funded coupon must be scoped to a seller.'})

    def save(self, *args, **kwargs):
        self.code = (self.code or '').strip().upper()
        super().save(*args, **kwargs)

    # ── Convenience ──────────────────────────────────────────────────────────
    @property
    def is_live(self):
        now = timezone.now()
        return (self.is_active and self.valid_from <= now
                and (self.valid_until is None or now < self.valid_until))

    def redemptions_counted(self):
        """Redemptions that count against usage limits (paid, not cancelled)."""
        return self.redemptions.exclude(order__status__in=('cancelled', 'failed'))

    def describe_discount(self):
        if self.discount_type == DiscountType.PERCENT:
            s = f"{self.discount_value.normalize():f}% off"
            if self.max_discount_amount:
                s += f" (up to ₹{self.max_discount_amount})"
            return s
        return f"₹{self.discount_value} off"


class CouponRedemption(models.Model):
    """One row per paid order that used a coupon. Created by the Order signal
    when payment lands; removed again if the order is cancelled/failed."""
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='redemptions')
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE, related_name='coupon_redemption')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='coupon_redemptions')
    guest_email = models.EmailField(null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.coupon.code} on {self.order_id}"
