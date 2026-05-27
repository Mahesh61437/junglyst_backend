import json
import logging
import time
import uuid
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.utils import timezone
from notifications.models import AppNotification
from cart.models import Cart
from cart.models import CartItem
from orders.models import Order
from orders.email_utils import send_order_confirmation_emails
from .models import Payment, PaymentGatewaySettings, PaymentGateway
from .cashfree_utils import verify_webhook_signature
from .razorpay_utils import verify_razorpay_webhook_signature

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class CashfreeWebhookView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request):
        body = request.body.decode('utf-8')
        signature = {
            'x-webhook-timestamp': request.headers.get('x-webhook-timestamp', ''),
            'x-webhook-signature': request.headers.get('x-webhook-signature', '')
        }

        if not signature['x-webhook-signature']:
            return HttpResponse("Missing signature", status=400)

        if not verify_webhook_signature(body, signature):
            return HttpResponse("Invalid signature", status=400)

        try:
            data = json.loads(body)
            event = data.get('type')
            payload = data.get('data', {})

            if event == 'PAYMENT_SUCCESS_WEBHOOK':
                payment_data = payload.get('payment', {})
                order_data = payload.get('order', {})
                cashfree_order_id = order_data.get('order_id')
                cashfree_payment_id = payment_data.get('cf_payment_id')

                if cashfree_order_id:
                    self.handle_order_paid(cashfree_order_id, cashfree_payment_id, payment_data)

            elif event == 'PAYMENT_FAILED_WEBHOOK':
                payment_data = payload.get('payment', {})
                order_data = payload.get('order', {})
                cashfree_order_id = order_data.get('order_id')
                if cashfree_order_id:
                    self.handle_payment_failed(cashfree_order_id, payment_data)

        except Exception as e:
            logger.exception(f"Webhook processing error: {e}")
            return HttpResponse("Webhook Error", status=500)

        return HttpResponse("OK", status=200)

    def handle_order_paid(self, cashfree_order_id, cashfree_payment_id, payment_data):
        try:
            with transaction.atomic():
                payment = Payment.objects.select_for_update().get(
                    cashfree_order_id=cashfree_order_id)

                # Already fully captured — idempotent, nothing to do
                if payment.status == 'captured':
                    return

                # Accept both 'created' and 'failed' statuses — the reconcile task
                # may have prematurely marked this 'failed' before the bank settled.
                # A webhook from Cashfree is authoritative: if Cashfree says SUCCESS,
                # we capture regardless of our internal status.

                order = payment.order

                insufficient_items = []
                for item in order.items.select_related('variant').all():
                    variant = (item.variant.__class__.objects
                            .select_for_update()
                            .get(pk=item.variant.pk))
                    if variant.stock < item.quantity:
                        insufficient_items.append(item.product_name)

                if insufficient_items:
                    order.status = 'failed'
                    order.payment_status = 'failed'
                    order.save()
                    logger.error(
                        "Webhook: stock mismatch for order %s — %s", order.order_number, insufficient_items
                    )
                    return

                payment.cashfree_payment_id = cashfree_payment_id
                payment.method = payment_data.get('payment_group', '')
                payment.status = 'captured'
                payment.gateway_status = payment_data.get('payment_status', 'SUCCESS')
                payment.bank_reference = payment_data.get('bank_reference', '') or payment_data.get('utr', '')
                payment.gateway_response = payment_data
                payment.paid_at = timezone.now()
                payment.save()

                order.is_paid = True
                order.payment_status = 'completed'
                order.status = 'confirmed'  # reset even if previously 'failed'
                order.save()

                if order.user:
                    AppNotification.objects.create(
                        user=order.user,
                        title="Order Placed!",
                        message=f"Your order {order.order_number} has been successfully placed and is being prepared."
                    )

                seller_notifs = []
                for sub in order.sub_orders.select_related('seller').all():
                    if sub.status not in ('placed', 'confirmed', 'shipped', 'delivered'):
                        sub.status = 'placed'
                        sub.save(update_fields=['status', 'updated_at'])
                    item_count = sub.items.count()
                    seller_notifs.append(AppNotification(
                        user=sub.seller,
                        title="New Order Received!",
                        message=(
                            f"Sub-order {sub.sub_order_number} has been placed with "
                            f"{item_count} item{'s' if item_count != 1 else ''}. "
                            f"Please confirm within 48 hours."
                        ),
                    ))
                if seller_notifs:
                    AppNotification.objects.bulk_create(seller_notifs)

                for item in order.items.all():
                    if item.variant:
                        item.variant.__class__.objects.filter(pk=item.variant.pk).update(
                                stock=F('stock') - item.quantity
                            )

                if order.user:
                    Cart.objects.filter(user=order.user).update(updated_at=timezone.now())
                    CartItem.objects.filter(cart__user=order.user).delete()

                order_number = order.order_number

            send_order_confirmation_emails(order)
            logger.info("Webhook: captured order %s via Cashfree (late settlement handled).", order_number)

            # Cancel remaining scheduled reconcile tasks — payment is done
            try:
                from payments.tasks import cancel_payment_checks
                cancel_payment_checks(payment.id)
            except Exception:
                pass

        except Payment.DoesNotExist:
            logger.warning("Webhook: no Payment record for cashfree_order_id=%s", cashfree_order_id)


    def handle_payment_failed(self, cashfree_order_id, payment_data=None):
        try:
            payment = Payment.objects.get(cashfree_order_id=cashfree_order_id)
            # Never overwrite a captured payment or a placed/shipped order
            if payment.status == 'captured':
                return
            order = payment.order
            if order.status in ('confirmed', 'processing', 'shipped', 'in_transit', 'out_for_delivery', 'delivered'):
                return
            payment.status = 'failed'
            payment.gateway_status = 'FAILED'
            if payment_data:
                payment.cashfree_payment_id = payment_data.get('cf_payment_id') or payment.cashfree_payment_id
                payment.method = payment_data.get('payment_group', '') or payment.method
                payment.bank_reference = payment_data.get('bank_reference', '') or payment_data.get('utr', '')
                error_details = payment_data.get('error_details') or {}
                payment.error_code = error_details.get('error_code', '') or payment_data.get('payment_status', '')
                payment.error_message = error_details.get('error_description', '') or error_details.get('error_reason', '')
                payment.gateway_response = payment_data
            payment.save()
            order.status = 'failed'
            order.payment_status = 'failed'
            order.save()

            # Cancel remaining scheduled reconcile tasks — payment resolved
            try:
                from payments.tasks import cancel_payment_checks
                cancel_payment_checks(payment.id)
            except Exception:
                pass
        except Payment.DoesNotExist:
            logger.warning("Webhook FAILED: no Payment for cashfree_order_id=%s", cashfree_order_id)


