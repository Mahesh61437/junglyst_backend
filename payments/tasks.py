"""
payments/tasks.py

On-demand payment reconciliation using django-celery-beat PeriodicTask.

When a Payment is created at checkout, we create 4 ClockedSchedule entries
in the database, each linked to a PeriodicTask that fires ONCE:

    T+15min  → check_payment_status(payment_id)
    T+30min  → check_payment_status(payment_id)
    T+1hr    → check_payment_status(payment_id)
    T+24hr   → check_payment_status(payment_id)

Advantages over apply_async(countdown=...):
  - Tasks are visible in Django Admin (Periodic Tasks section)
  - They can be filtered by payment ID, disabled, or deleted
  - When a payment is resolved, cancel_payment_checks() deletes all
    remaining tasks — they never execute at all (not even briefly)
  - Survives worker restarts (persisted in DB, not in-memory)
"""

import json
import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

# Delays for the 4 scheduled checks
RECONCILE_CHECKS = [
    (15,    '15min'),
    (30,    '30min'),
    (60,    '1hr'),
    (1440,  '24hr'),   # 24 * 60
]

TASK_NAME_PREFIX = 'payment-check'


def _task_name(payment_id, label):
    """Generate a unique, filterable name for a scheduled task."""
    return f"{TASK_NAME_PREFIX}-{payment_id}-{label}"


# ── Schedule / Cancel helpers ────────────────────────────────────────────────

def schedule_payment_checks(payment_id):
    """
    Create 4 one-shot PeriodicTask entries in django-celery-beat.
    Call this right after Payment.objects.create() in the checkout view.

    Each task uses ClockedSchedule (fires once at a specific datetime).
    """
    from django_celery_beat.models import ClockedSchedule, PeriodicTask

    now = timezone.now()

    for delay_minutes, label in RECONCILE_CHECKS:
        fire_at = now + timedelta(minutes=delay_minutes)

        clocked, _ = ClockedSchedule.objects.get_or_create(
            clocked_time=fire_at,
        )

        task_name = _task_name(payment_id, label)

        # Avoid duplicates if checkout is retried
        PeriodicTask.objects.update_or_create(
            name=task_name,
            defaults={
                'task': 'payments.tasks.check_payment_status',
                'clocked': clocked,
                'one_off': True,       # auto-disables after first run
                'enabled': True,
                'args': json.dumps([payment_id]),
                'kwargs': json.dumps({'label': label}),
            },
        )

    logger.info(
        "Scheduled %d reconcile checks for payment %s (at %s)",
        len(RECONCILE_CHECKS), payment_id,
        ", ".join(label for _, label in RECONCILE_CHECKS),
    )


def cancel_payment_checks(payment_id):
    """
    Delete all remaining scheduled tasks for a resolved payment.
    Call this after a payment is captured or gateway-confirmed failed.
    """
    from django_celery_beat.models import PeriodicTask

    prefix = f"{TASK_NAME_PREFIX}-{payment_id}-"
    deleted_count, _ = PeriodicTask.objects.filter(
        name__startswith=prefix,
        enabled=True,
    ).delete()

    if deleted_count:
        logger.info(
            "Cancelled %d remaining scheduled checks for payment %s",
            deleted_count, payment_id,
        )


# ── Gateway status helpers ───────────────────────────────────────────────────

def _cashfree_gateway_status(payment):
    """
    Query Cashfree payments list endpoint for a definitive status.

    Returns:
      ('captured', cf_payment_id, payment_data)  — bank confirmed SUCCESS
      ('failed',   None,          payment_data)   — bank confirmed FAILURE
      ('pending',  None,          None)            — still in-flight
    """
    import requests as req
    from payments.cashfree_utils import get_cashfree_url, get_cashfree_headers

    FINAL_FAILED = {"FAILED", "CANCELLED", "VOID", "NOT_ATTEMPTED", "USER_DROPPED"}

    try:
        url = f"{get_cashfree_url()}/{payment.cashfree_order_id}/payments"
        r = req.get(url, headers=get_cashfree_headers(), timeout=15)
        if r.status_code != 200:
            logger.warning(
                "Cashfree payments fetch %s: HTTP %s",
                payment.cashfree_order_id, r.status_code,
            )
            return 'pending', None, None

        payments_list = r.json() if isinstance(r.json(), list) else []
        for p in payments_list:
            pstatus = (p.get("payment_status") or "").upper()
            if pstatus == "SUCCESS":
                return 'captured', p.get("cf_payment_id"), p
            if pstatus in FINAL_FAILED:
                return 'failed', None, p

        return 'pending', None, None

    except Exception as exc:
        logger.warning(
            "Cashfree reconcile error for %s: %s",
            payment.cashfree_order_id, exc,
        )
        return 'pending', None, None


