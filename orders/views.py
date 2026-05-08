from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from types import SimpleNamespace
import uuid
from decimal import Decimal
from cart.models import Cart
from core.models import ProductVariant
from shipping.models import ShippingAddress
from shipping.serializers import ShippingAddressSerializer
from shipping.pincode_zones import classify_pincode
from notifications.models import AppNotification
from payments.models import Payment
from payments.razorpay_utils import create_razorpay_order, verify_payment_signature
from .models import Order, OrderItem, SubOrder
from .serializers import OrderSerializer, SellerOrderSerializer, SellerSubOrderSerializer

# ── Helpers ─────────────────────────────────────────────────────────────────

_LETTER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def _generate_order_number():
    """JNG-YYYY-NNNNN  (year + 5-digit random hex)"""
    import datetime
    year = datetime.datetime.now().year
    suffix = uuid.uuid4().hex[:5].upper()
    return f"JNG-{year}-{suffix}"

def _sub_order_number(master: str, index: int) -> str:
    """JNG-2026-ABCDE-A, -B, -C"""
    return f"{master}-{_LETTER[index]}"

def _shipping_fee_for_seller(subtotal: float, has_heavy: bool) -> int:
    if has_heavy:
        if subtotal < 999:   return 99
        if subtotal < 2499:  return 49
        return 0
    else:
        if subtotal < 699:   return 99
        if subtotal < 999:   return 49
        return 0

