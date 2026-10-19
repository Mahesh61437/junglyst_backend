"""Booking creation, lifecycle and money maths for workshops."""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from notifications.models import AppNotification
from orders.bookings import BookingStatus, PAID_BOOKING_STATUSES, PAISE, split_commission
from orders.models import Order, OrderStatus, OrderType
from orders.order_types import OrderTypeHandler

from .models import (
    WorkshopBooking, WorkshopBookingSplit, WorkshopOrganiser, WorkshopPayout, WorkshopSession,
)

logger = logging.getLogger(__name__)

ZERO = Decimal('0')


class BookingError(Exception):
    """A booking request that can't be honoured. Message is safe for clients."""


def _generate_order_number():
    return f"JNG-{timezone.now().year}-{uuid.uuid4().hex[:5].upper()}"


def _unique_order_number():
    number = _generate_order_number()
    while Order.all_objects.filter(order_number=number).exists():
        number = _generate_order_number()
    return number


@dataclass
class BookingRequest:
    workshop: object
    session_id: int
    option_id: int
    participants: int
    attendee_name: str
    attendee_phone: str
    attendee_email: str
    notes: str = ''
    user: object = None
    builds: int = None  # shared (group) options: terrariums being built


def resolve_builds(option, participants, builds):
    """How many terrariums this booking pays for.

    Per-person options: one each. Shared options: the buyer picks how many;
    every terrarium needs at least one person and holds at most
    `max_people_per_build`.
    """
    if not option.is_shared:
        return participants
    builds = builds or 1
    if builds < 1:
        raise BookingError("Choose at least one terrarium.")
    if participants < builds:
        raise BookingError("Each shared terrarium needs at least one person.")
    if participants > builds * option.max_people_per_build:
        raise BookingError(
            f"{option.name} is shared by up to {option.max_people_per_build} people. "
            f"Add another terrarium for {participants} people.")
    return builds


# ── Co-host split ────────────────────────────────────────────────────────────

@dataclass
class OrganiserTerms:
    seller_id: object
    share_percent: Decimal
    commission_percent: Decimal


def organiser_terms(workshop):
    """Current split terms. A workshop without co-host rows (created before
    co-hosting existed) is treated as its lead seller taking 100%."""
    rows = [OrganiserTerms(o.seller_id, o.share_percent, o.commission_percent)
            for o in workshop.organisers.all()]
    return rows or [OrganiserTerms(workshop.seller_id, Decimal('100'), workshop.commission_percent)]


def allocate_by_share(total, shares):
    """Split `total` by percentage shares, rounded to paise, summing exactly to
    `total` (largest remainder goes to the first shares)."""
    total = Decimal(total)
    raw = [total * Decimal(share) / Decimal('100') for share in shares]
    parts = [r.quantize(PAISE, rounding='ROUND_DOWN') for r in raw]
    leftover = int(((total - sum(parts)) / PAISE).to_integral_value())
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)
    for i in by_remainder[:leftover]:
        parts[i] += PAISE
    return parts


def compute_splits(gross, terms):
    """[(terms, gross_part, commission, payout)] for a booking's gross amount."""
    parts = allocate_by_share(gross, [t.share_percent for t in terms])
    out = []
    for term, part in zip(terms, parts):
        commission, payout = split_commission(part, term.commission_percent)
        out.append((term, part, commission, payout))
    return out


def apply_splits(booking, terms):
    """(Re)write a saved booking's split rows and its money totals."""
    rows = compute_splits(booking.total_amount, terms)
    booking.splits.all().delete()
    WorkshopBookingSplit.objects.bulk_create([
        WorkshopBookingSplit(
            booking=booking, seller_id=term.seller_id, share_percent=term.share_percent,
            gross_amount=part, commission_percent=term.commission_percent,
            commission_amount=commission, seller_payout=payout,
        )
        for term, part, commission, payout in rows
    ])
    booking.commission_amount = sum((c for _, _, c, _ in rows), ZERO)
    booking.seller_payout = sum((p for _, _, _, p in rows), ZERO)
    # Effective blended rate, for display; the per-seller rates are on the splits.
    booking.commission_percent = (
        (booking.commission_amount * 100 / booking.total_amount).quantize(PAISE) if booking.total_amount else ZERO
    )
    booking.seller_id = terms[0].seller_id
    booking.save(update_fields=['commission_amount', 'seller_payout', 'commission_percent', 'seller', 'updated_at'])


