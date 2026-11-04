"""Quote lifecycle: edit, assign, send the pay request, pay, and the Order handler."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import User
from notifications.models import AppNotification
from orders.bookings import PAISE, unique_order_number
from orders.models import Order, OrderItem, OrderStatus, OrderType, SellerSettlement, SubOrder
from orders.order_types import OrderTypeHandler
from payments.models import Payment

from .models import (
    EDITABLE_QUOTE_STATUSES, OPEN_QUOTE_STATUSES, PAY_BY_REMINDER_HOURS, PAY_REQUEST_VALID_DAYS, AdditionalCharge,
    ChargeStatus, PayoutStatus, QuoteEvent, QuotePaymentAttempt, QuoteRequest, QuoteStatus, UNPAID_STATUSES,
)

logger = logging.getLogger(__name__)

HUNDRED = Decimal('100')
QUOTE_FIGURE_FIELDS = (
    'item_price', 'shipping_fee', 'shape', 'length_cm', 'width_cm', 'height_cm', 'inclusions', 'quote_notes',
)


class QuoteError(Exception):
    """An action the request's current state doesn't allow. Message is safe for clients."""


class QuoteExpired(QuoteError):
    """The customer tried to pay after the pay-by deadline."""


def new_pay_by():
    return timezone.now() + timedelta(days=PAY_REQUEST_VALID_DAYS)


def record_event(quote, actor, action, changes=None):
    QuoteEvent.objects.create(quote_request=quote, actor=actor, action=action, changes=changes or {})


def _notify(users, title, message):
    AppNotification.objects.bulk_create(AppNotification(user=user, title=title, message=message) for user in users)


def super_admins():
    return list(User.objects.filter(is_superuser=True, is_active=True))


def _jsonable(value):
    return str(value) if isinstance(value, Decimal) else value


# ── Quote figures ────────────────────────────────────────────────────────────

def _has_paid_order(quote):
    return bool(quote.order_id and quote.order.is_paid)


def _void_open_orders(quote):
    """Cancel every unpaid payment-attempt Order of a request (no signals; nothing was captured).

    A payment that still completes on one of them later is caught by the handler and flagged
    for a manual refund, so closing the order here never loses money silently.
    """
    open_order_ids = list(
        quote.payment_attempts.filter(order__is_paid=False).exclude(order__status=OrderStatus.CANCELLED)
        .values_list('order_id', flat=True)
    )
    if open_order_ids:
        Order.objects.filter(pk__in=open_order_ids).update(status=OrderStatus.CANCELLED, updated_at=timezone.now())


PRICE_FIELDS = ('item_price', 'shipping_fee')


def update_quote_details(quote, actor, details, actor_is_admin):
    """Apply validated quote figures.

    Sellers may edit only before the pay request is sent. The admin may also correct a sent quote
    until it is paid: if the price changes, any open payment window is closed, the customer is told
    and given a fresh pay-by date.
    """
    reprice_sent_quote = actor_is_admin and quote.status == QuoteStatus.AWAITING_PAYMENT and not _has_paid_order(quote)
    editable = quote.status in EDITABLE_QUOTE_STATUSES or reprice_sent_quote
    if not editable:
        raise QuoteError('This quote can no longer be edited.')
    changes = {}
    update_fields = []
    for field, value in details.items():
        if field == 'admin_notes' and not actor_is_admin:
            continue
        if getattr(quote, field) != value:
            changes[field] = {'from': _jsonable(getattr(quote, field)), 'to': _jsonable(value)}
            setattr(quote, field, value)
            update_fields.append(field)
    if not changes:
        return quote
    if quote.status == QuoteStatus.SUBMITTED:
        quote.status = QuoteStatus.IN_REVIEW
        update_fields.append('status')
    price_changed = any(field in changes for field in PRICE_FIELDS)
    if reprice_sent_quote and price_changed:
        _void_open_orders(quote)
        quote.order = None
        quote.pay_by = new_pay_by()
        quote.pay_by_reminder_sent_at = None
        update_fields += ['order', 'pay_by', 'pay_by_reminder_sent_at']
    quote.save(update_fields=update_fields + ['updated_at'])
    record_event(quote, actor, 'quote_edited', changes)
    if reprice_sent_quote and price_changed:
        if quote.user_id:
            _notify([quote.user], 'Your quote was updated',
                    f'The price for {quote.reference} is now ₹{quote.total_amount}. '
                    f'Pay from My Orders within {PAY_REQUEST_VALID_DAYS} days.')
        transaction.on_commit(lambda: _queue_email('send_pay_request_email_task', quote.id))
    if not actor_is_admin:
        _notify(super_admins(), f'Quote updated by seller: {quote.reference}',
                f'{actor.email} changed the quote for {quote.reference}. Review it before sending the pay request.')
    return quote


@transaction.atomic
def assign_seller(quote, admin, seller):
    # Reassigning is fine until money is taken; the shipment is created for whoever is assigned at payment.
    if quote.status not in OPEN_QUOTE_STATUSES or _has_paid_order(quote):
        raise QuoteError('A seller can no longer be assigned to this request.')
    previous = quote.assigned_seller_id
    quote.assigned_seller = seller
    if quote.status == QuoteStatus.SUBMITTED:
        quote.status = QuoteStatus.IN_REVIEW
    quote.save(update_fields=['assigned_seller', 'status', 'updated_at'])
    record_event(quote, admin, 'seller_assigned', {'from': str(previous) if previous else None, 'to': str(seller.id)})
    _notify([seller], f'New quote request: {quote.reference}',
            f'You have been assigned a custom order request ({quote.offering.name}). Review it and add the quote details.')
    return quote


@transaction.atomic
def send_pay_request(quote, admin):
    """Lock the figures and ask the customer to pay."""
    quote = QuoteRequest.objects.select_for_update().get(pk=quote.pk)
    if quote.status not in EDITABLE_QUOTE_STATUSES:
        raise QuoteError('The pay request can only be sent for an open request.')
    if quote.assigned_seller_id is None:
        raise QuoteError('Assign a seller before sending the pay request.')
    if quote.item_price is None or quote.item_price <= 0:
        raise QuoteError('Set the item price before sending the pay request.')
    quote.status = QuoteStatus.AWAITING_PAYMENT
    quote.quote_sent_at = timezone.now()
    quote.pay_by = new_pay_by()
    quote.pay_by_reminder_sent_at = None
    quote.commission_percent = quote.offering.commission_percent
    quote.save(update_fields=['status', 'quote_sent_at', 'pay_by', 'pay_by_reminder_sent_at', 'commission_percent', 'updated_at'])
    record_event(quote, admin, 'pay_request_sent', {'total_amount': str(quote.total_amount), 'pay_by': quote.pay_by.isoformat()})
    if quote.user_id:
        _notify([quote.user], 'Your quote is ready',
                f'Your quote {quote.reference} is ready: ₹{quote.total_amount}. '
                f'Pay from My Orders within {PAY_REQUEST_VALID_DAYS} days to confirm.')
    transaction.on_commit(lambda: _queue_email('send_pay_request_email_task', quote.id))
    return quote


@transaction.atomic
def decline_request(quote, admin, reason=''):
    quote = QuoteRequest.objects.select_for_update(of=('self',)).select_related('order').get(pk=quote.pk)
    if quote.status not in OPEN_QUOTE_STATUSES or _has_paid_order(quote):
        raise QuoteError('This request can no longer be declined.')
    _void_open_orders(quote)  # a payment window the customer opened is closed (a late payment is flagged for refund)
    quote.status = QuoteStatus.DECLINED
    quote.admin_notes = f'{quote.admin_notes}\nDeclined: {reason}'.strip() if reason else quote.admin_notes
    quote.save(update_fields=['status', 'admin_notes', 'updated_at'])
    record_event(quote, admin, 'declined', {'reason': reason})
    return quote


def notify_new_request(quote):
    _notify(super_admins(), f'New quote request: {quote.reference}',
            f'{quote.contact_name} asked for a quote on {quote.offering.name} '
            f'(budget ₹{quote.budget_min:,.0f}–₹{quote.budget_max:,.0f}).')
    transaction.on_commit(lambda: _queue_email('send_new_request_emails_task', quote.id))


def _queue_email(task_name, quote_id):
    from . import tasks

    try:
        getattr(tasks, task_name).delay(str(quote_id))
    except Exception:
        # Don't fail the request if the broker is down; the admin queue still shows it.
        logger.exception('Could not queue %s for request %s', task_name, quote_id)


# ── Pay-by deadline ──────────────────────────────────────────────────────────

def expire_overdue_pay_requests(now=None):
    """Move unpaid requests past their pay-by date to EXPIRED. Returns how many changed."""
    now = now or timezone.now()
    overdue = (QuoteRequest.objects.filter(status=QuoteStatus.AWAITING_PAYMENT, pay_by__lt=now)
               .exclude(order__is_paid=True).select_related('user'))
    expired = 0
    for quote in overdue:
        # Conditional update: a payment captured in the meantime wins.
        if not QuoteRequest.objects.filter(pk=quote.pk, status=QuoteStatus.AWAITING_PAYMENT).update(
                status=QuoteStatus.EXPIRED, updated_at=now):
            continue
        expired += 1
        record_event(quote, None, 'expired', {'pay_by': quote.pay_by.isoformat()})
        if quote.user_id:
            _notify([quote.user], 'Your quote has expired',
                    f'The payment window for {quote.reference} has closed. Call us and we can renew it.')
    return expired


@transaction.atomic
def renew_pay_request(quote, admin):
    """Give the customer another PAY_REQUEST_VALID_DAYS days to pay the same quote."""
    quote = QuoteRequest.objects.select_for_update().get(pk=quote.pk)
    if quote.status not in (QuoteStatus.EXPIRED, QuoteStatus.AWAITING_PAYMENT):
        raise QuoteError('Only an unpaid quote can be renewed.')
    quote.status = QuoteStatus.AWAITING_PAYMENT
    quote.pay_by = new_pay_by()
    quote.pay_by_reminder_sent_at = None
    quote.save(update_fields=['status', 'pay_by', 'pay_by_reminder_sent_at', 'updated_at'])
    record_event(quote, admin, 'pay_request_renewed', {'pay_by': quote.pay_by.isoformat()})
    if quote.user_id:
        _notify([quote.user], 'Your quote is open again',
                f'You can pay for {quote.reference} until {timezone.localtime(quote.pay_by):%d %b, %I:%M %p}.')
    transaction.on_commit(lambda: _queue_email('send_pay_request_email_task', quote.id))
    return quote


def send_pay_by_reminders(now=None):
    """Remind customers whose pay-by date is within PAY_BY_REMINDER_HOURS. One reminder per pay-by date."""
    now = now or timezone.now()
    due = (QuoteRequest.objects.filter(status=QuoteStatus.AWAITING_PAYMENT, pay_by__gt=now,
                                       pay_by__lte=now + timedelta(hours=PAY_BY_REMINDER_HOURS),
                                       pay_by_reminder_sent_at__isnull=True)
           .exclude(order__is_paid=True).select_related('user'))
    sent = 0
    for quote in due:
        # Conditional update: two workers never send the same reminder twice.
        if not QuoteRequest.objects.filter(pk=quote.pk, pay_by_reminder_sent_at__isnull=True).update(
                pay_by_reminder_sent_at=now):
            continue
        sent += 1
        record_event(quote, None, 'pay_by_reminder', {'pay_by': quote.pay_by.isoformat()})
        if quote.user_id:
            _notify([quote.user], 'Your quote expires soon',
                    f'Pay for {quote.reference} by {timezone.localtime(quote.pay_by):%d %b, %I:%M %p} to keep this quote.')
        _queue_email('send_pay_by_reminder_email_task', quote.id)
    return sent


# ── Customer cancellation ────────────────────────────────────────────────────


@transaction.atomic
def customer_cancel(quote, user, reason=''):
    """A customer cancels their own request before paying. Once paid, only the admin can cancel."""
    quote = (QuoteRequest.objects.select_for_update(of=('self',))
             .select_related('assigned_seller', 'user', 'order').get(pk=quote.pk))
    reason = ' '.join((reason or '').split())[:500]
    if quote.status in UNPAID_STATUSES:
        _void_open_orders(quote)
        quote.status = QuoteStatus.CANCELLED
        quote.cancel_reason = reason or 'Cancelled by the customer'
        quote.save(update_fields=['status', 'cancel_reason', 'updated_at'])
        record_event(quote, user, 'cancelled_by_customer', {'reason': reason})
        _notify(super_admins(), f'Customer cancelled: {quote.reference}',
                f'{quote.contact_name} cancelled the request before paying.' + (f' Reason: {reason}' if reason else ''))
        if quote.assigned_seller_id:
            _notify([quote.assigned_seller], f'Request cancelled: {quote.reference}', 'The customer cancelled before paying.')
        return 'cancelled'
    raise QuoteError('A paid order cannot be cancelled here. Please call us and we will help.')


# ── Cancellation (refund is done manually by the admin) ─────────────────────

CANCELLABLE_STATUSES = (QuoteStatus.AWAITING_PAYMENT, QuoteStatus.EXPIRED, QuoteStatus.PAID)


def amount_paid(quote):
    """What the customer has paid for this request so far (first payment + paid extras)."""
    paid = Decimal('0')
    if quote.order_id and quote.order.is_paid:
        paid += quote.order.total_amount
    for charge in quote.additional_charges.all():
        if charge.status == ChargeStatus.PAID:
            paid += charge.amount
    return paid


@transaction.atomic
def cancel_order(quote, admin, reason=''):
    """Cancel a quoted or paid custom order. Stops the seller's shipment; money is refunded by hand."""
    quote = (QuoteRequest.objects.select_for_update(of=('self',)).select_related('order', 'assigned_seller', 'user')
             .get(pk=quote.pk))
    if quote.status not in CANCELLABLE_STATUSES:
        raise QuoteError('This request can no longer be cancelled.')
    sub_orders = list(SubOrder.objects.filter(order_id=quote.order_id)) if quote.order_id else []
    if any(sub.status == 'delivered' for sub in sub_orders):
        raise QuoteError('This order was already delivered.')
    refund_due = amount_paid(quote)
    was_paid = quote.status == QuoteStatus.PAID

    quote.status = QuoteStatus.CANCELLED
    quote.cancel_reason = reason
    quote.save(update_fields=['status', 'cancel_reason', 'updated_at'])
    quote.additional_charges.filter(status=ChargeStatus.REQUESTED).update(status=ChargeStatus.CANCELLED)
    if sub_orders:
        # Plain updates on purpose: no fulfilment or payment signals should react to a manual cancellation.
        SubOrder.objects.filter(pk__in=[s.pk for s in sub_orders]).update(status='cancelled', updated_at=timezone.now())
        Order.objects.filter(pk=quote.order_id).update(status=OrderStatus.CANCELLED, updated_at=timezone.now())
        for sub in sub_orders:
            settlement = SellerSettlement.objects.filter(sub_order=sub).first()
            if settlement and not settlement.is_settled:
                settlement.delete()
            elif settlement:
                _notify(super_admins(), f'Recover seller payout: {quote.reference}',
                        f'{quote.reference} was cancelled after the seller was paid ₹{settlement.amount}.')
    record_event(quote, admin, 'cancelled', {'reason': reason, 'refund_due': str(refund_due)})

    if quote.user_id:
        refund_note = f' Your payment of ₹{refund_due} will be refunded.' if refund_due else ''
        _notify([quote.user], 'Your custom order was cancelled', f'{quote.reference} has been cancelled.{refund_note}')
    if was_paid and quote.assigned_seller_id:
        _notify([quote.assigned_seller], f'Custom order cancelled: {quote.reference}', 'Please do not ship this order.')
    return quote, refund_due