@method_decorator(csrf_exempt, name='dispatch')
class RazorpayWebhookView(APIView):
    """
    Razorpay webhook receiver.

    Signature: HMAC-SHA256(raw_body, RAZORPAY_WEBHOOK_SECRET) — hex digest,
    compared to the X-Razorpay-Signature header. See
    https://razorpay.com/docs/webhooks/validate-test/.

    Handled events:
      - payment.captured       → mark Payment captured (idempotent)
      - order.paid             → same capture flow (fallback if payment.captured missed)
      - payment.authorized     → log only; payment_capture=1 auto-captures, so
                                 a follow-up payment.captured handles the rest
      - payment.failed         → mark Payment failed
      - refund.created/processed/failed → logged for audit; no model changes
                                          yet (no Refund model in this codebase)

    Razorpay retries failed webhooks for up to 24h, so any non-2xx response
    triggers retry. We return 200 even for unhandled events so they don't
    needlessly retry. Signature failures return 400 (the secret is wrong —
    retrying won't help, but Razorpay's convention is 4xx for these).
    """
    permission_classes = (AllowAny,)

    def post(self, request):
        t0 = time.perf_counter()
        raw_body = request.body
        received_signature = request.headers.get('X-Razorpay-Signature', '')

        # Razorpay sends X-Razorpay-Event-Id on every delivery; same ID is used
        # across retries of the same event, so it's the canonical idempotency
        # key for a webhook delivery. We also mint our own short req_id so a
        # single Python invocation can be traced in logs even when X-Razorpay-
        # Event-Id is absent (e.g. a manual Postman test).
        rzp_event_id = request.headers.get('X-Razorpay-Event-Id', '')
        req_id = uuid.uuid4().hex[:8]
        remote_ip = (
            request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', '?')
        )
        content_type = request.META.get('CONTENT_TYPE', '?')

        # Best-effort peek at the event name so signature-failure logs still
        # know what was being delivered. Never fatal.
        try:
            preview_event = json.loads(raw_body.decode('utf-8')).get('event', '?')
        except Exception:
            preview_event = '?'

        # ── Entry log — one line per webhook hit ─────────────────────────────
        logger.info(
            "Razorpay webhook RX req_id=%s rzp_event_id=%s event=%s "
            "body_len=%d content_type=%s remote_ip=%s sig_present=%s",
            req_id, rzp_event_id or '<none>', preview_event,
            len(raw_body), content_type, remote_ip,
            'yes' if received_signature else 'no',
        )

        # ── Signature verification ───────────────────────────────────────────
        ok, reason, computed_prefix = verify_razorpay_webhook_signature(
            raw_body, received_signature
        )
        if not ok:
            logger.warning(
                "Razorpay webhook SIG_FAIL req_id=%s reason=%s event=%s "
                "body_len=%d received_sig=%s computed_prefix=%s remote_ip=%s "
                "elapsed_ms=%d",
                req_id, reason, preview_event, len(raw_body),
                received_signature or '<missing>',
                computed_prefix or '<n/a>',
                remote_ip,
                int((time.perf_counter() - t0) * 1000),
            )
            status_code = 400 if reason != 'secret_unset' else 503
            return HttpResponse(f"Signature check failed: {reason}", status=status_code)

        # ── Body parse ───────────────────────────────────────────────────────
        try:
            data = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning(
                "Razorpay webhook BAD_BODY req_id=%s error=%s body_preview=%r",
                req_id, exc, raw_body[:200],
            )
            return HttpResponse("Malformed body", status=400)

        event = data.get('event', '')
        payload = data.get('payload', {}) or {}
        account_id = data.get('account_id', '')
        contains = data.get('contains', [])

        logger.info(
            "Razorpay webhook PARSED req_id=%s event=%s account_id=%s contains=%s "
            "payload_keys=%s",
            req_id, event, account_id, contains, list(payload.keys()),
        )

        # ── Route to handler ─────────────────────────────────────────────────
        try:
            if event in ('payment.captured', 'order.paid'):
                self._handle_capture(event, payload, req_id)
            elif event == 'payment.authorized':
                self._handle_authorized(payload, req_id)
            elif event == 'payment.failed':
                self._handle_failed(payload, req_id)
            elif event in ('refund.created', 'refund.processed', 'refund.failed'):
                self._handle_refund(event, payload, req_id)
            else:
                logger.info(
                    "Razorpay webhook UNHANDLED req_id=%s event=%s — acked with 200.",
                    req_id, event,
                )
        except Exception:
            # Full payload is invaluable for disputes & post-mortems.
            logger.exception(
                "Razorpay webhook HANDLER_ERROR req_id=%s event=%s payload=%s",
                req_id, event, payload,
            )
            return HttpResponse("Webhook Error", status=500)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Razorpay webhook ACK req_id=%s event=%s elapsed_ms=%d",
            req_id, event, elapsed_ms,
        )
        return HttpResponse("OK", status=200)

    # ── Event handlers ────────────────────────────────────────────────────

    def _handle_capture(self, event, payload, req_id):
        """payment.captured / order.paid → run shared capture flow."""
        from payments.tasks import _capture_payment

        payment_entity = (payload.get('payment') or {}).get('entity') or {}
        order_entity = (payload.get('order') or {}).get('entity') or {}

        razorpay_order_id = payment_entity.get('order_id') or order_entity.get('id')
        razorpay_payment_id = payment_entity.get('id')
        amount = payment_entity.get('amount') or order_entity.get('amount')
        method = payment_entity.get('method', '')

        logger.info(
            "Razorpay webhook CAPTURE_EXTRACTED req_id=%s event=%s order_id=%s "
            "payment_id=%s amount=%s method=%s",
            req_id, event, razorpay_order_id, razorpay_payment_id, amount, method,
        )

        if not razorpay_order_id:
            logger.warning(
                "Razorpay webhook CAPTURE_NO_ORDER_ID req_id=%s event=%s "
                "payment_entity_keys=%s order_entity_keys=%s",
                req_id, event,
                list(payment_entity.keys()), list(order_entity.keys()),
            )
            return

        try:
            payment = Payment.objects.select_related('order').get(
                razorpay_order_id=razorpay_order_id
            )
        except Payment.DoesNotExist:
            logger.warning(
                "Razorpay webhook CAPTURE_PAYMENT_NOT_FOUND req_id=%s event=%s "
                "razorpay_order_id=%s — was the Order created via our checkout?",
                req_id, event, razorpay_order_id,
            )
            return

        prev_status = payment.status
        prev_order_status = payment.order.status
        order_number = payment.order.order_number

        logger.info(
            "Razorpay webhook CAPTURE_PAYMENT_FOUND req_id=%s payment_id=%s "
            "order_number=%s prev_payment_status=%s prev_order_status=%s",
            req_id, payment.id, order_number, prev_status, prev_order_status,
        )

        # _capture_payment is idempotent — early-returns when status='captured'.
        _capture_payment(payment, razorpay_payment_id, payment_entity or None)
        payment.refresh_from_db()

        action = 'no_change' if prev_status == payment.status else f'{prev_status}->{payment.status}'
        logger.info(
            "Razorpay webhook CAPTURE_DONE req_id=%s event=%s payment_id=%s "
            "order_number=%s action=%s new_payment_status=%s",
            req_id, event, payment.id, order_number, action, payment.status,
        )

    def _handle_authorized(self, payload, req_id):
        """
        payment.authorized fires before capture. Since we use payment_capture=1,
        a payment.captured event will follow. We just log here.
        """
        entity = (payload.get('payment') or {}).get('entity') or {}
        logger.info(
            "Razorpay webhook AUTHORIZED req_id=%s order_id=%s payment_id=%s "
            "amount=%s method=%s — awaiting payment.captured.",
            req_id,
            entity.get('order_id'), entity.get('id'),
            entity.get('amount'), entity.get('method'),
        )

    def _handle_failed(self, payload, req_id):
        from payments.tasks import cancel_payment_checks

        entity = (payload.get('payment') or {}).get('entity') or {}
        razorpay_order_id = entity.get('order_id')
        error_code = entity.get('error_code', '')
        error_desc = entity.get('error_description', '') or entity.get('error_reason', '')

        logger.info(
            "Razorpay webhook FAILED_EXTRACTED req_id=%s order_id=%s payment_id=%s "
            "error_code=%s error_description=%s",
            req_id, razorpay_order_id, entity.get('id'), error_code, error_desc,
        )

        if not razorpay_order_id:
            logger.warning(
                "Razorpay webhook FAILED_NO_ORDER_ID req_id=%s entity_keys=%s",
                req_id, list(entity.keys()),
            )
            return

        try:
            payment = Payment.objects.select_related('order').get(
                razorpay_order_id=razorpay_order_id
            )
        except Payment.DoesNotExist:
            logger.warning(
                "Razorpay webhook FAILED_PAYMENT_NOT_FOUND req_id=%s "
                "razorpay_order_id=%s",
                req_id, razorpay_order_id,
            )
            return

        order = payment.order
        # Never overwrite a successful capture or a progressed order.
        if payment.status == 'captured':
            logger.info(
                "Razorpay webhook FAILED_SKIP req_id=%s payment_id=%s "
                "reason=already_captured — refusing to mark failed.",
                req_id, payment.id,
            )
            return
        if order.status in ('confirmed', 'processing', 'shipped',
                            'in_transit', 'out_for_delivery', 'delivered'):
            logger.info(
                "Razorpay webhook FAILED_SKIP req_id=%s payment_id=%s order_status=%s "
                "reason=order_already_progressed",
                req_id, payment.id, order.status,
            )
            return

        with transaction.atomic():
            payment.status = 'failed'
            payment.gateway_status = 'failed'
            payment.razorpay_payment_id = entity.get('id') or payment.razorpay_payment_id
            payment.method = entity.get('method', '') or payment.method
            payment.error_code = error_code or ''
            payment.error_message = error_desc or ''
            payment.gateway_response = entity
            payment.save()

            order.status = 'failed'
            order.payment_status = 'failed'
            order.save()

        try:
            cancel_payment_checks(payment.id)
        except Exception:
            logger.exception(
                "Razorpay webhook FAILED_CANCEL_CHECKS_ERROR req_id=%s payment_id=%s",
                req_id, payment.id,
            )

        logger.info(
            "Razorpay webhook FAILED_DONE req_id=%s payment_id=%s order_number=%s "
            "error_code=%s",
            req_id, payment.id, order.order_number, error_code,
        )

    def _handle_refund(self, event, payload, req_id):
        """
        No Refund model in this codebase yet — just persist the latest refund
        info on Payment.gateway_response so admins / disputes have a trail.
        """
        entity = (payload.get('refund') or {}).get('entity') or {}
        razorpay_payment_id = entity.get('payment_id')
        refund_id = entity.get('id')
        amount = entity.get('amount')
        refund_status = entity.get('status')

        logger.info(
            "Razorpay webhook REFUND_EXTRACTED req_id=%s event=%s refund_id=%s "
            "payment_id=%s amount=%s status=%s",
            req_id, event, refund_id, razorpay_payment_id, amount, refund_status,
        )

        if not razorpay_payment_id:
            logger.warning(
                "Razorpay webhook REFUND_NO_PAYMENT_ID req_id=%s event=%s "
                "entity_keys=%s",
                req_id, event, list(entity.keys()),
            )
            return

        try:
            payment = Payment.objects.get(razorpay_payment_id=razorpay_payment_id)
        except Payment.DoesNotExist:
            logger.warning(
                "Razorpay webhook REFUND_PAYMENT_NOT_FOUND req_id=%s event=%s "
                "razorpay_payment_id=%s",
                req_id, event, razorpay_payment_id,
            )
            return

        # Merge refund info onto gateway_response — never overwrite the original
        # payment data, just append under a 'refunds' key.
        existing = payment.gateway_response or {}
        refunds = list(existing.get('refunds', []))
        refunds.append({'event': event, 'refund': entity})
        existing['refunds'] = refunds
        payment.gateway_response = existing
        payment.save(update_fields=['gateway_response', 'updated_at'])

        logger.info(
            "Razorpay webhook REFUND_DONE req_id=%s event=%s payment_id=%s "
            "refund_id=%s amount=%s total_refunds_on_payment=%d",
            req_id, event, payment.id, refund_id, amount, len(refunds),
        )