RECALCULABLE_STATUSES = (BookingStatus.PENDING_PAYMENT,) + PAID_BOOKING_STATUSES


def unsettled_bookings(workshop, today=None):
    """Bookings whose split can still change without touching settled money.

    No settlement recorded for the workshop yet: every live booking. Once
    a transfer has been made, only sessions that haven't happened yet (money
    for past sessions may already have been paid out on the old split).
    """
    today = today or timezone.localdate()
    qs = WorkshopBooking.objects.filter(workshop=workshop, status__in=RECALCULABLE_STATUSES)
    if workshop.payouts.exists():
        qs = qs.filter(session_date__gte=today)
    return qs


def paid_booking_count(workshop):
    return WorkshopBooking.objects.filter(workshop=workshop, status__in=PAID_BOOKING_STATUSES).count()


@transaction.atomic
def recalculate_unsettled(workshop):
    """Re-split unsettled bookings on the workshop's current terms. Returns count."""
    terms = organiser_terms(workshop)
    bookings = list(unsettled_bookings(workshop).select_for_update())
    for booking in bookings:
        apply_splits(booking, terms)
    if bookings:
        logger.info("Re-split %d bookings of workshop %s on new co-host terms", len(bookings), workshop.pk)
    return len(bookings)


@transaction.atomic
def create_pending_booking(req):
    """Lock the session, check seats, and create Order + WorkshopBooking.

    Seats are held for SEAT_HOLD_MINUTES while the buyer pays.
    """
    workshop = req.workshop
    if not workshop.is_registration_open():
        raise BookingError("Registrations for this workshop are closed.")
    if req.participants < 1:
        raise BookingError("Book at least one seat.")

    option = workshop.options.filter(id=req.option_id, is_active=True).first()
    if option is None:
        raise BookingError("Please choose an available workshop option.")
    # One seat per terrarium: a shared terrarium's group takes a single seat.
    seats = resolve_builds(option, req.participants, req.builds)
    if seats > workshop.max_seats_per_booking:
        raise BookingError(f"You can book between 1 and {workshop.max_seats_per_booking} seats at a time.")

    # Row lock serialises concurrent bookings for the same session.
    session = (WorkshopSession.objects.select_for_update()
               .filter(id=req.session_id, workshop=workshop, is_active=True).first())
    if session is None:
        raise BookingError("Please choose an available session.")
    starts_at = timezone.make_aware(datetime.combine(session.date, session.start_time))
    if starts_at <= timezone.now():
        raise BookingError("This session has already started.")

    seats_taken = WorkshopSession.objects.with_seats_taken().get(pk=session.pk).seats_taken
    seats_left = session.capacity - seats_taken
    if seats > seats_left:
        if seats_left <= 0:
            raise BookingError("This session is fully booked. Please pick another date.")
        raise BookingError(f"Only {seats_left} seat(s) left in this session.")

    terms = organiser_terms(workshop)
    booking = WorkshopBooking(
        workshop=workshop, session=session, option=option, seller_id=terms[0].seller_id,
        user=req.user,
        attendee_name=req.attendee_name, attendee_phone=req.attendee_phone,
        attendee_email=req.attendee_email, notes=req.notes,
        workshop_title=workshop.title, option_name=option.name, builds=seats,
        session_date=session.date, session_start=session.start_time, session_end=session.end_time,
    )
    # Placeholder commission; apply_splits() below writes the real per-seller numbers.
    booking.price(option.price, req.participants, terms[0].commission_percent, quantity=seats)

    order = Order.objects.create(
        order_number=_unique_order_number(),
        order_type=OrderType.WORKSHOP,
        user=req.user,
        guest_email=None if req.user else req.attendee_email,
        guest_phone=None if req.user else req.attendee_phone,
        # Orders require an address blob; for a workshop it's the attendee contact.
        shipping_address={
            'full_name': req.attendee_name, 'phone': req.attendee_phone, 'email': req.attendee_email,
        },
        subtotal=booking.total_amount,
        total_amount=booking.total_amount,
        status=OrderStatus.PENDING,
    )
    booking.order = order
    booking.save()
    apply_splits(booking, terms)
    return booking


