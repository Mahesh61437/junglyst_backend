from rest_framework import serializers
from .models import Cart, CartItem


# ── Lean serializers — only what the frontend actually needs ──────────────────

class CartSellerSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    store_name = serializers.SerializerMethodField()
    shipping_days = serializers.SerializerMethodField()
    daily_cutoff_time = serializers.SerializerMethodField()
    blackout_dates = serializers.SerializerMethodField()
    next_shipping_date = serializers.SerializerMethodField()
    slug = serializers.SerializerMethodField()

    def get_slug(self, obj):
        try:
            return obj.seller_profile.slug
        except Exception:
            return ''

    def get_store_name(self, obj):
        try:
            return obj.seller_profile.store_name
        except Exception:
            return ''

    def get_shipping_days(self, obj):
        try:
            return obj.seller_profile.shipping_days or []
        except Exception:
            return []

    def get_daily_cutoff_time(self, obj):
        try:
            t = obj.seller_profile.daily_cutoff_time
            return t.strftime('%H:%M') if t else '12:00'
        except Exception:
            return '12:00'

    def get_blackout_dates(self, obj):
        try:
            return [
                {'start_date': b.start_date.isoformat(), 'end_date': b.end_date.isoformat()}
                for b in obj.seller_profile.blackout_dates.all()
            ]
        except Exception:
            return []

    def get_next_shipping_date(self, obj):
        try:
            d = obj.seller_profile.get_next_shipping_date()
            return d.isoformat() if d else None
        except Exception:
            return None


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
        Per-seller aggregated weight breakdown with shipping category.
        Helps sellers know total actual + volumetric weight they need to pack.
        Also indicates shipping category: light, heavy, or hybrid (both light and heavy).
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
                    'has_light': False,
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

            # Track both light and heavy items
            if v.item_category == 'heavy':
                sellers[seller_id]['has_heavy'] = True
            elif v.item_category == 'light':
                sellers[seller_id]['has_light'] = True

        # Add chargeable weight = max(actual, volumetric) and determine shipping category
        for s in sellers.values():
            s['chargeable_weight_g'] = max(
                s['total_actual_weight_g'],
                s['total_volumetric_weight_g']
            )
            # Determine shipping category: hybrid if both light and heavy
            if s['has_light'] and s['has_heavy']:
                s['shipping_category'] = 'hybrid'
            elif s['has_heavy']:
                s['shipping_category'] = 'heavy'
            else:
                s['shipping_category'] = 'light'

        return list(sellers.values())


class CheckoutNudgeProductSerializer(serializers.Serializer):
    """Minimal serializer for checkout nudge products — avoids heavy nested serializers."""
    id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    price = serializers.SerializerMethodField()
    variant_id = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    from_cart_seller = serializers.BooleanField()
    seller_name = serializers.SerializerMethodField()
    source_category_name = serializers.SerializerMethodField()

    def get_source_category_name(self, obj):
        return getattr(obj, '_source_category_name', '')

    def get_price(self, obj):
        variant = self._cheapest_variant(obj)
        return float(variant.price) if variant else None

    def get_variant_id(self, obj):
        variant = self._cheapest_variant(obj)
        return str(variant.id) if variant else None

    def get_stock(self, obj):
        variant = self._cheapest_variant(obj)
        return variant.stock if variant else 0

    def get_image_url(self, obj):
        img = next(iter(obj.images.all()), None)
        return img.image_url if img else None

    def get_seller_name(self, obj):
        try:
            return obj.seller.seller_profile.store_name
        except Exception:
            return ''

    def _cheapest_variant(self, obj):
        return next(
            (v for v in sorted(obj.variants.all(), key=lambda v: v.price)
             if v.is_active and v.stock > 0),
            None
        )