class PaymentGatewaySettingsView(APIView):
    """
    GET  /api/payments/gateway-settings/  -> { active_gateway }
    PATCH /api/payments/gateway-settings/ -> { active_gateway }
    Admin-only.
    """
    from core.permissions import IsAdminUser
    permission_classes = (IsAdminUser,)

    def get(self, request):
        s = PaymentGatewaySettings.get_solo()
        return Response({"active_gateway": s.active_gateway})

    def patch(self, request):
        from django.contrib.auth import get_user_model
        active = request.data.get("active_gateway")
        if active not in (PaymentGateway.CASHFREE, PaymentGateway.RAZORPAY):
            return Response({"error": "active_gateway must be 'cashfree' or 'razorpay'."}, status=400)
        s = PaymentGatewaySettings.get_solo()
        previous = s.active_gateway
        s.active_gateway = active
        s.save(update_fields=["active_gateway", "updated_at"])

        if previous != active:
            User = get_user_model()
            superadmins = User.objects.filter(is_staff=True, is_superuser=True, is_active=True)
            label = {"cashfree": "Cashfree (UPI/QR)", "razorpay": "Razorpay"}.get(active, active)
            prev_label = {"cashfree": "Cashfree (UPI/QR)", "razorpay": "Razorpay"}.get(previous, previous)
            actor = request.user.get_full_name() or request.user.username or request.user.email
            notifs = [
                AppNotification(
                    user=admin,
                    title="Payment Gateway Switched",
                    message=(
                        f"{actor} switched the active payment gateway from "
                        f"{prev_label} to {label}. "
                        f"All new checkouts will now use {label}."
                    ),
                )
                for admin in superadmins
            ]
            if notifs:
                AppNotification.objects.bulk_create(notifs)

        return Response({"active_gateway": s.active_gateway})