def _organiser_notification(booking, seller_id, payout):
    return AppNotification(
        user_id=seller_id,
        title="New workshop booking",
        message=(
            f"{booking.attendee_name} booked {booking.builds} seat(s) for "
            f"{booking.workshop_title} on {booking.session_date:%d %b %Y}, "
            f"{booking.session_start:%I:%M %p}. Your share: ₹{payout}. Ref {booking.reference}."
        ),
    )


def _overbooking_alerts(booking):
    """A payment that lands after its seat hold expired can push a session past
    capacity. The buyer has paid, so the booking stays confirmed; flag it and
    tell the super admins so they can move or refund someone."""
    from core.models import User

    seats_booked = (WorkshopBooking.objects
                    .filter(session_id=booking.session_id, status__in=PAID_BOOKING_STATUSES)
                    .aggregate(total=Coalesce(Sum('builds'), 0))['total'])
    capacity = WorkshopSession.objects.values_list('capacity', flat=True).get(pk=booking.session_id)
    over_by = seats_booked - capacity
    if over_by <= 0:
        return []
    note = f"Overbooked: session over capacity by {over_by} seat(s) when this payment was confirmed."
    WorkshopBooking.objects.filter(pk=booking.pk).update(
        admin_note="\n".join(part for part in (booking.admin_note, note) if part))
    logger.warning("Workshop session %s overbooked by %d (booking %s)", booking.session_id, over_by, booking.reference)
    message = (f"{booking.workshop_title} on {booking.session_date:%d %b %Y}, {booking.session_start:%I:%M %p} "
               f"has {seats_booked} seats sold for {capacity}. Booking {booking.reference} was paid after its "
               f"hold expired. Move an attendee to another session or refund them.")
    return [
        AppNotification(user_id=admin_id, title="Workshop session overbooked", message=message)
        for admin_id in User.objects.filter(is_superuser=True, is_deleted=False).values_list('id', flat=True)
    ]


def confirm_paid_booking(order):
    """Idempotent: flip the booking to confirmed once its order is paid."""
    now = timezone.now()
    updated = WorkshopBooking.objects.filter(
        order=order, status__in=[BookingStatus.PENDING_PAYMENT, BookingStatus.PAYMENT_FAILED],
    ).update(status=BookingStatus.CONFIRMED, confirmed_at=now, updated_at=now)
    if not updated:
        return False

    booking = WorkshopBooking.objects.select_related('user', 'order').get(order=order)
    notifications = [
        _organiser_notification(booking, split.seller_id, split.seller_payout)
        for split in booking.splits.all()
    ]
    if booking.user_id:
        notifications.append(AppNotification(
            user=booking.user,
            title="Workshop booked!",
            message=(
                f"You're in for {booking.workshop_title} on {booking.session_date:%d %b %Y} at "
                f"{booking.session_start:%I:%M %p}. Booking ref {booking.reference}."
            ),
        ))
    notifications += _overbooking_alerts(booking)
    AppNotification.objects.bulk_create(notifications)

    from .tasks import send_workshop_booking_emails_task
    try:
        send_workshop_booking_emails_task.delay(str(booking.id))
    except Exception:
        logger.exception("Could not queue workshop emails for %s", booking.reference)
    return True


def release_failed_booking(order):
    """Idempotent: an unpaid booking whose order failed gives its seats back."""
    now = timezone.now()
    return WorkshopBooking.objects.filter(
        order=order, status=BookingStatus.PENDING_PAYMENT,
    ).update(status=BookingStatus.PAYMENT_FAILED, updated_at=now)


