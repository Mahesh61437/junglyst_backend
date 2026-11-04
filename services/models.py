"""
Quote-based services.

A service is configured by the super admin as data, never as code:

    ServiceGroup      "Terrariums", "Maintenance", ... shown as separate home-page sections
     └─ ServiceOffering   "Custom closed terrarium"
         └─ ServiceFormField  the questions a customer answers when asking for a quote

A customer sends a QuoteRequest (always with a state and an expected budget).
The super admin assigns a seller; admin or seller then fill in the quote
(price, shipping, size, inclusions). Once the admin sends the pay request the
customer pays from My Orders, which creates an Order of OrderType.SERVICE with
a SubOrder for the seller so the normal seller shipping tools apply.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

MONEY_MAX_DIGITS = 12
DEFAULT_COMMISSION_PERCENT = Decimal('15.00')


class ServiceGroup(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.TextField(blank=True, default='')
    image_url = models.URLField(max_length=500, blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    show_on_home = models.BooleanField(default=True)
    home_sort_order = models.PositiveSmallIntegerField(default=0)
    # States this group's services deliver to (codes, e.g. ["KA", "TN"]). Empty = all of India.
    # A service can narrow it with its own list.
    allowed_states = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['home_sort_order', 'name']

    def __str__(self):
        return self.name


class ServiceOffering(models.Model):
    group = models.ForeignKey(ServiceGroup, on_delete=models.PROTECT, related_name='offerings')
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    description = models.TextField(blank=True, default='')
    image_url = models.URLField(max_length=500, blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    # Empty list = use the group's delivery states. Otherwise this service's own state codes.
    allowed_states = models.JSONField(default=list, blank=True)
    # Shown as a hint on the form ("usually starts around ₹3,000"); never blocks a request.
    suggested_min_budget = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0'))],
    )
    # Platform's cut of the item price when this offering is paid (shipping goes to the seller).
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=DEFAULT_COMMISSION_PERCENT,
        validators=[MinValueValidator(Decimal('0'))],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f'{self.group.name} / {self.name}'

    @property
    def delivery_states(self):
        """State codes this service delivers to; empty means all of India."""
        return self.allowed_states or self.group.allowed_states

    @property
    def delivery_state_names(self):
        from shipping.regions import STATE_NAMES
        return [STATE_NAMES[code] for code in self.delivery_states if code in STATE_NAMES]


class FormFieldType(models.TextChoices):
    TEXT = 'text', _('Short text')
    TEXTAREA = 'textarea', _('Long text')
    NUMBER = 'number', _('Number')
    SELECT = 'select', _('Choose one')
    MULTISELECT = 'multiselect', _('Choose several')
    DATE = 'date', _('Date')


class ServiceFormField(models.Model):
    offering = models.ForeignKey(ServiceOffering, on_delete=models.CASCADE, related_name='form_fields')
    key = models.SlugField(max_length=60)
    label = models.CharField(max_length=160)
    field_type = models.CharField(max_length=20, choices=FormFieldType.choices, default=FormFieldType.TEXT)
    required = models.BooleanField(default=False)
    # select / multiselect: list of allowed option strings.
    choices = models.JSONField(default=list, blank=True)
    min_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    max_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    max_length = models.PositiveSmallIntegerField(null=True, blank=True)
    help_text = models.CharField(max_length=240, blank=True, default='')
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['offering', 'key'], name='services_formfield_unique_key'),
        ]

    def __str__(self):
        return f'{self.offering.name}: {self.label}'


class QuoteStatus(models.TextChoices):
    SUBMITTED = 'submitted', _('Submitted')
    IN_REVIEW = 'in_review', _('In review')
    AWAITING_PAYMENT = 'awaiting_payment', _('Awaiting payment')
    PAID = 'paid', _('Paid')
    DELIVERED = 'delivered', _('Delivered')
    EXPIRED = 'expired', _('Payment window expired')
    DECLINED = 'declined', _('Declined')
    CANCELLED = 'cancelled', _('Cancelled')


# Days the customer has to pay once the pay request is sent.
PAY_REQUEST_VALID_DAYS = 3
# The customer is reminded this many hours before the pay-by deadline.
PAY_BY_REMINDER_HOURS = 24
# Most reference photos a customer can attach to a request.
MAX_REFERENCE_IMAGES = 5

# Statuses in which the quote figures can still change.
EDITABLE_QUOTE_STATUSES = (QuoteStatus.SUBMITTED, QuoteStatus.IN_REVIEW)
OPEN_QUOTE_STATUSES = (QuoteStatus.SUBMITTED, QuoteStatus.IN_REVIEW, QuoteStatus.AWAITING_PAYMENT)
# Requests where the customer has not paid yet (so they can cancel outright).
UNPAID_STATUSES = OPEN_QUOTE_STATUSES + (QuoteStatus.EXPIRED,)

# How the request lists are split: work in progress, done, and closed without an order.
QUOTE_SECTIONS = {
    'active': OPEN_QUOTE_STATUSES + (QuoteStatus.PAID, QuoteStatus.EXPIRED),
    'delivered': (QuoteStatus.DELIVERED,),
    'closed': (QuoteStatus.DECLINED, QuoteStatus.CANCELLED),
}


class QuoteRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=20, unique=True, editable=False)
    offering = models.ForeignKey(ServiceOffering, on_delete=models.PROTECT, related_name='quote_requests')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='quote_requests',
    )

    contact_name = models.CharField(max_length=120)
    contact_phone = models.CharField(max_length=15)
    contact_email = models.EmailField()
    state_code = models.CharField(max_length=2, db_index=True)

    budget_min = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2)
    budget_max = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2)

    # What the customer answered, with the questions as they were asked, so later
    # edits to the form never rewrite an old request.
    answers = models.JSONField(default=dict)
    form_snapshot = models.JSONField(default=list)
    reference_images = models.JSONField(default=list, blank=True)
    customer_notes = models.TextField(blank=True, default='')

    status = models.CharField(
        max_length=20, choices=QuoteStatus.choices, default=QuoteStatus.SUBMITTED, db_index=True,
    )
    assigned_seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='assigned_quote_requests',
    )

    # ── The quote (filled by super admin or the assigned seller) ──
    item_price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2, null=True, blank=True)
    shipping_fee = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2, default=Decimal('0'))
    shape = models.CharField(max_length=60, blank=True, default='')
    length_cm = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    width_cm = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    # [{"label": "Glass container", "amount": "1200.00" | null}, ...]
    inclusions = models.JSONField(default=list, blank=True)
    quote_notes = models.TextField(blank=True, default='')
    admin_notes = models.TextField(blank=True, default='')
    # Platform cut snapshotted when the pay request is sent.
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    quote_sent_at = models.DateTimeField(null=True, blank=True)
    # Deadline to pay; after it the request becomes EXPIRED until the admin renews it.
    pay_by = models.DateTimeField(null=True, blank=True, db_index=True)
    # Why the admin cancelled (shown to the customer). Refunds are made manually.
    cancel_reason = models.CharField(max_length=500, blank=True, default='')
    # Set once the "pay soon" reminder went out for the current pay-by date.
    pay_by_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    order = models.OneToOneField(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='service_quote',
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['assigned_seller', 'status']),
        ]

    def __str__(self):
        return self.reference

    @property
    def total_amount(self):
        if self.item_price is None:
            return None
        return self.item_price + self.shipping_fee

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generate_reference()
        super().save(*args, **kwargs)


def generate_reference():
    """JNG-<year>-<5 hex>, the same shape as order numbers. The first payment's Order reuses it,
    so the customer sees one ID from request to delivery; it is kept unique across both tables."""
    from orders.models import Order

    while True:
        reference = f'JNG-{timezone.now().year}-{uuid.uuid4().hex[:5].upper()}'
        taken = (QuoteRequest.objects.filter(reference=reference).exists()
                 or Order.all_objects.filter(order_number=reference).exists())
        if not taken:
            return reference


class ChargeStatus(models.TextChoices):
    REQUESTED = 'requested', _('Awaiting payment')
    PAID = 'paid', _('Paid')
    CANCELLED = 'cancelled', _('Cancelled')


class PayoutStatus(models.TextChoices):
    CREDITED = 'credited', _('Added to the seller payout')
    MANUAL_DUE = 'manual_due', _('Seller must be paid separately')
    MANUAL_DONE = 'manual_done', _('Paid to the seller separately')


class AdditionalCharge(models.Model):
    """Extra money asked after the first payment (an upgrade, extra plants, a bigger container...).

    Each one is paid through its own Order (the gateway allows one payment per order), and the
    seller's share is added to the original sub-order's payout when it is paid.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    quote_request = models.ForeignKey(QuoteRequest, on_delete=models.CASCADE, related_name='additional_charges')
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2)
    status = models.CharField(max_length=20, choices=ChargeStatus.choices, default=ChargeStatus.REQUESTED, db_index=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    order = models.OneToOneField(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='additional_charge',
    )
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    seller_payout = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2)
    # When the seller's settlement was already paid out before this extra payment came in,
    # the admin pays the extra amount separately and records it here.
    payout_status = models.CharField(max_length=20, choices=PayoutStatus.choices, default=PayoutStatus.CREDITED)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.quote_request.reference}: {self.description}'


class QuotePaymentAttempt(models.Model):
    """Every Order created to collect money for a request (first payment or an extra).

    The gateway allows one payment per Order, so a retry needs a fresh Order. Keeping
    every attempt here means a late capture on an older attempt is still matched to
    its request instead of being lost.
    """
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE, related_name='quote_attempt')
    quote_request = models.ForeignKey(QuoteRequest, on_delete=models.CASCADE, related_name='payment_attempts')
    charge = models.ForeignKey(
        'AdditionalCharge', on_delete=models.CASCADE, null=True, blank=True, related_name='payment_attempts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class QuoteEvent(models.Model):
    """Who changed what on a request. Append-only history shown to admins."""
    quote_request = models.ForeignKey(QuoteRequest, on_delete=models.CASCADE, related_name='events')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    action = models.CharField(max_length=40)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
