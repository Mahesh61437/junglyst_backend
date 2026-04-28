from rest_framework import serializers
from .models import Cart, CartItem
from core.serializers import ProductSerializer, ProductVariantSerializer

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    variant = ProductVariantSerializer(read_only=True)
    
    class Meta:
        model = CartItem
        fields = ('id', 'product', 'variant', 'quantity')

class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_price = serializers.SerializerMethodField()
    
    class Meta:
        model = Cart
        fields = ('id', 'user', 'session_id', 'items', 'total_price')

    def get_total_price(self, obj):
        return sum(item.variant.price * item.quantity for item in obj.items.all())