# ── Payment ──────────────────────────────────────────────────────────────────

def _order_number_for(preferred):
    """The quote's own JNG reference (or `<reference>-E<n>` for an extra payment), unless an
    order already took it, in which case a fresh JNG number."""
    if not Order.all_objects.filter(order_number=preferred).exists():
        return preferred
    return unique_order_number()


def _commission_split(quote):
    """(platform commission, seller payout) on the item price; shipping goes to the seller."""
    commission = (quote.item_price * quote.commission_percent / HUNDRED).quantize(PAISE)
    return commission, quote.item_price - commission + quote.shipping_fee


@transaction.atomic
def prepare_order_for_payment(quote_id, user, shipping_address):
    """Create (or refresh) the unpaid Order + seller SubOrder for an awaiting-payment quote.

    The amount always comes from the stored quote, never from the request.
    """
    # of=('self',): Postgres can't row-lock the nullable side of the assigned_seller outer join.
    quote = (QuoteRequest.objects.select_for_update(of=('self',))
             .select_related('offering', 'assigned_seller').get(pk=quote_id))
    if quote.status == QuoteStatus.EXPIRED:
        raise QuoteExpired('This quote has expired. Please call us and we will renew it.')
    if quote.status != QuoteStatus.AWAITING_PAYMENT:
        raise QuoteError('This quote is not waiting for payment.')
    if quote.pay_by and quote.pay_by < timezone.now():
        raise QuoteExpired('This quote has expired. Please call us and we will renew it.')
    owned = quote.user_id == user.id or (quote.user_id is None and user.email.lower() == quote.contact_email)
    if not owned:
        raise QuoteError('This quote belongs to another account.')

    current = quote.order
    if current is not None and current.is_paid:
        raise QuoteError('This quote is already paid.')
    if current is not None and current.status == OrderStatus.PENDING and not _gateway_started(current):
        current.shipping_address = shipping_address
        current.save(update_fields=['shipping_address', 'updated_at'])
        return quote, current

    attempt_number = quote.payment_attempts.filter(charge__isnull=True).count() + 1
    preferred = quote.reference if attempt_number == 1 else f'{quote.reference}-{attempt_number}'
    order = Order.objects.create(
        order_number=_order_number_for(preferred), order_type=OrderType.SERVICE, user=user,
        shipping_address=shipping_address, status=OrderStatus.PENDING, subtotal=quote.item_price,
        shipping_fee=quote.shipping_fee, total_amount=quote.total_amount,
    )
    OrderItem.objects.create(
        order=order, product=None, variant=None, product_name=quote.offering.name,
        variant_name=quote.reference, unit_price=quote.item_price, gst_percentage=Decimal('0'), quantity=1,
        seller=quote.assigned_seller,
    )
    QuotePaymentAttempt.objects.create(order=order, quote_request=quote)
    quote.order = order
    quote.user = user
    quote.save(update_fields=['order', 'user', 'updated_at'])
    return quote, order


