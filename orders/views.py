from rest_framework import generics, status, permissions
from rest_framework.response import Response
from django.db import transaction
import uuid
from cart.models import Cart
from shipping.models import ShippingAddress
from shipping.serializers import ShippingAddressSerializer
from notifications.models import AppNotification
from payments.models import Payment
from payments.razorpay_utils import create_razorpay_order, verify_payment_signature
from .models import Order, OrderItem
from .serializers import OrderSerializer

class CheckoutView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    @transaction.atomic
    def post(self, request):
        cart_id = request.data.get('cart_id')
        address_id = request.data.get('address_id')
        guest_info = request.data.get('guest_info')
        
        try:
            cart = Cart.objects.get(id=cart_id)
        except Cart.DoesNotExist:
            return Response({"error": "Cart not found"}, status=404)
            
        if not cart.items.exists():
            return Response({"error": "Cart is empty"}, status=400)
            
        # Address logic
        if request.user.is_authenticated:
            try:
                address_obj = ShippingAddress.objects.get(id=address_id, user=request.user)
                shipping_address = ShippingAddressSerializer(address_obj).data
                email = request.user.email
                phone = request.user.phone
            except ShippingAddress.DoesNotExist:
                return Response({"error": "Shipping address not found"}, status=400)
        else:
            if not guest_info:
                return Response({"error": "Guest info required"}, status=400)
            shipping_address = guest_info.get('address')
            email = guest_info.get('email')
            phone = guest_info.get('phone')

        # Stock and Totals
        subtotal = 0
        gst_total = 0
        
        for item in cart.items.all():
            if item.quantity > item.variant.stock:
                return Response({
                    "error": f"Botanical inventory mismatch: {item.product.name} ({item.variant.name}) has only {item.variant.stock} specimens available. Please adjust your box."
                }, status=400)
            
            subtotal += item.variant.price * item.quantity
            if item.product.categories.exists():
                gst_total += (item.variant.price * item.quantity * item.product.categories.first().gst_percentage / 100)
        
        total_amount = subtotal + gst_total
        
        order = Order.objects.create(
            order_number=f"JUN-{uuid.uuid4().hex[:8].upper()}",
            user=request.user if request.user.is_authenticated else None,
            guest_email=email if not request.user.is_authenticated else None,
            guest_phone=phone if not request.user.is_authenticated else None,
            shipping_address=shipping_address,
            subtotal=subtotal,
            gst_total=gst_total,
            total_amount=total_amount,
            status='pending'
        )
        
        for item in cart.items.all():
            OrderItem.objects.create(
                order=order,
                product=item.product,
                variant=item.variant,
                product_name=item.product.name,
                variant_name=item.variant.name,
                unit_price=item.variant.price,
                gst_percentage=item.product.categories.first().gst_percentage if item.product.categories.exists() else 0,
                quantity=item.quantity,
                seller=item.product.seller
            )
            
        # Create Razorpay Order
        try:
            razorpay_order = create_razorpay_order(int(total_amount * 100))
            Payment.objects.create(
                order=order,
                razorpay_order_id=razorpay_order['id'],
                amount=total_amount
            )
            return Response({
                "order": OrderSerializer(order).data,
                "razorpay_order_id": razorpay_order['id'],
                "amount": total_amount,
                "currency": "INR"
            }, status=201)
        except Exception as e:
            return Response({"error": f"Payment initiation failed: {str(e)}"}, status=500)

class VerifyPaymentView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        razorpay_order_id = request.data.get('razorpay_order_id')
        razorpay_payment_id = request.data.get('razorpay_payment_id')
        razorpay_signature = request.data.get('razorpay_signature')
        
        if verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
            try:
                payment = Payment.objects.get(razorpay_order_id=razorpay_order_id)
                order = payment.order
                
                # Final Stock Check before capture
                insufficient_items = []
                for item in order.items.all():
                    if item.variant and item.variant.stock < item.quantity:
                        insufficient_items.append(item.product_name)
                
                if insufficient_items:
                    # In a real scenario, we would trigger a refund here
                    order.status = 'failed'
                    order.save()
                    return Response({
                        "error": f"Fulfillment integrity compromised. The following specimens went out of stock during your transaction: {', '.join(insufficient_items)}. Please contact support for a refund."
                    }, status=400)

                payment.razorpay_payment_id = razorpay_payment_id
                payment.razorpay_signature = razorpay_signature
                payment.status = 'captured'
                payment.save()
                
                order.is_paid = True
                order.status = 'placed'
                order.save()
                
                if order.user:
                    AppNotification.objects.create(
                        user=order.user,
                        title="Order Placed!",
                        message=f"Your order {order.order_number} has been successfully placed."
                    )
                
                for item in order.items.all():
                    if item.variant:
                        item.variant.stock -= item.quantity
                        item.variant.save()
                        
                return Response({"message": "Payment verified and order placed"}, status=200)
            except Payment.DoesNotExist:
                return Response({"error": "Payment record not found"}, status=404)
        else:
            return Response({"error": "Invalid payment signature"}, status=400)

class OrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        user = self.request.user
        if user.role == 'grower':
            # For growers, return orders that contain their items
            return Order.objects.filter(items__seller=user).distinct()
        # For collectors, return their own orders
        return Order.objects.filter(user=user)
