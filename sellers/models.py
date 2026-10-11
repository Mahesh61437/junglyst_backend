from datetime import date, datetime, time, timedelta
from django.db import models
from django.utils import timezone
from core.models import User
from django.utils.text import slugify
from shipping.regions import REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST, STATE_NAMES

# Region code → SellerProfile field, kept in one place for filters and serializers
REGION_SHIPS_FIELDS = {
    REGION_SOUTH: 'ships_to_south',
    REGION_NORTH: 'ships_to_north',
    REGION_NORTH_EAST: 'ships_to_north_east',
}
REGION_FEE_FIELDS = {
    REGION_SOUTH: 'delivery_fee_south',
    REGION_NORTH: 'delivery_fee_north',
    REGION_NORTH_EAST: 'delivery_fee_north_east',
}

WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# Hard cap so we never loop forever if a seller blacks out every upcoming day
MAX_SHIPPING_LOOKAHEAD_DAYS = 90

# Default dispatch days for a fresh seller — Mon/Wed/Fri (0, 2, 4).
# Applied automatically at profile creation and as a fallback so checkout
# never blocks just because a seller skipped the (optional) onboarding step.
DEFAULT_SHIPPING_DAYS = [0, 2, 4]


def _default_shipping_days():
    # Return a fresh list — JSONField default must be a callable, never a mutable literal
    return list(DEFAULT_SHIPPING_DAYS)


