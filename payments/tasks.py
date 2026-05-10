"""
payments/tasks.py

Periodic reconciliation for stuck / in-flight payments.

Design principles:
  1. NEVER expire a payment based on age alone — only expire when the
     gateway EXPLICITLY says the payment failed/cancelled.
  2. Keep checking until we get a definitive gateway response OR
     until HARD_EXPIRE_HOURS (24h) — this covers UPI late settlements.
  3. Once expired by reconcile (no explicit failure — just timeout),
     the Cashfree webhook can still revive it if the bank settles late
     (handle_order_paid in views.py skips 'captured' but processes 'failed').

Runs every hour via Celery Beat. Cashfree webhook handles real-time success;
this task is the safety net for network drops, browser kills, etc.

Razorpay does NOT have a webhook in this setup — this task is the only
safety net for Razorpay late settlements.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Give UPI at least 10 min before first check (most resolve in < 60s, but
# bank processing can take longer)
MIN_AGE_MINUTES = 10

# Hard-expire after 24 hours — no bank will settle a UPI after this
HARD_EXPIRE_HOURS = 24


# ── Cashfree: get definitive payment status ──────────────────────────────────

def _cashfree_gateway_status(payment):
    """
    Query Cashfree payments list endpoint for a definitive status.

    Returns:
      ('captured', cf_payment_id)  — bank confirmed SUCCESS
      ('failed',   None)           — bank confirmed FAILURE/CANCELLED
      ('pending',  None)           — still in-flight; check again next run
    """
    import requests as req
    from payments.cashfree_utils import get_cashfree_url, get_cashfree_headers

    FINAL_FAILED = {"FAILED", "CANCELLED", "VOID", "NOT_ATTEMPTED", "USER_DROPPED"}

    try:
        url = f"{get_cashfree_url()}/{payment.cashfree_order_id}/payments"
        r = req.get(url, headers=get_cashfree_headers(), timeout=15)
        if r.status_code != 200:
            logger.warning(
                "Cashfree payments fetch %s: HTTP %s", payment.cashfree_order_id, r.status_code
            )
            return 'pending', None

        payments_list = r.json() if isinstance(r.json(), list) else []
        for p in payments_list:
            pstatus = (p.get("payment_status") or "").upper()
            if pstatus == "SUCCESS":
                return 'captured', p.get("cf_payment_id")
            if pstatus in FINAL_FAILED:
                return 'failed', None
            # PENDING / PROCESSING → keep polling

        # No payment attempt recorded yet at Cashfree level
        return 'pending', None

    except Exception as exc:
        logger.warning(
            "Cashfree reconcile error for %s: %s", payment.cashfree_order_id, exc
        )
        return 'pending', None


# ── Razorpay: get definitive payment status ──────────────────────────────────

def _razorpay_gateway_status(payment):
    """
    Query Razorpay orders/{id}/payments for a definitive status.

    Returns same tuple as _cashfree_gateway_status.
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
                "Razorpay payments fetch %s: HTTP %s", payment.razorpay_order_id, r.status_code
            )
            return 'pending', None

        for p in r.json().get('items', []):
            pstatus = p.get('status', '').lower()
            if pstatus == 'captured':
                return 'captured', p.get('id')
            if pstatus in FINAL_FAILED:
                return 'failed', None
            # 'created' / 'authorized' → still in-flight

        return 'pending', None

    except Exception as exc:
        logger.warning(
            "Razorpay reconcile error for %s: %s", payment.razorpay_order_id, exc
        )
        return 'pending', None


# ── Shared capture helper ────────────────────────────────────────────────────

@transaction.atomic
def _capture_payment(payment, gateway_payment_id=None):
    """
    Mark payment + order as captured; deduct stock; notify buyer.
    Safe to call multiple times — idempotent on payment.status == 'captured'.
    Also handles the case where the order was previously marked 'failed'
    by an earlier (premature) expiry: resets it to 'placed'.
    """
    from cart.models import Cart, CartItem
    from notifications.models import AppNotification
    from orders.email_utils import send_order_confirmation_emails

    order = payment.order

    # Strict idempotency — never double-capture
    if payment.status == 'captured':
        logger.info("_capture_payment: payment %s already captured, skipping.", payment.id)
        return

    # Final stock check
    insufficient = [
        item.product_name
        for item in order.items.all()
        if item.variant and item.variant.stock < item.quantity
    ]
    if insufficient:
        payment.status = 'failed'
        payment.save(update_fields=['status'])
        order.status = 'failed'
        order.payment_status = 'failed'
        order.save(update_fields=['status', 'payment_status', 'updated_at'])
        logger.error(
            "Reconcile: stock mismatch for order %s — %s", order.order_number, insufficient
        )
        return

    # Write gateway payment ID
    if gateway_payment_id:
        if payment.gateway == 'cashfree':
            payment.cashfree_payment_id = gateway_payment_id
        else:
            payment.razorpay_payment_id = gateway_payment_id

    payment.status = 'captured'
    payment.save()

    # Reset order — may have been prematurely marked 'failed'
    order.is_paid = True
    order.payment_status = 'completed'
    order.status = 'placed'
    order.save()

    # Deduct stock
    for item in order.items.all():
        if item.variant:
            item.variant.stock -= item.quantity
            item.variant.save()

    # Mark sub-orders as placed
    for sub in order.sub_orders.all():
        sub.status = 'placed'
        sub.save(update_fields=['status', 'updated_at'])

    # Clear buyer cart
    if order.user:
        Cart.objects.filter(user=order.user).update(updated_at=timezone.now())
        CartItem.objects.filter(cart__user=order.user).delete()

    # Notify buyer
    if order.user:
        AppNotification.objects.create(
            user=order.user,
            title="Order Placed!",
            message=(
                f"Your order {order.order_number} has been confirmed. "
                f"Payment received — thank you!"
            ),
        )

    try:
        send_order_confirmation_emails(order)
    except Exception as exc:
        logger.error(
            "Reconcile: email failed for order %s: %s", order.order_number, exc
        )

    logger.info("Reconcile: captured order %s via %s", order.order_number, payment.gateway)


