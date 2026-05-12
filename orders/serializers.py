from rest_framework import serializers
from .models import Order, OrderItem, SubOrder
from shipping.serializers import ShipmentSerializer


# ── Item serializers ──────────────────────────────────────────────────────────

class OrderItemListSerializer(serializers.ModelSerializer):
    """Lean item serializer for the order list endpoint — no extra DB hits."""
    product_image = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ('id', 'product_name', 'variant_name', 'unit_price', 'quantity', 'product_image')

    def get_product_image(self, obj):
        if not obj.product:
            return None
        # Reads from prefetch cache — no extra query when called from OrderListView
        images = obj.product.images.all()
        img = next((i for i in images if i.is_primary), None) or next(iter(images), None)
        return img.image_url if img else None


class OrderItemSerializer(serializers.ModelSerializer):
    """Full item serializer for the order detail endpoint."""
    product_image = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = (
            'id', 'product', 'variant', 'product_name', 'variant_name',
            'unit_price', 'gst_percentage', 'quantity', 'seller', 'product_image',
        )

    def get_product_image(self, obj):
        if not obj.product:
            return None
        images = obj.product.images.all()
        img = next((i for i in images if i.is_primary), None) or next(iter(images), None)
        return img.image_url if img else None


# ── Order list serializer ─────────────────────────────────────────────────────

class OrderListSerializer(serializers.ModelSerializer):
    """Minimal serializer for GET /orders/ — only what the profile history tab needs."""
    items = OrderItemListSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            'id', 'order_number', 'status', 'payment_status',
            'subtotal', 'shipping_fee', 'total_amount',
            'is_paid', 'created_at',
            'items',
        )


# ── Order detail serializer ───────────────────────────────────────────────────

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
        # Use the prefetched attr set by OrderDetailView — avoids one query per sub-order
        shipments = getattr(obj.order, 'shipments_prefetched', None)
        if shipments is not None:
            shipment = next((s for s in shipments if s.seller_id == obj.seller_id), None)
        else:
            shipment = obj.order.shipments.filter(seller=obj.seller).first()
        return ShipmentSerializer(shipment).data if shipment else None

    def get_dispatch_hours_remaining(self, obj):
        if not obj.dispatch_deadline:
            return None
        from django.utils import timezone
        delta = obj.dispatch_deadline - timezone.now()
        return max(0, round(delta.total_seconds() / 3600, 1))


class OrderDetailSerializer(serializers.ModelSerializer):
    """Full serializer for GET /orders/<pk>/ — all nested data."""
    items = OrderItemSerializer(many=True, read_only=True)
    sub_orders = SubOrderSerializer(many=True, read_only=True)
    shipments = ShipmentSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = (
            'id', 'order_number', 'status', 'payment_status',
            'user', 'guest_email', 'guest_phone',
            'shipping_address',
            'subtotal', 'shipping_fee', 'gst_total', 'total_amount',
            'awb_number', 'courier_name', 'estimated_delivery',
            'is_paid', 'created_at', 'updated_at',
            'items', 'sub_orders', 'shipments',
        )


# Alias so checkout response (which serializes after create) keeps working
OrderSerializer = OrderDetailSerializer


# ── Seller serializers ────────────────────────────────────────────────────────

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