def _razorpay_gateway_status(payment):
    """
    Query Razorpay orders/{id}/payments for a definitive status.
    Returns same tuple shape as _cashfree_gateway_status.
    """
    import requests as req
    from payments.razorpay_utils import _auth

    FINAL_FAILED = {"failed"}

    try:
        key_id, key_secret = _auth()
        url = f"https://api.razorpay.com/v1/orders/{payment.razorpay_order_id}/payments"
        r = req.get(url, auth=(key_id, key_secret), timeout=15)
        if r.status_code != 200:
            logger.warning(
                "Razorpay payments fetch %s: HTTP %s",
                payment.razorpay_order_id, r.status_code,
            )
            return 'pending', None, None

        for p in r.json().get('items', []):
            pstatus = p.get('status', '').lower()
            if pstatus == 'captured':
                return 'captured', p.get('id'), p
            if pstatus in FINAL_FAILED:
                return 'failed', None, p

        return 'pending', None, None

    except Exception as exc:
        logger.warning(
            "Razorpay reconcile error for %s: %s",
            payment.razorpay_order_id, exc,
        )
        return 'pending', None, None


# ── Shared capture helper ────────────────────────────────────────────────────

@transaction.atomic
def _capture_payment(payment, gateway_payment_id=None, gateway_data=None):
    """
    Mark payment + order as captured; deduct stock; notify buyer.
    Idempotent — safe to call multiple times.
    """
    from orders.tasks import send_order_confirmation_emails_task, create_order_notifications_task, clear_buyer_cart_task

    order = payment.order

    # Already captured → nothing to do
    if payment.status == 'captured':
        logger.info("_capture_payment: payment %s already captured, skipping.", payment.id)
        return

    # Final stock check
    insufficient = []
    for item in order.items.select_related('variant').all():
        if item.variant:
            variant = item.variant.__class__.objects.select_for_update().get(pk=item.variant.pk)
            if variant.stock < item.quantity:
                insufficient.append(item.product_name)

    if insufficient:
        payment.status = 'failed'
        payment.error_message = f"Stock mismatch: {', '.join(insufficient)}"
        payment.save(update_fields=['status', 'error_message'])
        order.status = 'failed'
        order.payment_status = 'failed'
        order.save(update_fields=['status', 'payment_status', 'updated_at'])
        logger.error(
            "Reconcile: stock mismatch for order %s — %s",
            order.order_number, insufficient,
        )
        # Cancel remaining checks — payment is resolved (failed)
        cancel_payment_checks(payment.id)
        return

    # Write gateway payment ID
    if gateway_payment_id:
        if payment.gateway == 'cashfree':
            payment.cashfree_payment_id = gateway_payment_id
        else:
            payment.razorpay_payment_id = gateway_payment_id

    # Store gateway response for dispute tracking
    if gateway_data:
        payment.gateway_response = gateway_data
        payment.bank_reference = (
            gateway_data.get('bank_reference', '')
            or gateway_data.get('utr', '')
            or gateway_data.get('acquirer_data', {}).get('upi_transaction_id', '')
        )

    payment.status = 'captured'
    payment.gateway_status = 'SUCCESS' if payment.gateway == 'cashfree' else 'captured'
    payment.paid_at = timezone.now()
    payment.save()

    # Reset order — may have been prematurely marked 'failed'
    order.is_paid = True
    order.payment_status = 'completed'
    order.status = 'placed'
    order.save()

    # Deduct stock atomically
    for item in order.items.select_related('variant').all():
        if item.variant:
            item.variant.__class__.objects.filter(pk=item.variant.pk).update(
                stock=F('stock') - item.quantity
            )

    # Mark sub-orders as placed
    for sub in order.sub_orders.all():
        if sub.status not in ('placed', 'confirmed', 'shipped', 'delivered'):
            sub.status = 'placed'
            sub.save(update_fields=['status', 'updated_at'])

    order_number = order.order_number

    # Fire async tasks — notifications, emails, cart clearing are non-blocking
    create_order_notifications_task.delay(str(order.id))
    send_order_confirmation_emails_task.delay(str(order.id))
    if order.user_id:
        clear_buyer_cart_task.delay(str(order.user_id))

    logger.info("Reconcile: captured order %s via %s", order_number, payment.gateway)

    # Cancel remaining scheduled checks — payment is done
    cancel_payment_checks(payment.id)