def _gateway_started(order):
    """True once a gateway order exists for this Order (one Payment per Order is allowed)."""
    return Payment.all_objects.filter(order=order).exists()


def _create_seller_sub_order(quote, order):
    """The seller's shipment, created only once the money is in."""
    if order.sub_orders.exists():
        return order.sub_orders.first()
    _, seller_payout = _commission_split(quote)
    sub_order = SubOrder.objects.create(
        order=order, sub_order_number=f'{order.order_number}-A', seller=quote.assigned_seller,
        subtotal=quote.item_price, shipping_fee=quote.shipping_fee, seller_total=seller_payout,
        status='placed',
    )
    # The line item names the seller assigned *now*, which may differ from when the order was opened.
    order.items.update(sub_order=sub_order, seller=quote.assigned_seller)
    return sub_order


# ── Additional payments ──────────────────────────────────────────────────────

@transaction.atomic
def request_additional_charge(quote, actor, description, amount):
    """Ask the customer for more money after the first payment. Admin or the assigned seller."""
    if quote.status != QuoteStatus.PAID:
        raise QuoteError('An additional payment can only be requested after the first payment.')
    commission = (amount * quote.commission_percent / HUNDRED).quantize(PAISE)
    charge = AdditionalCharge.objects.create(
        quote_request=quote, description=description, amount=amount, requested_by=actor,
        commission_percent=quote.commission_percent, seller_payout=amount - commission,
    )
    record_event(quote, actor, 'additional_requested', {'description': description, 'amount': str(amount)})
    if quote.user_id:
        _notify([quote.user], 'Additional payment requested',
                f'{description}: ₹{amount} for {quote.reference}. Pay from My Orders.')
    if actor.id != quote.assigned_seller_id:
        _notify([quote.assigned_seller], f'Additional payment requested: {quote.reference}', f'{description}: ₹{amount}.')
    if not actor.is_superuser:
        _notify(super_admins(), f'Additional payment requested: {quote.reference}',
                f'{actor.email} asked the customer for ₹{amount} ({description}).')
    transaction.on_commit(lambda: _queue_email('send_additional_charge_email_task', charge.id))
    return charge


