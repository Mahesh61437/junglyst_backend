from rest_framework import serializers
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
        addr = obj.order.shipping_address or {}
        return addr.get('firstName') or addr.get('full_name', '').split()[0] or 'Buyer'

    def get_buyer_pincode(self, obj):
        addr = obj.order.shipping_address or {}
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