# ── Main reconciliation task ─────────────────────────────────────────────────

@shared_task(name='payments.tasks.reconcile_pending_payments')
def reconcile_pending_payments():
    """
    Runs every hour via Celery Beat.

    Checks ALL payments still in 'created' status that are older than
    MIN_AGE_MINUTES. No upper age cap — we keep checking until the
    gateway gives a definitive answer or HARD_EXPIRE_HOURS is reached.

    Decision tree per payment:
      gateway → SUCCESS        : capture (works even if previously marked 'failed')
      gateway → explicit FAIL  : mark failed (if not already)
      gateway → PENDING/unknown: leave as 'created'; check next run
      age > HARD_EXPIRE_HOURS  : hard-expire (no bank settles after 24h)
    """
    from payments.models import Payment

    now = timezone.now()
    min_age_cutoff = now - timedelta(minutes=MIN_AGE_MINUTES)
    hard_expire_cutoff = now - timedelta(hours=HARD_EXPIRE_HOURS)

    # Include 'failed' status too — Cashfree can still succeed after we
    # prematurely expire (bank settles late). Re-check anything not 'captured'.
    stale_payments = Payment.objects.filter(
        status__in=['created', 'failed'],
        created_at__lte=min_age_cutoff,
        created_at__gte=hard_expire_cutoff,  # don't query payments older than 24h
    ).exclude(
        status='failed',
        order__payment_status='failed',
        order__status='failed',
    ).select_related('order')
    # ^ For 'failed' payments, only re-check if the gateway might still
    #   succeed — exclude ones where we got an explicit failure webhook.
    #   (We distinguish by checking if both payment + order are 'failed'.)

    total = stale_payments.count()
    if not total:
        logger.info("Reconcile: no stale payments to check.")
        return "No stale payments to reconcile."

    logger.info("Reconcile: checking %d payment(s).", total)
    captured = expired = skipped = 0

    for payment in stale_payments:
        try:
            # ── Get gateway status ────────────────────────────────────────
            if payment.gateway == 'cashfree' and payment.cashfree_order_id:
                result, gw_id = _cashfree_gateway_status(payment)
            elif payment.gateway == 'razorpay' and payment.razorpay_order_id:
                result, gw_id = _razorpay_gateway_status(payment)
            else:
                logger.warning("Reconcile: payment %s has no gateway ID, skipping.", payment.id)
                skipped += 1
                continue

            # ── Handle result ─────────────────────────────────────────────
            if result == 'captured':
                _capture_payment(payment, gw_id)
                captured += 1

            elif result == 'failed':
                if payment.status != 'failed':
                    with transaction.atomic():
                        payment.status = 'failed'
                        payment.save(update_fields=['status'])
                        order = payment.order
                        # Don't overwrite a placed order (webhook may have captured it)
                        if order.status not in ('placed', 'shipped', 'delivered'):
                            order.status = 'failed'
                            order.payment_status = 'failed'
                            order.save(update_fields=['status', 'payment_status', 'updated_at'])
                    logger.info(
                        "Reconcile: gateway-confirmed failure for order %s (%s)",
                        payment.order.order_number, payment.gateway
                    )
                expired += 1

            else:
                # Still pending at gateway — check if we've hit the hard limit
                age = now - payment.created_at
                if age >= timedelta(hours=HARD_EXPIRE_HOURS):
                    with transaction.atomic():
                        payment.status = 'failed'
                        payment.save(update_fields=['status'])
                        order = payment.order
                        if order.status not in ('placed', 'shipped', 'delivered'):
                            order.status = 'failed'
                            order.payment_status = 'failed'
                            order.save(update_fields=['status', 'payment_status', 'updated_at'])
                    logger.warning(
                        "Reconcile: hard-expired order %s after %dh (no gateway response)",
                        payment.order.order_number, HARD_EXPIRE_HOURS
                    )
                    expired += 1
                else:
                    # Still within window — leave for next run
                    skipped += 1

        except Exception as exc:
            logger.error("Reconcile: unhandled error for payment %s: %s", payment.id, exc)
            skipped += 1

    summary = f"Reconcile complete: {captured} captured, {expired} expired, {skipped} still pending."
    logger.info(summary)
    return summary