class CheckoutView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    @transaction.atomic
    def post(self, request):
        cart_id = request.data.get('cart_id')
        address_id = request.data.get('address_id')
        guest_info = request.data.get('guest_info')
        raw_items = request.data.get('items')   # guest itemized checkout (no cart)
        pincode = request.data.get('pincode', '')

        # ── Resolve cart items ──────────────────────────────────────────────
        if cart_id:
            try:
                cart_obj = Cart.objects.get(id=cart_id)
            except Cart.DoesNotExist:
                return Response({"error": "Cart not found"}, status=404)
            if not cart_obj.items.exists():
                return Response({"error": "Cart is empty"}, status=400)
            cart_items = list(cart_obj.items.select_related(
                'product', 'product__seller', 'variant'
            ).prefetch_related('product__categories').all())
        elif raw_items:
            if not isinstance(raw_items, list) or len(raw_items) == 0:
                return Response({"error": "Items list is empty"}, status=400)
            cart_items = []
            for entry in raw_items:
                vid = entry.get('variant_id')
                qty = int(entry.get('quantity', 1))
                if not vid or qty < 1:
                    return Response({"error": "Each item requires a valid variant_id and quantity >= 1"}, status=400)
                try:
                    variant = ProductVariant.objects.select_related(
                        'product', 'product__seller'
                    ).prefetch_related('product__categories').get(id=vid)
                except ProductVariant.DoesNotExist:
                    return Response({"error": f"Product variant not found: {vid}"}, status=400)
                cart_items.append(SimpleNamespace(
                    product=variant.product,
                    variant=variant,
                    quantity=qty,
                ))
        else:
            return Response({"error": "cart_id or items is required"}, status=400)

        # Hard pincode zone check (SHIP-002)
        if pincode:
            zone_info = classify_pincode(str(pincode))
            if not zone_info['deliverable']:
                return Response({"error": "Sorry, we don't deliver to your pincode yet."}, status=400)

        # Address
        if request.user.is_authenticated and address_id:
            # Saved address flow
            try:
                address_obj = ShippingAddress.objects.get(id=address_id, user=request.user)
                # Convert to plain dict with only address fields — strip user FK (UUID)
                # so the JSONField can serialize cleanly
                _raw = ShippingAddressSerializer(address_obj).data
                shipping_address = {
                    k: str(v) if hasattr(v, 'hex') else v   # UUID → str safety net
                    for k, v in _raw.items()
                    if k not in ('id', 'user', 'created_at', 'updated_at', 'is_default')
                }
                email = request.user.email
                phone = request.user.phone
            except (ShippingAddress.DoesNotExist, ValueError):
                return Response({"error": "Shipping address not found or invalid"}, status=400)
        else:
            # Guest checkout OR logged-in user with a one-time address
            if not guest_info:
                return Response({"error": "Shipping address or guest info is required"}, status=400)
            shipping_address = guest_info.get('address')
            email = guest_info.get('email') or (request.user.email if request.user.is_authenticated else None)
            phone = guest_info.get('phone') or (request.user.phone if request.user.is_authenticated else None)

        # Stock check + group items by seller

        seller_buckets = {}  # seller_id → {seller, items, subtotal, has_heavy}
        for item in cart_items:
            if item.quantity > item.variant.stock:
                return Response({
                    "error": f"Inventory mismatch: {item.product.name} has only {item.variant.stock} units available."
                }, status=400)

            sid = str(item.product.seller_id)
            if sid not in seller_buckets:
                seller_buckets[sid] = {
                    'seller': item.product.seller,
                    'items': [],
                    'subtotal': 0,
                    'has_heavy': False,
                }
            price = float(item.variant.price) * item.quantity
            seller_buckets[sid]['items'].append(item)
            seller_buckets[sid]['subtotal'] += price
            if item.variant.item_category == 'heavy':
                seller_buckets[sid]['has_heavy'] = True

        # SHIP-003: max 3 sellers
        if len(seller_buckets) > 3:
            return Response({"error": "Cart supports up to 3 sellers. Please remove items."}, status=400)

        # SHIP-003: min ₹500 per seller
        for sid, bucket in seller_buckets.items():
            if bucket['subtotal'] < 500:
                store = getattr(getattr(bucket['seller'], 'seller_profile', None), 'store_name', None) or bucket['seller'].username
                return Response({"error": f"Minimum order from {store} is ₹500."}, status=400)

        # Totals
        subtotal = sum(float(item.variant.price) * item.quantity for item in cart_items)
        gst_total = sum(
            float(item.variant.price) * item.quantity *
            float(item.product.categories.first().gst_percentage if item.product.categories.exists() else 0) / 100
            for item in cart_items
        )
        total_shipping = sum(
            _shipping_fee_for_seller(b['subtotal'], b['has_heavy'])
            for b in seller_buckets.values()
        )
        total_amount = subtotal + gst_total + total_shipping

        # Create master Order with new number format: JNG-YYYY-XXXXX
        order_number = _generate_order_number()
        while Order.objects.filter(order_number=order_number).exists():
            order_number = _generate_order_number()

        order = Order.objects.create(
            order_number=order_number,
            user=request.user if request.user.is_authenticated else None,
            guest_email=email if not request.user.is_authenticated else None,
            guest_phone=phone if not request.user.is_authenticated else None,
            shipping_address=shipping_address,
            subtotal=subtotal,
            shipping_fee=total_shipping,
            gst_total=gst_total,
            total_amount=total_amount,
            status='pending',
        )

        # Create SubOrders + OrderItems (SHIP-003)
        now = timezone.now()
        dispatch_deadline = now + timedelta(hours=48)

        for idx, (sid, bucket) in enumerate(seller_buckets.items()):
            seller_shipping = _shipping_fee_for_seller(bucket['subtotal'], bucket['has_heavy'])
            sub_order = SubOrder.objects.create(
                order=order,
                sub_order_number=_sub_order_number(order_number, idx),
                seller=bucket['seller'],
                subtotal=bucket['subtotal'],
                shipping_fee=seller_shipping,
                seller_total=bucket['subtotal'] + seller_shipping,
                status='placed',
                dispatch_deadline=dispatch_deadline,
            )

            for item in bucket['items']:
                gst_pct = float(item.product.categories.first().gst_percentage) if item.product.categories.exists() else 0
                OrderItem.objects.create(
                    order=order,
                    sub_order=sub_order,
                    product=item.product,
                    variant=item.variant,
                    product_name=item.product.name,
                    variant_name=item.variant.name,
                    unit_price=item.variant.price,
                    gst_percentage=gst_pct,
                    quantity=item.quantity,
                    seller=bucket['seller'],
                )

        # Razorpay
        try:
            razorpay_order = create_razorpay_order(int(total_amount * 100))
            Payment.objects.create(
                order=order,
                razorpay_order_id=razorpay_order['id'],
                amount=total_amount,
            )
            return Response({
                "order": OrderSerializer(order).data,
                "razorpay_order_id": razorpay_order['id'],
                "amount": total_amount,
                "currency": "INR",
            }, status=201)
        except Exception as e:
            print(f"DEBUG: Razorpay failed: {str(e)}")
            return Response({
                "error": f"Failed to initialize payment gateway: {str(e)}"
            }, status=400)

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
                    order.payment_status = 'failed'
                    order.save()
                    return Response({
                        "error": f"Fulfillment integrity compromised. The following specimens went out of stock during your transaction: {', '.join(insufficient_items)}. Please contact support for a refund."
                    }, status=400)

                payment.razorpay_payment_id = razorpay_payment_id
                payment.razorpay_signature = razorpay_signature
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

                # Notify each seller about their new sub-order (non-blocking bulk create)
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

                # Clear the buyer's cart
                if order.user:
                    Cart.objects.filter(user=order.user).update(updated_at=timezone.now())
                    from cart.models import CartItem
                    CartItem.objects.filter(cart__user=order.user).delete()

                return Response({"message": "Payment verified and order placed"}, status=200)
            except Payment.DoesNotExist:
                return Response({"error": "Payment record not found"}, status=404)
        else:
            return Response({"error": "Invalid payment signature"}, status=400)

class OrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        # Return the user's own purchases only
        return Order.objects.filter(user=self.request.user).order_by('-created_at')

class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user)


class SellerOrderListView(generics.ListAPIView):
    """Grower-scoped order list — items and shipment pre-filtered to the seller."""
    serializer_class = SellerOrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return (
            Order.objects
            .filter(items__seller=self.request.user)
            .distinct()
            .prefetch_related('items', 'shipments')
            .order_by('-created_at')
        )


class SellerSubOrderListView(generics.ListAPIView):
    """List sub-orders belonging to the requesting seller."""
    serializer_class = SellerSubOrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        qs = SubOrder.objects.filter(seller=self.request.user).select_related('order').prefetch_related('items')
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by('-created_at')

    def paginate_queryset(self, queryset):
        if self.request.query_params.get('no_pagination'):
            return None
        return super().paginate_queryset(queryset)


class ConfirmSubOrderView(APIView):
    """Seller confirms a sub-order → status 'confirmed', starts 48h dispatch clock."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if sub_order.status != 'placed':
            return Response({'error': f'Cannot confirm: current status is {sub_order.status}'}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        sub_order.status = 'confirmed'
        sub_order.confirmed_at = now
        sub_order.dispatch_deadline = now + timedelta(hours=48)
        sub_order.save(update_fields=['status', 'confirmed_at', 'dispatch_deadline', 'updated_at'])

        buyer = sub_order.order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Order Confirmed!',
                message=f'Your order {sub_order.sub_order_number} has been confirmed by the seller and is being prepared.',
            )

        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class UploadPackagingPhotoView(APIView):
    """Seller uploads packaging photo URL(s) for a sub-order."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        photo_url = request.data.get('photo_url')
        if not photo_url:
            return Response({'error': 'photo_url required'}, status=status.HTTP_400_BAD_REQUEST)

        photos = sub_order.packaging_photos or []
        photos.append(photo_url)
        sub_order.packaging_photos = photos
        if sub_order.status == 'confirmed':
            sub_order.status = 'packing'
        sub_order.save(update_fields=['packaging_photos', 'status', 'updated_at'])

        return Response({'packaging_photos': sub_order.packaging_photos, 'status': sub_order.status})


