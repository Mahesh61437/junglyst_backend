"""
Shared shape for non-product order lines (workshops now, services later).

A booking is one paid Order of a non-product OrderType. The concrete model
lives in its own app and adds what it books (a workshop session, a service
visit, ...). Everything money-related is snapshotted at booking time, so
later edits to prices or commission never rewrite past revenue or payouts.
"""
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from .models import Order

PAISE = Decimal('0.01')
HUNDRED = Decimal('100')


class BookingStatus(models.TextChoices):
    PENDING_PAYMENT = 'pending_payment', _('Awaiting payment')
    CONFIRMED = 'confirmed', _('Confirmed')
    ATTENDED = 'attended', _('Attended')
    NO_SHOW = 'no_show', _('No-show')
    CANCELLED = 'cancelled', _('Cancelled')
    PAYMENT_FAILED = 'payment_failed', _('Payment failed')


# Bookings the buyer has paid for. These count as revenue and seat usage.
PAID_BOOKING_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.ATTENDED, BookingStatus.NO_SHOW)


def split_commission(gross_amount, commission_percent):
    """Return (commission_amount, seller_payout) for a buyer-paid amount.

    Listed booking prices are what the buyer pays; the platform keeps
    `commission_percent` of it and the organiser receives the rest.
    """
    gross = Decimal(gross_amount)
    commission = (gross * Decimal(commission_percent) / HUNDRED).quantize(PAISE, rounding=ROUND_HALF_UP)
    return commission, gross - commission


class AbstractBooking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='%(class)s')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='%(class)s_bookings',
    )
    # The organiser who is paid for this booking (snapshot of the listing's seller).
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='%(class)s_sales',
    )

    attendee_name = models.CharField(max_length=120)
    attendee_phone = models.CharField(max_length=15)
    attendee_email = models.EmailField()
    participants = models.PositiveSmallIntegerField(default=1)
    notes = models.TextField(blank=True, default='')

    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2)
    seller_payout = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(
        max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING_PAYMENT, db_index=True,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['-created_at']

    @property
    def reference(self):
        return self.order.order_number

    @property
    def is_paid(self):
        return self.status in PAID_BOOKING_STATUSES

    def price(self, unit_price, participants, commission_percent, quantity=None):
        """Fill the money snapshot fields. `quantity` is what the unit price is
        charged per (defaults to one per participant)."""
        self.unit_price = Decimal(unit_price)
        self.participants = participants
        self.total_amount = self.unit_price * (participants if quantity is None else quantity)
        self.commission_percent = Decimal(commission_percent)
        self.commission_amount, self.seller_payout = split_commission(self.total_amount, self.commission_percent)
