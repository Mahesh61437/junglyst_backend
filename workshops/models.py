"""
Workshops: hands-on terrarium / aquarium sessions that Junglyst runs in
collaboration with a seller (the organiser). Junglyst lists the workshop and
takes the payment; the organiser runs it and is paid out minus commission.

    Workshop ──< WorkshopOrganiser (co-hosting sellers: revenue share % + commission %)
             ──< WorkshopOption    (bowl size / shape tiers, each with a price)
             ──< WorkshopSession   (date + time + seat capacity)
             ──< WorkshopBooking   (one paid Order; see orders.bookings)
                    ──< WorkshopBookingSplit (that booking's money per organiser)
             ──< WorkshopPayout    (settlement transfers to an organiser)
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from core.models import SoftDeleteModel
from orders.bookings import AbstractBooking, BookingStatus, PAID_BOOKING_STATUSES
from orders.models import Order

# An unpaid booking holds its seats this long, so two buyers can't both pay
# for the last seat. After that the seats return to the pool.
SEAT_HOLD_MINUTES = 20
DEFAULT_COMMISSION_PERCENT = Decimal('20.00')
DEFAULT_MAX_SEATS_PER_BOOKING = 6


class WorkshopCategory(models.TextChoices):
    TERRARIUM = 'terrarium', _('Terrarium')
    AQUARIUM = 'aquarium', _('Aquarium')
    AQUASCAPING = 'aquascaping', _('Aquascaping')
    PALUDARIUM = 'paludarium', _('Paludarium')
    KOKEDAMA = 'kokedama', _('Kokedama')
    OTHER = 'other', _('Other')


class SkillLevel(models.TextChoices):
    ALL_LEVELS = 'all_levels', _('All levels')
    BEGINNER = 'beginner', _('Beginner')
    INTERMEDIATE = 'intermediate', _('Intermediate')
    ADVANCED = 'advanced', _('Advanced')


class RegistrationState(models.TextChoices):
    OPENS_LATER = 'opens_later', _('Opens later')
    OPEN = 'open', _('Open')
    SOLD_OUT = 'sold_out', _('Sold out')
    CLOSED = 'closed', _('Closed')
    NO_DATES = 'no_dates', _('No upcoming dates')


class NoticeTone(models.TextChoices):
    INFO = 'info', _('Info')
    WARNING = 'warning', _('Important')
    SUCCESS = 'success', _('Good news')


class WorkshopStatus(models.TextChoices):
    DRAFT = 'draft', _('Draft')
    PUBLISHED = 'published', _('Published')
    CANCELLED = 'cancelled', _('Cancelled')
    COMPLETED = 'completed', _('Completed')


# Fixed path segments under /api/workshops/ that a slug must never shadow.
RESERVED_SLUGS = {'admin', 'seller', 'bookings', 'my-bookings'}


def _unique_slug(model, title, instance_pk=None):
    base = slugify(title)[:200] or 'workshop'
    slug, suffix = base, 2
    while slug in RESERVED_SLUGS or model.all_objects.filter(slug=slug).exclude(pk=instance_pk).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


class Workshop(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    category = models.CharField(max_length=20, choices=WorkshopCategory.choices, default=WorkshopCategory.TERRARIUM)
    tagline = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='')
    # Short "what you get" bullets, e.g. "3 inch round glass bowl", "Welcome drink".
    highlights = models.JSONField(default=list, blank=True)
    what_to_bring = models.JSONField(default=list, blank=True)
    # Admin-defined extra sections, e.g. Terms & conditions, Notes, What you'll make:
    # [{"title": "...", "body": "..."}], shown in this order as plain text.
    custom_sections = models.JSONField(default=list, db_default=Value([], output_field=models.JSONField()), blank=True)
    # Image URLs from /api/core/upload/. The first one is the cover.
    images = models.JSONField(default=list, blank=True)

    # Lead organiser, kept in sync with the first WorkshopOrganiser row. The
    # money split lives on WorkshopOrganiser / WorkshopBookingSplit.
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='organized_workshops',
        help_text='Lead organiser (first co-host).',
    )
    status = models.CharField(max_length=20, choices=WorkshopStatus.choices, default=WorkshopStatus.DRAFT, db_index=True)
    is_featured = models.BooleanField(default=False)

    registration_opens_at = models.DateTimeField(null=True, blank=True)
    registration_closes_at = models.DateTimeField(null=True, blank=True)

    venue_name = models.CharField(max_length=160, blank=True, default='')
    address = models.TextField(blank=True, default='')
    city = models.CharField(max_length=80, blank=True, default='')
    map_url = models.URLField(max_length=500, blank=True, default='')

    duration_minutes = models.PositiveIntegerField(default=120)
    skill_level = models.CharField(max_length=20, choices=SkillLevel.choices, default=SkillLevel.ALL_LEVELS)
    min_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_seats_per_booking = models.PositiveSmallIntegerField(
        default=DEFAULT_MAX_SEATS_PER_BOOKING, validators=[MinValueValidator(1), MaxValueValidator(50)],
    )
    cancellation_policy = models.TextField(blank=True, default='')
    # Highlighted banner on the workshop page, e.g. "Venue not finalised yet".
    notice = models.CharField(max_length=300, blank=True, default='', db_default='')
    notice_tone = models.CharField(max_length=10, choices=NoticeTone.choices, default=NoticeTone.INFO,
                                   db_default=NoticeTone.INFO)

    # Who attendees call about the session. Shown only to people who booked
    # (ticket, My Orders, confirmation email), never on the public page.
    contact_name = models.CharField(max_length=120, blank=True, default='', db_default='')
    contact_phone = models.CharField(max_length=15, blank=True, default='', db_default='')

    # Default commission for newly added co-hosts. Each WorkshopOrganiser row
    # carries the commission actually applied to that seller's share.
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=DEFAULT_COMMISSION_PERCENT,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Workshop, self.title, self.pk)
        super().save(*args, **kwargs)

    @property
    def cover_image(self):
        return self.images[0] if self.images else ''

    def is_registration_open(self, now=None):
        """Time window only. Seat availability is per session (see registration_state)."""
        now = now or timezone.now()
        if self.status != WorkshopStatus.PUBLISHED or self.is_deleted:
            return False
        if self.registration_opens_at and now < self.registration_opens_at:
            return False
        if self.registration_closes_at and now > self.registration_closes_at:
            return False
        return True

    def registration_state(self, upcoming_sessions, now=None):
        """Where registrations stand right now, worked out on every request so
        they open and close on their own: at the configured times, and as soon
        as every upcoming session is full.

        `upcoming_sessions` must carry the `seats_taken` annotation.
        """
        now = now or timezone.now()
        if self.status != WorkshopStatus.PUBLISHED or self.is_deleted:
            return RegistrationState.CLOSED
        if self.registration_closes_at and now > self.registration_closes_at:
            return RegistrationState.CLOSED
        if not upcoming_sessions:
            return RegistrationState.NO_DATES
        if all(s.capacity - s.seats_taken <= 0 for s in upcoming_sessions):
            return RegistrationState.SOLD_OUT
        if self.registration_opens_at and now < self.registration_opens_at:
            return RegistrationState.OPENS_LATER
        return RegistrationState.OPEN


class WorkshopOrganiser(models.Model):
    """A seller co-hosting the workshop. Each booking is split across the
    organisers by `share_percent` (totals 100); Junglyst keeps
    `commission_percent` of each seller's share."""
    workshop = models.ForeignKey(Workshop, on_delete=models.CASCADE, related_name='organisers')
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='workshop_cohosting')
    share_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100'))],
    )
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [models.UniqueConstraint(fields=['workshop', 'seller'], name='uniq_workshop_organiser')]

    def __str__(self):
        return f"{self.workshop.title} · {self.seller_id} ({self.share_percent}%)"