@transaction.atomic
def cancel_additional_charge(charge, actor):
    charge = AdditionalCharge.objects.select_for_update(of=('self',)).select_related('order', 'quote_request').get(pk=charge.pk)
    if charge.status != ChargeStatus.REQUESTED:
        raise QuoteError('Only an unpaid request can be cancelled.')
    if charge.order_id and charge.order.is_paid:
        raise QuoteError('This payment is already being processed.')
    charge.status = ChargeStatus.CANCELLED
    charge.save(update_fields=['status'])
    record_event(charge.quote_request, actor, 'additional_cancelled', {'description': charge.description})
    return charge


@transaction.atomic
def prepare_charge_order(charge_id, user):
    """Create (or reuse) the unpaid Order for an additional charge. The amount comes from the stored charge."""
    charge = (AdditionalCharge.objects.select_for_update(of=('self',))
              .select_related('quote_request__order', 'quote_request__assigned_seller').get(pk=charge_id))
    quote = charge.quote_request
    if charge.status != ChargeStatus.REQUESTED:
        raise QuoteError('This payment request is no longer open.')
    if quote.user_id != user.id:
        raise QuoteError('This payment belongs to another account.')
    if charge.order_id:
        if charge.order.is_paid:
            raise QuoteError('This payment is already made.')
        if charge.order.status == OrderStatus.PENDING and not _gateway_started(charge.order):
            return charge, charge.order
    extra_number = quote.payment_attempts.filter(charge__isnull=False).count() + 1
    order = Order.objects.create(
        order_number=_order_number_for(f'{quote.reference}-E{extra_number}'),
        order_type=OrderType.SERVICE, user=user,
        shipping_address=quote.order.shipping_address, status=OrderStatus.PENDING,
        subtotal=charge.amount, shipping_fee=Decimal('0'), total_amount=charge.amount,
    )
    OrderItem.objects.create(
        order=order, product=None, variant=None, product_name=f'Additional payment: {charge.description}'[:255],
        variant_name=quote.reference, unit_price=charge.amount, gst_percentage=Decimal('0'), quantity=1,
        seller=quote.assigned_seller,
    )
    QuotePaymentAttempt.objects.create(order=order, quote_request=quote, charge=charge)
    charge.order = order
    charge.save(update_fields=['order'])
    return charge, order