def _gateway_outcome(payment):
    """('captured' | 'failed' | 'pending', gateway_payment_id, data) from the live gateway."""
    from payments.tasks import _cashfree_gateway_status, _razorpay_gateway_status
    if payment.gateway == 'razorpay' and payment.razorpay_order_id:
        return _razorpay_gateway_status(payment)
    if payment.cashfree_order_id:
        return _cashfree_gateway_status(payment)
    return 'pending', None, None


def release_abandoned_hold(booking):
    """The buyer closed or failed the payment window: give the seats back now
    instead of waiting for the hold to expire.

    The gateway is asked first, so a payment that did go through is captured
    (and the booking confirmed) rather than released. Returns the booking's
    resulting status. A capture that still lands later (e.g. a UPI app
    finishing after the modal closed) is confirmed by the normal capture paths.
    """
    from payments.models import Payment
    from payments.tasks import _capture_payment, cancel_payment_checks

    if booking.status != BookingStatus.PENDING_PAYMENT:
        return booking.status
    payment = Payment.objects.filter(order=booking.order).first()
    if payment is not None and payment.status != 'captured':
        outcome, gateway_payment_id, data = _gateway_outcome(payment)
        if outcome == 'captured':
            with transaction.atomic():
                _capture_payment(payment, gateway_payment_id, data)
            # Idempotent; confirms now rather than waiting on the on_commit hook,
            # so the response already reflects the captured payment.
            confirm_paid_booking(booking.order)
            booking.refresh_from_db()
            return booking.status

    with transaction.atomic():
        if payment is not None and payment.status != 'captured':
            payment.status = 'cancelled'
            payment.save(update_fields=['status', 'updated_at'])
        order = Order.objects.select_for_update().get(pk=booking.order_id)
        if order.status == OrderStatus.PENDING and not order.is_paid:
            order.status = OrderStatus.FAILED
            order.payment_status = 'failed'
            order.save(update_fields=['status', 'payment_status', 'updated_at'])
        release_failed_booking(order)
    if payment is not None:
        try:
            cancel_payment_checks(payment.id)
        except Exception:
            logger.exception("Could not cancel reconcile checks for %s", booking.reference)
    booking.refresh_from_db()
    return booking.status


def organiser_contact(workshop):
    """Contact shown to attendees after booking: the configured contact, else
    the lead organiser's account phone."""
    names = [_store_name(o.seller) for o in workshop.organisers.all()] or [_store_name(workshop.seller)]
    return {
        'name': workshop.contact_name or ' & '.join(names),
        'phone': workshop.contact_phone or (workshop.seller.phone or ''),
        'hosts': names,
    }


class WorkshopOrderHandler(OrderTypeHandler):
    order_select_related = ('workshop_booking__workshop__seller__seller_profile',)
    order_prefetch_related = ('workshop_booking__workshop__organisers__seller__seller_profile',)

    PRIVATE_SUMMARY_FIELDS = ('attendee_name', 'organiser_contact')  # venue is public on the workshop page

    def summarize(self, order, public=False):
        booking = getattr(order, 'workshop_booking', None)
        if booking is None:
            return None
        summary = self._summary(booking)
        if public:
            for field in self.PRIVATE_SUMMARY_FIELDS:
                summary.pop(field, None)
        return summary

    @staticmethod
    def _summary(booking):
        workshop = booking.workshop
        return {
            'type': 'workshop',
            'id': str(booking.id),
            'status': booking.status,
            'status_label': booking.get_status_display(),
            'title': booking.workshop_title,
            'slug': workshop.slug,
            'cover_image': workshop.cover_image,
            'option_name': booking.option_name,
            'participants': booking.participants,
            'builds': booking.builds,
            'shared': booking.builds < booking.participants,
            'session_date': booking.session_date.isoformat(),
            'session_start': booking.session_start.strftime('%H:%M'),
            'session_end': booking.session_end.strftime('%H:%M'),
            'attendee_name': booking.attendee_name,
            'venue_name': workshop.venue_name,
            'city': workshop.city,
            'address': workshop.address,
            'map_url': workshop.map_url,
            'organiser_contact': organiser_contact(workshop),
        }

    def on_paid(self, order):
        confirm_paid_booking(order)

    def on_failed(self, order):
        release_failed_booking(order)


