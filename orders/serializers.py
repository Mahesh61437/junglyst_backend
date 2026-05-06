from rest_framework import serializers
from .models import Order, OrderItem
from shipping.serializers import ShipmentSerializer

class OrderItemSerializer(serializers.ModelSerializer):
    product_image = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = '__all__'

    def get_product_image(self, obj):
        if obj.product:
            image = obj.product.images.filter(is_primary=True).first()
            if not image:
                image = obj.product.images.first()
            if image:
                return image.image_url
        return None

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    shipments = ShipmentSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = '__all__'


class SellerOrderSerializer(serializers.ModelSerializer):
    """Order view scoped to the requesting seller: items and shipment filtered."""
    items = serializers.SerializerMethodField()
    my_shipment = serializers.SerializerMethodField()
    my_total = serializers.SerializerMethodField()
    my_item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            'id', 'order_number', 'status', 'is_paid',
            'shipping_address', 'total_amount',
            'created_at', 'updated_at',
            'items', 'my_shipment', 'my_total', 'my_item_count',
        )

    def _seller(self):
        return self.context['request'].user

    def get_items(self, obj):
        qs = obj.items.filter(seller=self._seller())
        return OrderItemSerializer(qs, many=True, context=self.context).data

    def get_my_shipment(self, obj):
        shipment = obj.shipments.filter(seller=self._seller()).first()
        if shipment:
            return ShipmentSerializer(shipment).data
        return None

    def get_my_total(self, obj):
        items = obj.items.filter(seller=self._seller())
        return float(sum(i.unit_price * i.quantity for i in items))

    def get_my_item_count(self, obj):
        return obj.items.filter(seller=self._seller()).count()
