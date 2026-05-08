import json
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.utils import timezone
from notifications.models import AppNotification
from cart.models import Cart
from cart.models import CartItem
from orders.models import Order
from .models import Payment
from .razorpay_utils import verify_webhook_signature

@method_decorator(csrf_exempt, name='dispatch')
class RazorpayWebhookView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request):
        body = request.body.decode('utf-8')
        signature = request.headers.get('X-Razorpay-Signature')

        if not signature:
            return HttpResponse("Missing signature", status=400)

        if not verify_webhook_signature(body, signature):
            return HttpResponse("Invalid signature", status=400)

        try:
            data = json.loads(body)
            event = data.get('event')
            payload = data.get('payload', {})

            if event == 'order.paid':
                order_data = payload.get('order', {}).get('entity', {})
                payment_data = payload.get('payment', {}).get('entity', {})
                razorpay_order_id = order_data.get('id')
                razorpay_payment_id = payment_data.get('id')

                if razorpay_order_id:
                    self.handle_order_paid(razorpay_order_id, razorpay_payment_id, payment_data)

            elif event == 'payment.failed':
                payment_data = payload.get('payment', {}).get('entity', {})
                razorpay_order_id = payment_data.get('order_id')
                if razorpay_order_id:
                    self.handle_payment_failed(razorpay_order_id)

        except Exception as e:
            print(f"Webhook processing error: {e}")
            return HttpResponse("Webhook Error", status=500)

        return HttpResponse("OK", status=200)

    def handle_order_paid(self, razorpay_order_id, razorpay_payment_id, payment_data):
        try:
            payment = Payment.objects.get(razorpay_order_id=razorpay_order_id)
            if payment.status == 'captured':
                return # Already processed by frontend handler
                
            order = payment.order
            
            # Reduce stock
            insufficient_items = []
            for item in order.items.all():
                if item.variant and item.variant.stock < item.quantity:
                    insufficient_items.append(item.product_name)
            
            if insufficient_items:
                order.status = 'failed'
                order.payment_status = 'failed'
                order.save()
                return

            payment.razorpay_payment_id = razorpay_payment_id
            payment.method = payment_data.get('method', '')
            payment.status = 'captured'
            payment.save()
            
            order.is_paid = True
            order.payment_status = 'completed'
            order.status = 'placed'
            order.save()
            
            # Notify buyer
            if order.user:
                AppNotification.objects.create(
                    user=order.user,
                    title="Order Placed!",
                    message=f"Your order {order.order_number} has been successfully placed and is being prepared."
                )

            # Notify each seller
            seller_notifs = []
            for sub in order.sub_orders.select_related('seller').all():
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
                    item.variant.stock -= item.quantity
                    item.variant.save()

            if order.user:
                Cart.objects.filter(user=order.user).update(updated_at=timezone.now())
                CartItem.objects.filter(cart__user=order.user).delete()

        except Payment.DoesNotExist:
            pass

    def handle_payment_failed(self, razorpay_order_id):
        try:
            payment = Payment.objects.get(razorpay_order_id=razorpay_order_id)
            if payment.status != 'captured':
                payment.status = 'failed'
                payment.save()
                order = payment.order
                order.status = 'failed'
                order.payment_status = 'failed'
                order.save()
        except Payment.DoesNotExist:
            pass