class SellerProfileManager(models.Manager):
    def get_or_get_default(self, user):
        display_name = user.get_full_name() or user.email.split('@')[0]
        profile, created = self.get_or_create(
            user=user,
            defaults={
                'store_name': f"{display_name}'s Store",
                'slug': slugify(display_name),
                'brand_color': '#0A3029',
                'shipping_days': _default_shipping_days(),
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
    pickup_address = models.CharField(max_length=255, blank=True, null=True, help_text="Street address for pickup")
    # Cached after first successful registration with Shiprocket.
    # Must match exactly the name under Settings → Manage Pickup Addresses in Shiprocket.
    shiprocket_pickup_location = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Shiprocket pickup location name (auto-set on first shipment)"
    )

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

    # Shipping schedule: list of weekday ints (0=Monday … 6=Sunday).
    # Defaults to Mon/Wed/Fri so new sellers can sell immediately even if they
    # skip the dispatch-days step during onboarding.
    shipping_days = models.JSONField(
        default=_default_shipping_days, blank=True,
        help_text='Weekdays the seller ships: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun. Defaults to Mon/Wed/Fri.',
    )

    # Orders placed on a shipping day before this time ship that day; after, they roll over.
    daily_cutoff_time = models.TimeField(
        default=time(12, 0),
        help_text='Daily order cut-off (IST). Orders placed on a shipping day after this time roll to next shipping day.',
    )

    # Delivery regions (see shipping.regions). All three on = ships all over India.
    ships_to_south = models.BooleanField(default=True, help_text='Ships to South India (AP, TS, TN, KA, KL)')
    ships_to_north = models.BooleanField(default=True, help_text='Ships to North India (all except South and North East)')
    ships_to_north_east = models.BooleanField(default=True, help_text='Ships to North East India')

    # Optional flat delivery fee per region. When blank, the seller's slab-based
    # SellerShippingConfig fee applies instead.
    delivery_fee_south = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    delivery_fee_north = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    delivery_fee_north_east = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SellerProfileManager()

    def ships_to(self, region) -> bool:
        """Whether this seller delivers to the region. Unknown region → True (don't block)."""
        field = REGION_SHIPS_FIELDS.get(region)
        return getattr(self, field) if field else True

    @property
    def service_regions(self) -> list:
        return [region for region, field in REGION_SHIPS_FIELDS.items() if getattr(self, field)]

    def delivery_fee_for(self, region):
        """Flat delivery fee configured for the region, or None to fall back to slabs."""
        field = REGION_FEE_FIELDS.get(region)
        return getattr(self, field) if field else None

    @property
    def excluded_state_codes(self) -> list:
        """State codes inside served regions that this seller won't deliver to.
        Uses prefetched `excluded_states` when present (prefetch it for lists)."""
        return sorted(e.state_code for e in self.excluded_states.all())

    def _blackout_dates(self):
        """Set of all individual dates currently blacked out for this seller."""
        out = set()
        # Avoid touching DB before the related table exists (e.g. during migrations / fresh installs)
        try:
            ranges = list(self.blackout_dates.all().values('start_date', 'end_date'))
        except Exception:
            return out
        for r in ranges:
            d = r['start_date']
            end = r['end_date']
            while d <= end:
                out.add(d)
                d += timedelta(days=1)
        return out

    def get_next_shipping_date(self, as_of=None):
        """
        Return the nearest upcoming date this seller will actually dispatch.

        Honors:
          - shipping_days (which weekdays they ship)
          - daily_cutoff_time (if today is a shipping day but it's past the cut-off, roll forward)
          - blackout dates (vacation / OOO — skip them)

        as_of: datetime in project TZ (Asia/Kolkata). Defaults to now().
        Returns a date, or None if no shipping_days configured.
        """
        days = sorted(set(d for d in (self.shipping_days or []) if isinstance(d, int) and 0 <= d <= 6))
        if not days:
            return None

        if as_of is None:
            as_of = timezone.localtime()
        elif timezone.is_naive(as_of):
            as_of = timezone.make_aware(as_of)
        else:
            as_of = timezone.localtime(as_of)

        today = as_of.date()
        now_t = as_of.time()
        cutoff = self.daily_cutoff_time or time(12, 0)
        blackouts = self._blackout_dates()

        for offset in range(MAX_SHIPPING_LOOKAHEAD_DAYS):
            candidate = today + timedelta(days=offset)
            if candidate.weekday() not in days:
                continue
            if candidate in blackouts:
                continue
            # Same-day shipping only if it's before cutoff
            if offset == 0 and now_t >= cutoff:
                continue
            return candidate
        return None

    def __str__(self):
        return self.store_name


class SellerBlackoutDate(models.Model):
    """A range of dates the seller will not ship (vacation, sick, OOO)."""
    seller = models.ForeignKey(
        SellerProfile, on_delete=models.CASCADE, related_name='blackout_dates'
    )
    start_date = models.DateField()
    end_date = models.DateField(help_text='Inclusive — last unavailable day')
    reason = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_date']
        indexes = [models.Index(fields=['seller', 'start_date'])]
        verbose_name = 'Seller Blackout Date'
        verbose_name_plural = 'Seller Blackout Dates'

    def __str__(self):
        return f'{self.seller.store_name}: {self.start_date} → {self.end_date}'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.end_date < self.start_date:
            raise ValidationError({'end_date': 'end_date must be on or after start_date'})


class SellerExcludedState(models.Model):
    """A state the seller won't deliver to, even though its region is enabled."""
    STATE_CHOICES = sorted(STATE_NAMES.items(), key=lambda item: item[1])

    seller = models.ForeignKey(SellerProfile, on_delete=models.CASCADE, related_name='excluded_states')
    state_code = models.CharField(max_length=2, choices=STATE_CHOICES)

    class Meta:
        unique_together = ('seller', 'state_code')
        indexes = [models.Index(fields=['state_code'])]
        verbose_name = 'Excluded Delivery State'
        verbose_name_plural = 'Excluded Delivery States'

    def __str__(self):
        return f'{self.seller.store_name} ✕ {STATE_NAMES.get(self.state_code, self.state_code)}'


class SellerShippingConfig(models.Model):
    """
    Per-seller, per-category shipping fee tiers. Managed by superadmin only.

    Fee resolution (ascending subtotal):
      subtotal < tier1_max  →  tier1_fee  (highest, e.g. ₹99)
      subtotal < tier2_max  →  tier2_fee  (lower,   e.g. ₹49)
      subtotal >= tier2_max →  0 (free)
    
    Categories: light, heavy, hybrid (hybrid applies when cart has both light and heavy items)
    """
    ITEM_CATEGORY = [('light', 'Light'), ('heavy', 'Heavy'), ('hybrid', 'Hybrid (Light + Heavy)')]

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


class ShippingDefaultConfig(models.Model):
    """
    Platform-wide default shipping tier values per item category.
    Used to pre-fill the form when a superadmin adds a new seller config.
    Managed via Django admin or PATCH /sellers/shipping-configs/defaults/.
    Categories: light, heavy, hybrid (hybrid applies when cart has both light and heavy items)
    """
    ITEM_CATEGORY = [('light', 'Light'), ('heavy', 'Heavy'), ('hybrid', 'Hybrid (Light + Heavy)')]

    item_category = models.CharField(max_length=10, choices=ITEM_CATEGORY, unique=True)
    tier1_max = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text='Default subtotal (₹) below which tier1_fee applies',
    )
    tier1_fee = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Default shipping fee for subtotals below tier1_max',
    )
    tier2_max = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text='Default subtotal (₹) below which tier2_fee applies',
    )
    tier2_fee = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Default shipping fee for subtotals between tier1_max and tier2_max',
    )

    class Meta:
        verbose_name = 'Shipping Default Config'
        verbose_name_plural = 'Shipping Default Configs'

    def __str__(self):
        return f'Default / {self.item_category}'


class AllowedSeller(models.Model):
    email = models.EmailField(unique=True, blank=True, null=True)
    phone = models.CharField(max_length=15, unique=True, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email or self.phone