def _credit_seller_for_charge(charge):
    """Add the seller's share to the original sub-order, and to its settlement when one exists.

    Returns the PayoutStatus: MANUAL_DUE when the settlement was already paid out, so the admin
    sees "pay the seller separately" on the request until it is recorded.
    """
    quote = charge.quote_request
    sub_order = quote.order.sub_orders.first() if quote.order_id else None
    if sub_order is None:
        return PayoutStatus.CREDITED
    sub_order.seller_total += charge.seller_payout
    sub_order.subtotal += charge.amount
    sub_order.save(update_fields=['seller_total', 'subtotal', 'updated_at'])
    settlement = getattr(sub_order, 'settlement', None)
    if settlement is None:
        return PayoutStatus.CREDITED
    if settlement.is_settled:
        _notify(super_admins(), f'Pay the seller extra: {quote.reference}',
                f'₹{charge.seller_payout} for "{charge.description}" arrived after the settlement was paid. Transfer it separately.')
        return PayoutStatus.MANUAL_DUE
    settlement.amount += charge.seller_payout
    settlement.save(update_fields=['amount'])
    return PayoutStatus.CREDITED


@transaction.atomic
def record_extra_payout(charge, admin):
    """The admin paid the seller the extra amount by hand; record it so it stops being flagged."""
    charge = AdditionalCharge.objects.select_for_update(of=('self',)).select_related('quote_request').get(pk=charge.pk)
    if charge.payout_status != PayoutStatus.MANUAL_DUE:
        raise QuoteError('This extra payment has no separate seller payout waiting.')
    charge.payout_status = PayoutStatus.MANUAL_DONE
    charge.save(update_fields=['payout_status'])
    record_event(charge.quote_request, admin, 'extra_payout_recorded',
                 {'description': charge.description, 'amount': str(charge.seller_payout)})
    return charge