# ── Money ────────────────────────────────────────────────────────────────────

_PAID = Q(status__in=PAID_BOOKING_STATUSES)


def _booking_totals(bookings):
    return bookings.aggregate(
        bookings=Count('id'),
        seats=Coalesce(Sum('builds'), 0),
        gross=Coalesce(Sum('total_amount'), ZERO),
        commission=Coalesce(Sum('commission_amount'), ZERO),
        seller_payout=Coalesce(Sum('seller_payout'), ZERO),
    )


def _store_name(user):
    profile = getattr(user, 'seller_profile', None)
    return profile.store_name if profile else (user.get_full_name() or user.email)


def seller_settlements(workshop, today=None):
    """Per co-host position for one workshop: share sold, commission, earned
    (sessions already held), settled and balance due."""
    from core.models import User

    today = today or timezone.localdate()
    paid = Q(booking__status__in=PAID_BOOKING_STATUSES)
    rows = (
        WorkshopBookingSplit.objects.filter(paid, booking__workshop=workshop)
        .values('seller')
        .annotate(
            gross=Sum('gross_amount'), commission=Sum('commission_amount'), payout=Sum('seller_payout'),
            earned=Coalesce(Sum('seller_payout', filter=Q(booking__session_date__lt=today)), ZERO),
        )
    )
    by_seller = {r['seller']: r for r in rows}
    paid_out = dict(workshop.payouts.values('seller').annotate(total=Sum('amount')).values_list('seller', 'total'))
    terms = {o.seller_id: o for o in workshop.organisers.all()}
    seller_ids = list(dict.fromkeys([*terms, *by_seller, *paid_out]))
    users = {u.id: u for u in User.objects.filter(id__in=seller_ids).select_related('seller_profile')}
    result = []
    for seller_id in seller_ids:
        row = by_seller.get(seller_id, {})
        earned = row.get('earned', ZERO)
        settled = paid_out.get(seller_id, ZERO)
        term = terms.get(seller_id)
        result.append({
            'seller': seller_id,
            'name': _store_name(users[seller_id]) if seller_id in users else '',
            'is_current_organiser': term is not None,
            'share_percent': term.share_percent if term else None,
            'commission_percent': term.commission_percent if term else None,
            'gross': row.get('gross', ZERO),
            'commission': row.get('commission', ZERO),
            'seller_payout': row.get('payout', ZERO),
            'earned': earned,
            'paid_out': settled,
            'balance_due': earned - settled,
        })
    return result


def workshop_financials(workshop, today=None):
    """Revenue + settlement position for one workshop.

    `earned` covers sessions that have already happened. That is what the
    organisers can be settled for. Upcoming sessions are shown separately.
    """
    today = today or timezone.localdate()
    paid = WorkshopBooking.objects.filter(workshop=workshop).filter(_PAID)
    totals = _booking_totals(paid)
    earned = _booking_totals(paid.filter(session_date__lt=today))
    paid_out = workshop.payouts.aggregate(total=Coalesce(Sum('amount'), ZERO))['total']
    sellers = seller_settlements(workshop, today)

    sessions = (
        workshop.sessions.with_seats_taken()
        .annotate(
            gross=Coalesce(Sum('bookings__total_amount', filter=Q(bookings__status__in=PAID_BOOKING_STATUSES)), ZERO),
            seller_payout=Coalesce(Sum('bookings__seller_payout', filter=Q(bookings__status__in=PAID_BOOKING_STATUSES)), ZERO),
        )
        .order_by('date', 'start_time')
    )
    capacity = sum(s.capacity for s in sessions if s.is_active)
    names = {s['seller']: s['name'] for s in sellers}
    return {
        'totals': totals,
        'earned': earned,
        'paid_out': paid_out,
        # Per seller, so one seller's advance can't hide what another is owed.
        'balance_due': sum((max(s['balance_due'], ZERO) for s in sellers), ZERO),
        'pending_settlement': totals['seller_payout'] - paid_out,
        'capacity': capacity,
        'occupancy_percent': round(totals['seats'] * 100 / capacity, 1) if capacity else 0,
        'sellers': sellers,
        'sessions': [
            {
                'id': s.id, 'date': s.date, 'start_time': s.start_time, 'end_time': s.end_time,
                'capacity': s.capacity, 'seats_booked': s.seats_booked, 'is_active': s.is_active,
                'gross': s.gross, 'seller_payout': s.seller_payout,
            }
            for s in sessions
        ],
        'payouts': [
            {**payout, 'seller_name': names.get(payout['seller'], '')}
            for payout in workshop.payouts.values('id', 'seller', 'amount', 'paid_on', 'reference', 'notes', 'created_at')
        ],
    }