class OptionPricing(models.TextChoices):
    PER_PERSON = 'per_person', _('Per person')          # everyone builds their own
    PER_BUILD = 'per_build', _('Per shared terrarium')  # a group builds one together


class WorkshopOption(models.Model):
    """A size / shape tier the attendee builds, e.g. "Round bowl · 3 inch".

    Group booking: a PER_BUILD option is priced per terrarium and shared by up
    to `max_people_per_build` people. A terrarium takes one seat and the
    price follows the number of terrariums ("builds").

    `images` shows this option's own build (e.g. the 6 inch bowl); the page
    falls back to the workshop gallery when it is empty.
    """
    workshop = models.ForeignKey(Workshop, on_delete=models.CASCADE, related_name='options')
    name = models.CharField(max_length=120)
    shape = models.CharField(max_length=60, blank=True, default='')
    size = models.CharField(max_length=60, blank=True, default='')
    description = models.CharField(max_length=300, blank=True, default='')
    images = models.JSONField(default=list, db_default=Value([], output_field=models.JSONField()), blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('1'))])
    pricing = models.CharField(max_length=12, choices=OptionPricing.choices, default=OptionPricing.PER_PERSON,
                               db_default=OptionPricing.PER_PERSON)
    max_people_per_build = models.PositiveSmallIntegerField(
        default=1, db_default=1, validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text='Shared options: how many people can build one terrarium together.',
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'price']

    @property
    def is_shared(self):
        return self.pricing == OptionPricing.PER_BUILD

    def __str__(self):
        return f"{self.workshop.title} — {self.name}"