def _handle_charge_paid(charge, order):
    quote = charge.quote_request
    if charge.status == ChargeStatus.PAID:
        if charge.order_id != order.id:
            _refund_alert(quote, order, f'a second payment for "{charge.description}"')
        return
    if quote.status == QuoteStatus.CANCELLED or charge.status == ChargeStatus.CANCELLED:
        _refund_alert(quote, order, f'"{charge.description}" after it was cancelled')
        return
    charge.status = ChargeStatus.PAID
    charge.paid_at = timezone.now()
    charge.order = order
    charge.payout_status = _credit_seller_for_charge(charge)
    charge.save(update_fields=['status', 'paid_at', 'order', 'payout_status'])
    record_event(quote, None, 'additional_paid', {'description': charge.description, 'amount': str(charge.amount)})
    _notify([quote.assigned_seller], f'Additional payment received: {quote.reference}', f'₹{charge.amount} for "{charge.description}".')
    _notify(super_admins(), f'Additional payment received: {quote.reference}',
            f'{quote.contact_name} paid ₹{charge.amount} for "{charge.description}".')


# ── Delivery ─────────────────────────────────────────────────────────────────

@transaction.atomic
def mark_delivered(quote, actor):
    """Admin marks a paid custom order delivered (e.g. hand delivery outside the courier flow).

    Moves the seller's sub-order to delivered and opens its payout settlement, exactly as
    the seller fulfilment flow does; the sub-order signal then records the quote as delivered.
    """
    quote = QuoteRequest.objects.select_for_update(of=('self',)).select_related('order').get(pk=quote.pk)
    if quote.status != QuoteStatus.PAID:
        raise QuoteError('Only a paid order can be marked delivered.')
    for sub_order in SubOrder.objects.filter(order_id=quote.order_id).exclude(status='delivered'):
        sub_order.status = 'delivered'
        sub_order.save(update_fields=['status', 'updated_at'])
        SellerSettlement.objects.get_or_create(
            sub_order=sub_order, defaults={'seller': sub_order.seller, 'amount': sub_order.seller_total},
        )
    record_quote_delivered(quote.pk, actor)
    return quote


