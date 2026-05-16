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
    variant_type = serializers.CharField(allow_null=True, allow_blank=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    compare_at_price = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    stock = serializers.IntegerField()
    item_category = serializers.CharField(allow_null=True)
    packed_weight_grams = serializers.IntegerField(allow_null=True)
    length = serializers.DecimalField(max_digits=10, decimal_places=2)
    width = serializers.DecimalField(max_digits=10, decimal_places=2)
    height = serializers.DecimalField(max_digits=10, decimal_places=2)
    image_url = serializers.SerializerMethodField()

    def get_image_url(self, obj):
        img = obj.images.first()
        return img.image_url if img else None


class CartProductSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    rating = serializers.FloatField(allow_null=True)
    seller = CartSellerSerializer()
    images = CartImageSerializer(many=True)
    image_url = serializers.SerializerMethodField()

    def get_image_url(self, obj):
        first = obj.images.first()
        return first.image_url if first else None


class CartItemSerializer(serializers.ModelSerializer):
    product_details = CartProductSerializer(source='product', read_only=True)
    variant_details = CartVariantSerializer(source='variant', read_only=True)

    class Meta:
        model = CartItem
        fields = ('id', 'product', 'variant', 'quantity', 'product_details', 'variant_details')


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_price = serializers.SerializerMethodField()
    seller_weight_summary = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ('id', 'user', 'session_id', 'items', 'total_price', 'seller_weight_summary')

    def get_total_price(self, obj):
        # Items are already prefetched — no extra query here
        return sum(
            item.variant.price * item.quantity
            for item in obj.items.all()
            if item.variant_id is not None
        )

    def get_seller_weight_summary(self, obj):
        """
        Per-seller aggregated weight breakdown.
        Helps sellers know total actual + volumetric weight they need to pack.
        Returns a dict keyed by seller_id.
        """
        from decimal import Decimal
        sellers = {}
        for item in obj.items.all():
            if not item.variant_id:
                continue
            v = item.variant
            qty = item.quantity
            seller_id = str(item.product.seller_id)

            if seller_id not in sellers:
                sellers[seller_id] = {
                    'seller_id': seller_id,
                    'total_actual_weight_g': 0,
                    'total_volumetric_weight_g': 0,
                    'has_heavy': False,
                    'item_count': 0,
                }

            # Actual packed weight
            packed = v.packed_weight_grams or 0
            sellers[seller_id]['total_actual_weight_g'] += packed * qty

            # Volumetric weight: (L × W × H / 5000) × 1000 grams
            try:
                vol_g = int(
                    (Decimal(str(v.length)) * Decimal(str(v.width)) * Decimal(str(v.height))
                     / Decimal('5000')) * Decimal('1000')
                )
            except Exception:
                vol_g = 0
            sellers[seller_id]['total_volumetric_weight_g'] += vol_g * qty
            sellers[seller_id]['item_count'] += qty

            if v.item_category == 'heavy':
                sellers[seller_id]['has_heavy'] = True

        # Add chargeable weight = max(actual, volumetric)
        for s in sellers.values():
            s['chargeable_weight_g'] = max(
                s['total_actual_weight_g'],
                s['total_volumetric_weight_g']
            )

        return list(sellers.values())