# ── Main on-demand reconcile task ────────────────────────────────────────────

@shared_task(
    name='payments.tasks.check_payment_status',
    bind=True,
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
)
def check_payment_status(self, payment_id, label=''):
    """
    Check the status of a single payment against the gateway API.

    Scheduled via django-celery-beat ClockedSchedule at:
      T+15min, T+30min, T+1hr, T+24hr

    Each invocation:
      1. Checks if payment is already resolved → exits + cancels remaining tasks
      2. If still pending → queries gateway API
      3. If captured/failed → processes + cancels remaining tasks
      4. If still pending at 24hr → hard-expires + cancels remaining tasks
    """
    from payments.models import Payment

    HARD_EXPIRE_SECONDS = 86400  # 24 hours

    try:
        payment = Payment.objects.select_related('order').get(id=payment_id)
    except Payment.DoesNotExist:
        logger.warning("check_payment_status[%s]: payment %s not found.", label, payment_id)
        cancel_payment_checks(payment_id)
        return f"Payment {payment_id} not found."

    # ── Fast exit: already resolved → cancel remaining tasks ────────────
    if payment.status == 'captured':
        logger.info("check_payment_status[%s]: payment %s already captured.", label, payment_id)
        cancel_payment_checks(payment_id)
        return f"Payment {payment_id} already captured — cancelled remaining checks."

    if (payment.status == 'failed'
            and payment.order.status == 'failed'
            and payment.gateway_status in ('FAILED', 'CANCELLED', 'VOID')):
        logger.info("check_payment_status[%s]: payment %s already gateway-failed.", label, payment_id)
        cancel_payment_checks(payment_id)
        return f"Payment {payment_id} already failed — cancelled remaining checks."

    # ── Query the gateway API ───────────────────────────────────────────
    if payment.gateway == 'cashfree' and payment.cashfree_order_id:
        result, gw_id, gw_data = _cashfree_gateway_status(payment)
    elif payment.gateway == 'razorpay' and payment.razorpay_order_id:
        result, gw_id, gw_data = _razorpay_gateway_status(payment)
    else:
        logger.warning("check_payment_status[%s]: payment %s has no gateway ID.", label, payment_id)
        return f"Payment {payment_id} has no gateway ID."

    # ── Handle result ───────────────────────────────────────────────────
    if result == 'captured':
        _capture_payment(payment, gw_id, gw_data)
        # cancel_payment_checks is called inside _capture_payment
        return f"Payment {payment_id} captured at {label} check."

    if result == 'failed':
        with transaction.atomic():
            payment.status = 'failed'
            payment.gateway_status = (gw_data or {}).get('payment_status', 'FAILED')
            error_details = (gw_data or {}).get('error_details') or {}
            payment.error_code = error_details.get('error_code', '')
            payment.error_message = error_details.get('error_description', '')
            payment.gateway_response = gw_data
            payment.save()
            order = payment.order
            if order.status not in ('placed', 'shipped', 'delivered'):
                order.status = 'failed'
                order.payment_status = 'failed'
                order.save(update_fields=['status', 'payment_status', 'updated_at'])
        cancel_payment_checks(payment_id)
        logger.info(
            "check_payment_status[%s]: gateway failure for payment %s (order %s)",
            label, payment_id, payment.order.order_number,
        )
        return f"Payment {payment_id} failed at {label} check — cancelled remaining."

    # ── Still pending — hard-expire if 24h reached ──────────────────────
    age_seconds = (timezone.now() - payment.created_at).total_seconds()
    if age_seconds >= HARD_EXPIRE_SECONDS:
        with transaction.atomic():
            payment.status = 'failed'
            payment.error_message = 'Hard-expired after 24 hours — no gateway response.'
            payment.save(update_fields=['status', 'error_message'])
            order = payment.order
            if order.status not in ('placed', 'shipped', 'delivered'):
                order.status = 'failed'
                order.payment_status = 'failed'
                order.save(update_fields=['status', 'payment_status', 'updated_at'])
        cancel_payment_checks(payment_id)
        logger.warning(
            "check_payment_status[%s]: hard-expired payment %s (order %s).",
            label, payment_id, payment.order.order_number,
        )
        return f"Payment {payment_id} hard-expired at {label} check."

    # Still within window — next scheduled check will handle it
    logger.info(
        "check_payment_status[%s]: payment %s still pending (age=%dm).",
        label, payment_id, int(age_seconds // 60),
    )
    return f"Payment {payment_id} still pending at {label} check (age={int(age_seconds // 60)}min)."
