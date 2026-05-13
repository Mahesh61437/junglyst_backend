from rest_framework import serializers
from .models import Cart, CartItem


# ── Lean serializers — only what the frontend actually needs ──────────────────

class CartSellerSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    store_name = serializers.SerializerMethodField()

    def get_store_name(self, obj):
        try:
            return obj.seller_profile.store_name
        except Exception:
            return ''


class CartImageSerializer(serializers.Serializer):
    variant = serializers.UUIDField(source='variant_id')
    image_url = serializers.CharField()


class CartVariantSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    compare_at_price = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    stock = serializers.IntegerField()
    item_category = serializers.CharField(allow_null=True)


class CartProductSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    rating = serializers.FloatField(allow_null=True)
    seller = CartSellerSerializer()
    images = CartImageSerializer(many=True)


class CartItemSerializer(serializers.ModelSerializer):
    product_details = CartProductSerializer(source='product', read_only=True)
    variant_details = CartVariantSerializer(source='variant', read_only=True)

    class Meta:
        model = CartItem
        fields = ('id', 'product', 'variant', 'quantity', 'product_details', 'variant_details')


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_price = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ('id', 'user', 'session_id', 'items', 'total_price')

    def get_total_price(self, obj):
        # Items are already prefetched — no extra query here
        return sum(
            item.variant.price * item.quantity
            for item in obj.items.all()
            if item.variant_id is not None
        )
