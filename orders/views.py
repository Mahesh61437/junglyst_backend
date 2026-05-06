from rest_framework import generics, status, permissions
from rest_framework.response import Response
from django.db import transaction
from django.core.serializers.json import DjangoJSONEncoder
import json
import uuid
from decimal import Decimal
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
        if request.user.is_authenticated and address_id:
            try:
                address_obj = ShippingAddress.objects.get(id=address_id, user=request.user)
                shipping_address_raw = ShippingAddressSerializer(address_obj).data
                # Force UUID serialization for JSONField

                shipping_address = json.loads(json.dumps(shipping_address_raw, cls=DjangoJSONEncoder))
                
                email = request.user.email
                phone = request.user.phone
            except (ShippingAddress.DoesNotExist, ValueError):
                return Response({"error": "Shipping address not found or invalid"}, status=400)
        else:
            # Fallback to guest_info if no address_id provided (even for auth users)
            if not guest_info:
                return Response({"error": "Address information is required (either address_id or guest_info)"}, status=400)
            shipping_address = guest_info.get('address')
            email = guest_info.get('email') or (request.user.email if request.user.is_authenticated else None)
            phone = guest_info.get('phone') or (request.user.phone if request.user.is_authenticated else None)

        # Stock and Totals
        subtotal = 0
        gst_total = 0
        
        for item in cart.items.all():
            if item.quantity > item.variant.stock:
                return Response({
                    "error": f"Botanical inventory mismatch: {item.product.name} ({item.variant.name}) has only {item.variant.stock} specimens available. Please adjust your box."
                }, status=400)
            
            subtotal += item.variant.price * item.quantity
            
            # Use flat 18% GST to match frontend calculation
            item_gst = (item.variant.price * item.quantity) * Decimal('0.18')
            gst_total += item_gst
        
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
                gst_percentage=Decimal('18.00'),
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
            })
        except Exception as e:
            # Skip payment success for testing purpose - return order even if payment init fails
            print(f"DEBUG: Razorpay failed but continuing for testing: {str(e)}")
            return Response({
                "order": OrderSerializer(order).data,
                "message": "Payment initialized in TEST MODE (skipped actual gateway)",
                "razorpay_order_id": f"test_order_{uuid.uuid4().hex[:8]}",
                "amount": total_amount,
                "currency": "INR"
            })

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
        if user.is_staff or user.role == 'admin':
            return Order.objects.all()
        # For collectors, return their own orders
        return Order.objects.filter(user=user)

class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        user = self.request.user
        if user.role == 'grower':
            return Order.objects.filter(items__seller=user).distinct()
        if user.is_staff or user.role == 'admin':
            return Order.objects.all()
        return Order.objects.filter(user=user)
