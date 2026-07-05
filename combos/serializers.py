from rest_framework import serializers

from .models import Combo, ComboItem, ComboImage


def _variant_image(variant):
    """Best image for a variant: its own image first, else the product's primary."""
    img = next(iter(variant.images.all()), None)
    if img:
        return img.image_url
    product_images = variant.product.images.all()
    primary = next((i for i in product_images if i.is_primary), None) or next(iter(product_images), None)
    return primary.image_url if primary else None


def _store_name(seller):
    try:
        return seller.seller_profile.store_name
    except Exception:
        return seller.get_full_name() or seller.username


def _grower_info(seller):
    """Compact grower descriptor used for monograms + per-seller grouping."""
    profile = getattr(seller, 'seller_profile', None)
    name = _store_name(seller)
    city = getattr(profile, 'location_city', '') or ''
    brand = getattr(profile, 'brand_color', '') or '#0A3029'
    ships = ''
    if profile is not None:
        try:
            d = profile.get_next_shipping_date()
            if d:
                ships = 'Ships ' + d.strftime('%a, %-d %b')
        except Exception:
            ships = ''
    return {
        'seller_id': seller.id,
        'name': name,
        'city': city,
        'brand': brand,
        'initial': (name[:1] or '·').upper(),
        'ships': ships,
    }


class ComboImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComboImage
        fields = ('id', 'image_url', 'is_primary', 'order')


class ComboItemSerializer(serializers.ModelSerializer):
    """A single component of a combo, with the seller it ships from."""
    variant_id = serializers.UUIDField(source='variant.id', read_only=True)
    product_id = serializers.UUIDField(source='variant.product.id', read_only=True)
    product_name = serializers.CharField(source='variant.product.name', read_only=True)
    product_slug = serializers.CharField(source='variant.product.slug', read_only=True)
    variant_name = serializers.CharField(source='variant.name', read_only=True)
    unit_price = serializers.DecimalField(source='variant.price', max_digits=12, decimal_places=2, read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    stock = serializers.IntegerField(source='variant.stock', read_only=True)
    in_stock = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    seller_id = serializers.UUIDField(source='variant.product.seller_id', read_only=True)
    seller_name = serializers.SerializerMethodField()

    class Meta:
        model = ComboItem
        fields = (
            'id', 'variant_id', 'product_id', 'product_name', 'product_slug',
            'variant_name', 'unit_price', 'quantity', 'line_total',
            'stock', 'in_stock', 'image_url', 'seller_id', 'seller_name',
        )

    def get_in_stock(self, obj):
        return obj.variant.stock >= obj.quantity

    def get_image_url(self, obj):
        return _variant_image(obj.variant)

    def get_seller_name(self, obj):
        return _store_name(obj.variant.product.seller)


class ComboSellerSummarySerializer(serializers.Serializer):
    """Per-seller grower descriptor + item count, for the grouped detail view."""
    seller_id = serializers.UUIDField()
    name = serializers.CharField()
    city = serializers.CharField()
    brand = serializers.CharField()
    initial = serializers.CharField()
    ships = serializers.CharField()
    item_count = serializers.IntegerField()


class ComboListSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source='get_combo_type_display', read_only=True)
    type_value = serializers.CharField(source='combo_type', read_only=True)
    original_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    effective_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    item_count = serializers.SerializerMethodField()
    seller_count = serializers.IntegerField(read_only=True)
    in_stock = serializers.BooleanField(read_only=True)
    availability = serializers.CharField(read_only=True)

    class Meta:
        model = Combo
        fields = (
            'id', 'name', 'slug', 'tagline', 'image_url',
            'type', 'type_value', 'is_featured',
            'price', 'original_price', 'effective_price', 'shipping_fee', 'total_price',
            'item_count', 'seller_count', 'in_stock', 'availability',
        )

    def get_item_count(self, obj):
        return len(obj.items.all())


class ComboDetailSerializer(ComboListSerializer):
    description = serializers.CharField(read_only=True)
    items = ComboItemSerializer(many=True, read_only=True)
    images = ComboImageSerializer(many=True, read_only=True)
    sellers = serializers.SerializerMethodField()
    max_purchasable = serializers.IntegerField(read_only=True)

    class Meta(ComboListSerializer.Meta):
        fields = ComboListSerializer.Meta.fields + (
            'description', 'max_purchasable', 'items', 'images', 'sellers',
        )

    def get_sellers(self, obj):
        groups = {}
        for item in obj.items.all():
            seller = item.variant.product.seller
            sid = seller.id
            if sid not in groups:
                groups[sid] = {**_grower_info(seller), 'item_count': 0}
            groups[sid]['item_count'] += 1
        return ComboSellerSummarySerializer(list(groups.values()), many=True).data