def record_quote_delivered(quote_id, actor=None):
    """Mark the request delivered once its sub-order is delivered. Idempotent."""
    updated = QuoteRequest.objects.filter(pk=quote_id, status=QuoteStatus.PAID).update(
        status=QuoteStatus.DELIVERED, updated_at=timezone.now(),
    )
    if not updated:
        return
    quote = QuoteRequest.objects.select_related('user').get(pk=quote_id)
    record_event(quote, actor, 'delivered')
    if quote.user_id:
        _notify([quote.user], 'Your custom order was delivered', f'{quote.reference} has been delivered. Enjoy!')


def _refund_alert(quote, order, what):
    """Money arrived that should not have (duplicate or after closing): the admin refunds it by hand."""
    record_event(quote, None, 'refund_needed', {'order_number': order.order_number, 'amount': str(order.total_amount)})
    _notify(super_admins(), f'Refund needed: {quote.reference}',
            f'{quote.contact_name} made {what} of ₹{order.total_amount} (order {order.order_number}). Refund it manually.')


class ServiceOrderHandler(OrderTypeHandler):
    order_select_related = ('service_quote__offering', 'additional_charge__quote_request')

    def summarize(self, order, public=False):
        charge = getattr(order, 'additional_charge', None)
        if charge is not None:
            return {'type': 'service', 'id': str(charge.id), 'reference': charge.quote_request.reference,
                    'status': charge.status, 'status_label': charge.get_status_display(),
                    'title': 'Additional payment', 'description': charge.description}
        quote = getattr(order, 'service_quote', None)
        if quote is None:
            return None
        summary = {
            'type': 'service',
            'id': str(quote.id),
            'reference': quote.reference,
            'status': quote.status,
            'status_label': quote.get_status_display(),
            'title': quote.offering.name,
            'shape': quote.shape,
            'inclusions': quote.inclusions,
        }
        if public:
            summary.pop('inclusions')
        return summary

    def on_paid(self, order):
        attempt = (QuotePaymentAttempt.objects
                   .select_related('charge__quote_request__assigned_seller', 'charge__quote_request__order',
                                   'quote_request__assigned_seller', 'quote_request__offering')
                   .filter(order=order).first())
        if attempt is None:
            return
        if attempt.charge_id:
            _handle_charge_paid(attempt.charge, order)
            return
        with transaction.atomic():
            quote = QuoteRequest.objects.select_for_update(of=('self',)).select_related(
                'assigned_seller', 'offering').get(pk=attempt.quote_request_id)
            self._first_payment_captured(quote, order)

    @staticmethod
    def _first_payment_captured(quote, order):
        """Move the request to PAID exactly once. Later saves of a paid order change nothing."""
        if quote.status in (QuoteStatus.PAID, QuoteStatus.DELIVERED):
            if quote.order_id != order.id:
                _refund_alert(quote, order, 'a second payment')
            return
        if quote.status in (QuoteStatus.CANCELLED, QuoteStatus.DECLINED):
            Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED, updated_at=timezone.now())
            _refund_alert(quote, order, 'a payment after the request was closed')
            return
        if order.total_amount != quote.total_amount:
            # The price changed after this payment window opened: money for the old amount arrived.
            Order.objects.filter(pk=order.pk).update(status=OrderStatus.CANCELLED, updated_at=timezone.now())
            _refund_alert(quote, order, 'a payment for an older price')
            return
        # AWAITING_PAYMENT, or EXPIRED when the customer started paying before the deadline.
        quote.status = QuoteStatus.PAID
        quote.paid_at = timezone.now()
        quote.order = order
        quote.save(update_fields=['status', 'paid_at', 'order', 'updated_at'])
        _create_seller_sub_order(quote, order)
        record_event(quote, None, 'paid', {'order_number': order.order_number})
        _notify([quote.assigned_seller], f'Custom order paid: {quote.reference}',
                f'Order {order.order_number} is paid. Prepare and ship it from your orders.')
        _notify(super_admins(), f'Custom order paid: {quote.reference}',
                f'{quote.contact_name} paid ₹{order.total_amount} for {quote.offering.name}.')

    def on_failed(self, order):
        """Payment failed: the quote stays awaiting payment so the customer can retry."""
        attempt = QuotePaymentAttempt.objects.select_related('quote_request').filter(order=order).first()
        if attempt and attempt.quote_request.status == QuoteStatus.AWAITING_PAYMENT:
            record_event(attempt.quote_request, None, 'payment_failed', {'order_number': order.order_number})
