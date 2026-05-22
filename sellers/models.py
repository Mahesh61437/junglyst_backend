from datetime import date, timedelta
from django.db import models
from core.models import User
from django.utils.text import slugify

WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

class SellerProfileManager(models.Manager):
    def get_or_get_default(self, user):
        display_name = user.get_full_name() or user.email.split('@')[0]
        profile, created = self.get_or_create(
            user=user,
            defaults={
                'store_name': f"{display_name}'s Sanctuary",
                'slug': slugify(display_name),
                'brand_color': '#0A3029'
            }
        )
        return profile, created

class SellerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='seller_profile')
    store_name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(unique=True)
    logo_url = models.URLField(max_length=1000, blank=True, null=True)   # full brand logo (rectangular/square)
    icon_url = models.URLField(max_length=1000, blank=True, null=True)   # small square mark / app icon
    banner_url = models.URLField(max_length=1000, blank=True, null=True)
    brand_color = models.CharField(max_length=7, default='#0A3029')
    bio = models.TextField(blank=True, null=True)
    tagline = models.CharField(max_length=255, blank=True, null=True)
    
    gst_number = models.CharField(max_length=15, blank=True, null=True)
    gst_document_url = models.URLField(max_length=1000, blank=True, null=True)

    PAYOUT_TYPE_CHOICES = [('upi', 'UPI'), ('bank', 'Bank Account')]
    payout_type = models.CharField(max_length=10, choices=PAYOUT_TYPE_CHOICES, default='upi', blank=True)
    # Stored encrypted via sellers.encryption — never log or expose raw values
    payout_account = models.CharField(max_length=500, blank=True, null=True)
    ifsc_code = models.CharField(max_length=500, blank=True, null=True)
    account_holder_name = models.CharField(max_length=255, blank=True, null=True)
    
    location_city = models.CharField(max_length=100, blank=True, null=True)
    location_state = models.CharField(max_length=100, blank=True, null=True)
    location_pincode = models.CharField(max_length=10, blank=True, null=True)
    pickup_address = models.CharField(max_length=255, blank=True, null=True, help_text="Street address for NimbusPost pickup")
    
    # Authenticity & Skill Showcase
    expertise_tags = models.JSONField(default=list, blank=True)
    infrastructure_details = models.TextField(blank=True, null=True)
    experience_years = models.PositiveIntegerField(default=0)
    identity_verified = models.BooleanField(default=False)
    
    # Promotion & Carousel Control
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    
    total_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=5.0)
    
    # Shipping schedule: list of weekday ints (0=Monday … 6=Sunday)
    shipping_days = models.JSONField(
        default=list, blank=True,
        help_text='Weekdays the seller ships: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SellerProfileManager()

    def get_next_shipping_date(self):
        """Return the nearest upcoming date (today included) when this seller ships."""
        days = sorted(set(d for d in (self.shipping_days or []) if isinstance(d, int) and 0 <= d <= 6))
        if not days:
            return None
        today = date.today()
        current_weekday = today.weekday()  # 0=Monday
        for day in days:
            if day >= current_weekday:
                return today + timedelta(days=day - current_weekday)
        # Wrap to next week
        return today + timedelta(days=7 - current_weekday + days[0])

    def __str__(self):
        return self.store_name

class SellerShippingConfig(models.Model):
    """
    Per-seller, per-category shipping fee tiers. Managed by superadmin only.

    Fee resolution (ascending subtotal):
      subtotal < tier1_max  →  tier1_fee  (highest, e.g. ₹99)
      subtotal < tier2_max  →  tier2_fee  (lower,   e.g. ₹49)
      subtotal >= tier2_max →  0 (free)
    """
    ITEM_CATEGORY = [('light', 'Light'), ('heavy', 'Heavy')]

    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shipping_configs')
    item_category = models.CharField(max_length=10, choices=ITEM_CATEGORY)

    tier1_max = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text='Subtotal (₹) below which tier1_fee applies',
    )
    tier1_fee = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Shipping fee for subtotals below tier1_max',
    )
    tier2_max = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text='Subtotal (₹) below which tier2_fee applies (must be > tier1_max)',
    )
    tier2_fee = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Shipping fee for subtotals between tier1_max and tier2_max',
    )
    show_nudge_products = models.BooleanField(
        default=False,
        help_text="Show this seller's products in cart nudge to help buyers reach free shipping",
    )

    class Meta:
        unique_together = ('seller', 'item_category')
        verbose_name = 'Seller Shipping Config'
        verbose_name_plural = 'Seller Shipping Configs'

    def fee_for(self, subtotal: float) -> int:
        if subtotal < float(self.tier1_max):
            return int(self.tier1_fee)
        if subtotal < float(self.tier2_max):
            return int(self.tier2_fee)
        return 0

    def __str__(self):
        try:
            store = self.seller.seller_profile.store_name
        except Exception:
            store = str(self.seller_id)
        return f'{store} / {self.item_category}'


class AllowedSeller(models.Model):
    email = models.EmailField(unique=True, blank=True, null=True)
    phone = models.CharField(max_length=15, unique=True, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email or self.phone
