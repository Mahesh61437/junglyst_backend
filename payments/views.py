import json
import logging
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
