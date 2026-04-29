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