def financials_overview(workshops, today=None):
    """One row per workshop for the admin revenue table. Two queries total."""
    today = today or timezone.localdate()
    paid_filter = Q(bookings__status__in=PAID_BOOKING_STATUSES)
    rows = workshops.annotate(
        paid_bookings=Count('bookings', filter=paid_filter),
        seats_sold=Coalesce(Sum('bookings__builds', filter=paid_filter), 0),
        gross=Coalesce(Sum('bookings__total_amount', filter=paid_filter), ZERO),
        commission=Coalesce(Sum('bookings__commission_amount', filter=paid_filter), ZERO),
        seller_payout=Coalesce(Sum('bookings__seller_payout', filter=paid_filter), ZERO),
        earned_payout=Coalesce(Sum(
            'bookings__seller_payout', filter=paid_filter & Q(bookings__session_date__lt=today),
        ), ZERO),
    )
    paid_out = dict(
        WorkshopPayout.objects.filter(workshop__in=workshops).values('workshop')
        .annotate(total=Sum('amount')).values_list('workshop', 'total')
    )
    return rows, paid_out


def seller_overview(seller, today=None):
    """Per-workshop totals of one seller's own splits and payouts."""
    from .models import Workshop

    today = today or timezone.localdate()
    paid = Q(booking__status__in=PAID_BOOKING_STATUSES)
    rows = (
        WorkshopBookingSplit.objects.filter(paid, seller=seller)
        .values('booking__workshop')
        .annotate(
            paid_bookings=Count('booking', distinct=True), seats_sold=Sum('booking__builds'),
            gross=Sum('gross_amount'), commission=Sum('commission_amount'), payout=Sum('seller_payout'),
            earned=Coalesce(Sum('seller_payout', filter=Q(booking__session_date__lt=today)), ZERO),
        )
    )
    by_workshop = {r['booking__workshop']: r for r in rows}
    paid_out = dict(
        WorkshopPayout.objects.filter(seller=seller).values('workshop')
        .annotate(total=Sum('amount')).values_list('workshop', 'total')
    )
    terms = {o.workshop_id: o for o in WorkshopOrganiser.objects.filter(seller=seller)}
    workshop_ids = set(by_workshop) | set(paid_out) | set(terms)
    workshops = Workshop.all_objects.filter(id__in=workshop_ids).order_by('-created_at')
    result = []
    for w in workshops:
        row = by_workshop.get(w.id, {})
        settled = paid_out.get(w.id, ZERO)
        term = terms.get(w.id)
        result.append({
            'id': w.id, 'title': w.title, 'slug': w.slug, 'status': w.status,
            'share_percent': term.share_percent if term else None,
            'commission_percent': term.commission_percent if term else None,
            'paid_bookings': row.get('paid_bookings', 0), 'seats_sold': row.get('seats_sold') or 0,
            'gross': row.get('gross', ZERO), 'seller_payout': row.get('payout', ZERO),
            'paid_out': settled, 'balance_due': row.get('earned', ZERO) - settled,
        })
    return result