class UpdateShipmentDetailsView(APIView):
    """Seller saves actual package weight + dimensions before shipping."""
    permission_classes = (permissions.IsAuthenticated,)

    def patch(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if sub_order.status in ('shipped', 'in_transit', 'out_for_delivery', 'delivered', 'cancelled'):
            return Response({'error': 'Cannot update shipment details after dispatch.'}, status=status.HTTP_400_BAD_REQUEST)

        weight = request.data.get('actual_weight_grams')
        length = request.data.get('actual_length_cm')
        breadth = request.data.get('actual_breadth_cm')
        height = request.data.get('actual_height_cm')

        errors = {}
        if weight is not None:
            try:
                w = int(weight)
                if not (1 <= w <= 30000):
                    errors['actual_weight_grams'] = 'Must be between 1 and 30,000 grams.'
                else:
                    sub_order.actual_weight_grams = w
            except (ValueError, TypeError):
                errors['actual_weight_grams'] = 'Must be a positive integer.'

        for field, val in [('actual_length_cm', length), ('actual_breadth_cm', breadth), ('actual_height_cm', height)]:
            if val is not None:
                try:
                    v = int(val)
                    if v <= 0:
                        errors[field] = 'Must be a positive integer.'
                    else:
                        setattr(sub_order, field, v)
                except (ValueError, TypeError):
                    errors[field] = 'Must be a positive integer.'

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        sub_order.save(update_fields=['actual_weight_grams', 'actual_length_cm', 'actual_breadth_cm', 'actual_height_cm', 'updated_at'])
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class SubOrderShipView(APIView):
    """Seller marks sub-order as shipped (triggers NimbusPost or manual AWB)."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if sub_order.status not in ('confirmed', 'packing'):
            return Response({'error': f'Cannot ship: current status is {sub_order.status}'}, status=status.HTTP_400_BAD_REQUEST)

        if not sub_order.packaging_photos:
            return Response({'error': 'Upload at least one packaging photo before shipping.'}, status=status.HTTP_400_BAD_REQUEST)

        missing = []
        if not sub_order.actual_weight_grams:
            missing.append('actual weight (grams)')
        if not sub_order.actual_length_cm or not sub_order.actual_breadth_cm or not sub_order.actual_height_cm:
            missing.append('box dimensions (L × B × H)')
        if missing:
            return Response({'error': f'Please fill in {" and ".join(missing)} before shipping.'}, status=status.HTTP_400_BAD_REQUEST)

        courier_id = request.data.get('courier_id')
        manual_awb = request.data.get('awb_number')
        manual_courier = request.data.get('courier_name')

        if manual_awb:
            sub_order.awb_number = manual_awb
            sub_order.courier_name = manual_courier or 'Manual'
            sub_order.status = 'shipped'
            sub_order.save(update_fields=['awb_number', 'courier_name', 'status', 'updated_at'])
        else:
            sub_order.status = 'shipped'
            sub_order.save(update_fields=['status', 'updated_at'])
            from shipping.tasks import create_nimbuspost_shipment
            create_nimbuspost_shipment.delay(str(sub_order.order_id), str(request.user.id), courier_id, str(sub_order.id))

        buyer = sub_order.order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Your order is on its way!',
                message=f'Order {sub_order.sub_order_number} has been shipped. AWB: {sub_order.awb_number or "pending"}.',
            )

        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class ShipNowView(APIView):
    """
    POST /api/orders/ship-now/
    Body: { order_id, courier_id (optional) }
    Updates order status, notifies buyer, triggers NimbusPost shipment creation.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        order_id = request.data.get('order_id')
        courier_id = request.data.get('courier_id')

        if not order_id:
            return Response({'error': 'order_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order = Order.objects.get(id=order_id, items__seller=request.user)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

        # Update order status to processing
        if order.status not in ('shipped', 'delivered', 'cancelled'):
            Order.objects.filter(id=order_id).update(status='processing')

        # Notify buyer
        buyer = order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Your order is being packed!',
                message=(
                    f'Great news! Order {order.order_number} is being prepared for shipment '
                    f'by your grower. You will receive tracking details soon.'
                ),
            )

        # Trigger async NimbusPost shipment creation
        from shipping.tasks import create_nimbuspost_shipment
        create_nimbuspost_shipment.delay(str(order_id), str(request.user.id), courier_id)

        return Response(
            {'message': 'Shipment initiated', 'order_id': str(order_id)},
            status=status.HTTP_202_ACCEPTED,
        )


# ── Notification message templates ───────────────────────────────────────────
_BUYER_MSGS = {
    'in_transit':       ('Your order is in transit!',         'Sub-order {sub} is on its way. AWB: {awb}.'),
    'out_for_delivery': ('Out for delivery today!',            'Sub-order {sub} will be delivered today. Keep your phone handy!'),
    'delivered':        ('Delivered!',                         'Sub-order {sub} has been delivered. We hope you love your specimens!'),
    'delivery_failed':  ('Delivery attempt failed',            'The delivery of {sub} was unsuccessful. The courier will retry soon.'),
    'doa_raised':       ('DOA complaint raised',               'A Dead-on-Arrival complaint for {sub} has been registered. Our team will be in touch.'),
    'cancelled':        ('Order cancelled',                    'Sub-order {sub} has been cancelled. If you paid, a refund will be processed.'),
}
_SELLER_MSGS = {
    'delivered':        ('Sub-order delivered',                'Sub-order {sub} has been delivered successfully.'),
    'delivery_failed':  ('Delivery failed — action needed',    'Sub-order {sub} delivery failed. Please coordinate with the courier.'),
    'cancelled':        ('Sub-order cancelled',                'Sub-order {sub} has been cancelled.'),
}

# Allowed status transitions (current → allowed next states)
_VALID_TRANSITIONS = {
    'placed':           {'confirmed', 'cancelled'},
    'confirmed':        {'packing', 'cancelled'},
    'packing':          {'shipped', 'cancelled'},
    'shipped':          {'in_transit', 'delivered', 'delivery_failed', 'cancelled'},
    'in_transit':       {'out_for_delivery', 'delivered', 'delivery_failed', 'cancelled'},
    'out_for_delivery': {'delivered', 'delivery_failed'},
    'delivery_failed':  {'out_for_delivery', 'delivered', 'cancelled'},
    'delivered':        {'doa_raised'},
    'doa_raised':       set(),
    'cancelled':        set(),
}


class UpdateSubOrderStatusView(APIView):
    """
    PATCH /api/orders/seller/sub-orders/<pk>/status/
    Called by NimbusPost webhook, admin panel, or seller for manual overrides.
    Fires buyer + seller notifications for each status change.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def patch(self, request, pk):
        # Sellers can only update their own sub-orders; staff/admin can update any
        try:
            if request.user.is_staff or getattr(request.user, 'role', '') == 'admin':
                sub_order = SubOrder.objects.select_related('order', 'order__user', 'seller').get(id=pk)
            else:
                sub_order = SubOrder.objects.select_related('order', 'order__user', 'seller').get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        if not new_status:
            return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)

        allowed = _VALID_TRANSITIONS.get(sub_order.status, set())
        if new_status not in allowed:
            return Response(
                {'error': f"Cannot transition from '{sub_order.status}' to '{new_status}'. Allowed: {sorted(allowed) or 'none'}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sub_order.status = new_status
        # Auto-set timestamps
        if new_status == 'confirmed' and not sub_order.confirmed_at:
            sub_order.confirmed_at = timezone.now()
            sub_order.dispatch_deadline = sub_order.confirmed_at + timedelta(hours=48)
        sub_order.save()

        awb = sub_order.awb_number or 'pending'
        notifs = []

        # Notify buyer
        buyer = sub_order.order.user
        guest_email = sub_order.order.guest_email
        if new_status in _BUYER_MSGS:
            title, msg_tpl = _BUYER_MSGS[new_status]
            msg = msg_tpl.format(sub=sub_order.sub_order_number, awb=awb)
            if buyer:
                notifs.append(AppNotification(user=buyer, title=title, message=msg))
            # TODO: send email to guest_email if no buyer account

        # Notify seller for key events
        if new_status in _SELLER_MSGS:
            title, msg_tpl = _SELLER_MSGS[new_status]
            msg = msg_tpl.format(sub=sub_order.sub_order_number)
            notifs.append(AppNotification(user=sub_order.seller, title=title, message=msg))

        if notifs:
            AppNotification.objects.bulk_create(notifs)

        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)
