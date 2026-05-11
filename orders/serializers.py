from rest_framework import serializers
from django.db import models
from .models import Order, OrderItem, SubOrder
from shipping.serializers import ShipmentSerializer


class OrderItemSerializer(serializers.ModelSerializer):
    product_image = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = '__all__'

    def get_product_image(self, obj):
        if obj.product:
            image = obj.product.images.filter(is_primary=True).first() or obj.product.images.first()
            if image:
                return image.image_url
        return None


class SubOrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    seller_name = serializers.SerializerMethodField()
    shipment = serializers.SerializerMethodField()
    dispatch_hours_remaining = serializers.SerializerMethodField()

    class Meta:
        model = SubOrder
        fields = (
            'id', 'sub_order_number', 'status', 'seller', 'seller_name',
            'subtotal', 'shipping_fee', 'seller_total',
            'confirmed_at', 'dispatch_deadline', 'dispatch_hours_remaining',
            'packaging_photos',
            'actual_weight_grams', 'actual_length_cm', 'actual_breadth_cm', 'actual_height_cm',
            'awb_number', 'courier_name',
            'created_at', 'updated_at',
            'items', 'shipment',
        )

    def get_seller_name(self, obj):
        try:
            return obj.seller.seller_profile.store_name
        except Exception:
            return obj.seller.get_full_name() or obj.seller.username

    def get_shipment(self, obj):
        shipment = obj.order.shipments.filter(seller=obj.seller).first()
        return ShipmentSerializer(shipment).data if shipment else None

    def get_dispatch_hours_remaining(self, obj):
        if not obj.dispatch_deadline:
            return None
        from django.utils import timezone
        delta = obj.dispatch_deadline - timezone.now()
        hours = delta.total_seconds() / 3600
        return max(0, round(hours, 1))


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    sub_orders = SubOrderSerializer(many=True, read_only=True)
    shipments = ShipmentSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = '__all__'


class SellerSubOrderSerializer(serializers.ModelSerializer):
    """Sub-order view for the seller dashboard — seller sees only their sub-order."""
    items = OrderItemSerializer(many=True, read_only=True)
    shipment = serializers.SerializerMethodField()
    dispatch_hours_remaining = serializers.SerializerMethodField()
    buyer_first_name = serializers.SerializerMethodField()
    buyer_pincode = serializers.SerializerMethodField()

    class Meta:
        model = SubOrder
        fields = (
            'id', 'sub_order_number', 'status',
            'subtotal', 'shipping_fee', 'seller_total',
            'confirmed_at', 'dispatch_deadline', 'dispatch_hours_remaining',
            'packaging_photos',
            'actual_weight_grams', 'actual_length_cm', 'actual_breadth_cm', 'actual_height_cm',
            'awb_number', 'courier_name',
            'created_at', 'updated_at',
            'items', 'shipment',
            'buyer_first_name', 'buyer_pincode',
        )

    def get_shipment(self, obj):
        shipment = obj.order.shipments.filter(seller=obj.seller).first()
        return ShipmentSerializer(shipment).data if shipment else None

    def get_dispatch_hours_remaining(self, obj):
        if not obj.dispatch_deadline:
            return None
        from django.utils import timezone
        delta = obj.dispatch_deadline - timezone.now()
        return max(0, round(delta.total_seconds() / 3600, 1))

    def get_buyer_first_name(self, obj):
        if obj.order.user:
            return obj.order.user.first_name or obj.order.user.username
        
        addr = obj.order.shipping_address
        if isinstance(addr, str):
            import json
            try:
                addr = json.loads(addr)
            except Exception:
                addr = {}
        if not isinstance(addr, dict):
            addr = {}
            
        return addr.get('firstName') or addr.get('full_name', '').split()[0] if addr.get('full_name') else 'Buyer'

    def get_buyer_pincode(self, obj):
        addr = obj.order.shipping_address
        if isinstance(addr, str):
            import json
            try:
                addr = json.loads(addr)
            except Exception:
                addr = {}
        if not isinstance(addr, dict):
            addr = {}
            
        return addr.get('zip') or addr.get('pincode') or addr.get('postal_code') or '—'


class SellerOrderSerializer(serializers.ModelSerializer):
    """Kept for backwards-compat; wraps sub_orders for the requesting seller."""
    sub_orders = serializers.SerializerMethodField()
    my_item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'order_number', 'status', 'is_paid',
            'shipping_address', 'total_amount',
            'created_at', 'updated_at',
            'sub_orders', 'my_item_count',
        )

    def _seller(self):
        return self.context['request'].user

    def get_sub_orders(self, obj):
        qs = obj.sub_orders.filter(seller=self._seller())
        return SellerSubOrderSerializer(qs, many=True, context=self.context).data

    def get_my_item_count(self, obj):
        return obj.items.filter(seller=self._seller()).count()


class OrderSuccessSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for success page — only includes essential data:
    order_number, total_quantity, and total_amount.
    Optimized to reduce API response time.
    """
    total_quantity = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ('id', 'order_number', 'total_quantity', 'total_amount', 'is_paid')

    def get_total_quantity(self, obj):
        """Calculate total items ordered across all order items."""
        return obj.items.aggregate(models.Sum('quantity'))['quantity__sum'] or 0


class OrderTrackingSerializer(serializers.ModelSerializer):
    """
    Lightweight tracking serializer — optimized for fast fetching.
    Only includes: order_id, order_number, order_quantity, payment_status, items, sub_order_details, shipment_details.
    """
    total_quantity = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    sub_orders = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ('id', 'order_number', 'total_quantity', 'status', 'payment_status', 'items', 'sub_orders', 'created_at')

    def get_total_quantity(self, obj):
        """Calculate total items ordered from pre-fetched items."""
        return sum(item.quantity for item in obj.items.all())

    def get_payment_status(self, obj):
        """Return payment status."""
        # Try to get cached payment if available, otherwise query
        try:
            payment = obj.payments.first()  # Related name from Payment model
        except:
            from payments.models import Payment
            payment = Payment.objects.filter(order=obj).first()
        
        return {
            'is_paid': obj.is_paid,
            'payment_method': payment.method if payment else None,
            'amount_paid': float(payment.amount) if payment else None,
        }

    def get_items(self, obj):
        """Return lightweight order items list from pre-fetched data."""
        return [
            {
                'id': str(item.id),
                'product_name': item.product_name or (item.product.name if item.product else 'Unknown'),
                'variant_name': item.variant_name or (item.variant.name if item.variant else 'Default'),
                'quantity': item.quantity,
                'unit_price': float(item.unit_price),
                'product_image': self._get_product_image_cached(item),
            }
            for item in obj.items.all()
        ]

    def get_sub_orders(self, obj):
        """Return only tracking-relevant sub-order data from pre-fetched relationships."""
        result = []
        for so in obj.sub_orders.all():
            items_data = [
                {
                    'product_name': item.product_name or (item.product.name if item.product else 'Unknown'),
                    'variant_name': item.variant_name or (item.variant.name if item.variant else 'Default'),
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                }
                for item in so.items.all()
            ]
            
            result.append({
                'id': str(so.id),
                'sub_order_number': so.sub_order_number,
                'status': so.status,
                'seller_name': self._get_seller_name_cached(so),
                'items': items_data,
                'awb_number': so.awb_number,
                'courier_name': so.courier_name,
                'shipment': self._get_shipment_data_cached(so),
            })
        return result

    def _get_product_image_cached(self, item):
        """Extract product image URL from pre-loaded product data."""
        try:
            if item.product and hasattr(item.product, 'images'):
                images = list(item.product.images.all()) if hasattr(item.product.images, 'all') else []
                if images:
                    primary = next((img for img in images if img.is_primary), None) or images[0]
                    return primary.image_url if hasattr(primary, 'image_url') else None
        except Exception:
            pass
        return None

    def _get_seller_name_cached(self, sub_order):
        """Extract seller name from pre-loaded data."""
        try:
            seller = sub_order.seller
            if hasattr(seller, 'seller_profile') and seller.seller_profile:
                return seller.seller_profile.store_name
            return seller.get_full_name() or seller.username
        except Exception:
            return 'Unknown Seller'

    def _get_shipment_data_cached(self, sub_order):
        """Return only tracking-relevant shipment data."""
        # Try to find shipment from pre-loaded shipments
        try:
            # Access the order's prefetched shipments
            if hasattr(sub_order, 'order') and hasattr(sub_order.order, 'shipments'):
                shipments = list(sub_order.order.shipments.all())
                shipment = next(
                    (s for s in shipments if s.seller_id == sub_order.seller_id), 
                    None
                )
                if shipment:
                    return {
                        'id': str(shipment.id),
                        'status': shipment.status,
                        'tracking_url': shipment.tracking_url,
                        'estimated_delivery': shipment.estimated_delivery.isoformat() if shipment.estimated_delivery else None,
                    }
        except Exception:
            pass
        return None