def seat_holding_filter(prefix='', now=None):
    """Q for bookings that occupy seats: paid ones, plus unpaid holds that are
    still inside the hold window on an order that hasn't failed."""
    now = now or timezone.now()
    hold_cutoff = now - timedelta(minutes=SEAT_HOLD_MINUTES)
    return (
        Q(**{f'{prefix}status__in': PAID_BOOKING_STATUSES})
        | Q(**{
            f'{prefix}status': BookingStatus.PENDING_PAYMENT,
            f'{prefix}created_at__gte': hold_cutoff,
        })
    )


class WorkshopSessionQuerySet(models.QuerySet):
    def with_seats_taken(self, now=None):
        # A seat is a workstation: one terrarium, however many people share it.
        return self.annotate(
            seats_taken=Coalesce(
                Sum('bookings__builds', filter=seat_holding_filter('bookings__', now)), 0,
            ),
            seats_booked=Coalesce(
                Sum('bookings__builds', filter=Q(bookings__status__in=PAID_BOOKING_STATUSES)), 0,
            ),
        )

    def upcoming(self, now=None):
        now = now or timezone.localtime()
        return self.filter(is_active=True).filter(
            Q(date__gt=now.date()) | Q(date=now.date(), start_time__gt=now.time())
        )


class WorkshopSession(models.Model):
    workshop = models.ForeignKey(Workshop, on_delete=models.CASCADE, related_name='sessions')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default='')

    objects = WorkshopSessionQuerySet.as_manager()

    class Meta:
        ordering = ['date', 'start_time']
        indexes = [models.Index(fields=['workshop', 'date'])]

    def __str__(self):
        return f"{self.workshop.title} · {self.date} {self.start_time:%H:%M}"


class WorkshopBooking(AbstractBooking):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='workshop_booking')
    workshop = models.ForeignKey(Workshop, on_delete=models.PROTECT, related_name='bookings')
    session = models.ForeignKey(WorkshopSession, on_delete=models.PROTECT, related_name='bookings')
    option = models.ForeignKey(WorkshopOption, on_delete=models.PROTECT, related_name='bookings')

    # Snapshots so the booking still reads correctly if the listing is edited.
    workshop_title = models.CharField(max_length=160)
    option_name = models.CharField(max_length=120)
    # Terrariums being built. Per-person options: one each (= participants).
    # Shared options: fewer, each built by a group. Price = unit_price × builds.
    builds = models.PositiveSmallIntegerField(default=1, db_default=1)
    session_date = models.DateField()
    session_start = models.TimeField()
    session_end = models.TimeField()

    class Meta(AbstractBooking.Meta):
        indexes = [
            models.Index(fields=['session', 'status']),
            models.Index(fields=['workshop', 'status']),
            models.Index(fields=['seller', 'status']),
        ]

    def __str__(self):
        return f"{self.order.order_number} · {self.workshop_title}"


class WorkshopBookingSplit(models.Model):
    """One organiser's slice of a booking. The booking's commission_amount and
    seller_payout are the sums of its splits."""
    booking = models.ForeignKey(WorkshopBooking, on_delete=models.CASCADE, related_name='splits')
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='workshop_booking_splits')
    share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2)
    seller_payout = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['-share_percent', 'id']
        indexes = [models.Index(fields=['seller', 'booking'])]
        constraints = [models.UniqueConstraint(fields=['booking', 'seller'], name='uniq_booking_split_seller')]


class WorkshopPayout(models.Model):
    """A settlement transfer from Junglyst to the organiser for a workshop."""
    workshop = models.ForeignKey(Workshop, on_delete=models.PROTECT, related_name='payouts')
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='workshop_payouts')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    paid_on = models.DateField(default=timezone.localdate)
    reference = models.CharField(max_length=100, blank=True, default='', help_text='UTR / transfer reference')
    notes = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_on', '-created_at']

    def __str__(self):
        return f"₹{self.amount} → {self.workshop.title}"
