from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from core.models import (
    Category, SubCategory, CategoryShippingRate, Tag, Product, ProductVariant, ProductImage, ProductReview, BugReport, Configuration
)
from cart.models import Cart, CartItem

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    seller_profile = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'email', 'username', 'phone', 'role', 'is_verified_seller', 'is_guest', 'full_name', 'avatar_url', 'location', 'seller_profile', 'is_staff', 'is_superuser')
        read_only_fields = ('id', 'is_verified_seller', 'is_superuser')

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username

    def get_seller_profile(self, obj):
        from sellers.serializers import SellerProfileSerializer
        try:
            return SellerProfileSerializer(obj.seller_profile).data
        except:
            return None


class PublicSellerProfileSerializer(serializers.Serializer):
    """
    Public-safe seller profile. Excludes PII (payout, GST, pickup address,
    account holder name) and is used wherever a seller's profile is embedded
    in a buyer-facing response (product detail, cart, etc.).
    """
    store_name = serializers.CharField()
    slug = serializers.CharField()
    logo_url = serializers.CharField(allow_null=True)
    icon_url = serializers.CharField(allow_null=True)
    banner_url = serializers.CharField(allow_null=True)
    brand_color = serializers.CharField()
    bio = serializers.CharField(allow_null=True)
    tagline = serializers.CharField(allow_null=True)
    location_city = serializers.CharField(allow_null=True)
    location_state = serializers.CharField(allow_null=True)
    expertise_tags = serializers.JSONField()
    infrastructure_details = serializers.CharField(allow_null=True)
    experience_years = serializers.IntegerField()
    identity_verified = serializers.BooleanField()
    is_featured = serializers.BooleanField()
    rating = serializers.CharField()
    shipping_days = serializers.JSONField()
    daily_cutoff_time = serializers.SerializerMethodField()
    blackout_dates = serializers.SerializerMethodField()
    next_shipping_date = serializers.SerializerMethodField()

    def get_daily_cutoff_time(self, obj):
        t = getattr(obj, 'daily_cutoff_time', None)
        return t.strftime('%H:%M') if t else '12:00'

    def get_blackout_dates(self, obj):
        try:
            return [
                {'start_date': b.start_date.isoformat(), 'end_date': b.end_date.isoformat()}
                for b in obj.blackout_dates.all()
            ]
        except Exception:
            return []

    def get_next_shipping_date(self, obj):
        try:
            d = obj.get_next_shipping_date()
            return d.isoformat() if d else None
        except Exception:
            return None


class PublicSellerSerializer(serializers.ModelSerializer):
    """
    Public-safe seller embed for product/cart responses. Only exposes id and the
    public seller profile — no email, phone, full_name, or staff flags.
    """
    seller_profile = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'seller_profile')

    def get_seller_profile(self, obj):
        try:
            return PublicSellerProfileSerializer(obj.seller_profile).data
        except Exception:
            return None

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    
    class Meta:
        model = User
        fields = ('email', 'username', 'password', 'phone', 'role', 'first_name', 'last_name')

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['username'],
            password=validated_data['password'],
            phone=validated_data.get('phone'),
            role=validated_data.get('role', 'collector'),
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', '')
        )
        from sellers.models import AllowedSeller
        email = (validated_data['email'] or '').strip()
        if AllowedSeller.objects.filter(email__iexact=email, is_active=True).exists():
            user.role = 'grower'
            user.is_staff = True
            user.save(update_fields=['role', 'is_staff'])
        return user

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        # The 'email' key will contain whatever the user typed (email or username)
        # Our custom backend core.backends.EmailOrUsernameModelBackend handles the lookup
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        return data

class CategoryShippingRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryShippingRate
        fields = ('id', 'category', 'sub_category', 'min_weight_grams', 'max_weight_grams',
                  'rate', 'free_above_order_value')


class SubCategorySerializer(serializers.ModelSerializer):
    effective_gst = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    effective_commission = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    shipping_rates = CategoryShippingRateSerializer(many=True, read_only=True)

    class Meta:
        model = SubCategory
        fields = ('id', 'name', 'slug', 'description', 'image_url',
                  'gst_percentage', 'commission_rate',
                  'effective_gst', 'effective_commission', 'shipping_rates')


class CategorySerializer(serializers.ModelSerializer):
    subcategories = SubCategorySerializer(many=True, read_only=True)
    shipping_rates = CategoryShippingRateSerializer(many=True, read_only=True)

    class Meta:
        model = Category
        fields = ('id', 'name', 'slug', 'description', 'image_url',
                  'gst_percentage', 'commission_rate', 'shipping_type',
                  'subcategories', 'shipping_rates')

class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = '__all__'


class ConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Configuration
        fields = ('id', 'name', 'data', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Name is required.')
        return value

    def validate_data(self, value):
        if value is None:
            return {}
        if not isinstance(value, (dict, list)):
            raise serializers.ValidationError('Data must be a JSON object or array.')
        return value

class ProductVariantSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    chargeable_weight = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = '__all__'
        read_only_fields = ['product']

    def get_chargeable_weight(self, obj):
        return obj.chargeable_weight

    def validate_packed_weight_grams(self, value):
        if value is not None and not (1 <= value <= 30000):
            raise serializers.ValidationError("Packed weight must be between 1g and 30,000g.")
        return value

    def validate(self, attrs):
        for dim_key in ('length', 'width', 'height'):
            val = attrs.get(dim_key)
            if val is not None and val <= 0:
                raise serializers.ValidationError({dim_key: "Box dimensions must be positive."})
        return attrs

class ProductImageSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    
    class Meta:
        model = ProductImage
        fields = ('id', 'image_url', 'is_primary', 'order', 'variant')
        read_only_fields = ('id',)

class ProductReviewSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(write_only=True, required=True)
    product = serializers.PrimaryKeyRelatedField(read_only=True)
    date = serializers.SerializerMethodField()
    image = serializers.ImageField(required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        fields = ('id', 'product', 'product_id', 'author', 'comment', 'plants', 'packaging', 'responsiveness', 'created_at', 'date', 'image', 'image_url')
        read_only_fields = ('id', 'created_at', 'date', 'image_url')

    def get_date(self, obj):
        return obj.created_at.strftime('%Y-%m-%d') if obj.created_at else None

    def get_image_url(self, obj):
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

    def create(self, validated_data):
        product_id = validated_data.pop('product_id')
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise serializers.ValidationError({'product_id': 'Product not found'})
        return ProductReview.objects.create(product=product, **validated_data)

class ProductSerializer(serializers.ModelSerializer):
    variants = ProductVariantSerializer(many=True, required=False)
    images = ProductImageSerializer(many=True, required=False)
    seller = PublicSellerSerializer(read_only=True)
    categories = CategorySerializer(many=True, read_only=True)
    sub_categories = SubCategorySerializer(many=True, read_only=True)
    sub_category = serializers.SerializerMethodField()
    category_id = serializers.IntegerField(write_only=True, required=False)
    category_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False)
    sub_category_id = serializers.IntegerField(write_only=True, required=False)
    sub_category_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False)
    seller_id = serializers.UUIDField(write_only=True, required=False)
    image = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    
    # Helper fields for the first variant (read-only for compatibility)
    price = serializers.SerializerMethodField()
    base_price = serializers.SerializerMethodField()
    gst_rate = serializers.SerializerMethodField()
    commission_rate = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    weight = serializers.SerializerMethodField()
    length = serializers.SerializerMethodField()
    width = serializers.SerializerMethodField()
    height = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = '__all__'
        read_only_fields = ['slug']

    def get_image(self, obj):
        images = obj.images.all()
        # Try to get primary image first, then fall back to first image
        primary_img = next((img for img in images if img.is_primary), None)
        target_img = primary_img or (images[0] if images else None)
        return target_img.image_url if target_img and target_img.image_url else None

    def get_category(self, obj):
        categories = obj.categories.all()
        return categories[0].name if categories else None

    def get_sub_category(self, obj):
        subcategories = obj.sub_categories.all()
        if subcategories:
            return SubCategorySerializer(subcategories[0], context=self.context).data
        return None

    def _first_variant(self, obj):
        # Uses prefetch cache — avoids 9 separate DB hits per product
        if not hasattr(obj, '_cached_first_variant'):
            variants = obj.variants.all()
            obj._cached_first_variant = variants[0] if variants else None
        return obj._cached_first_variant

    def get_variant(self, obj):
        return self._first_variant(obj)

    def get_price(self, obj):
        v = self._first_variant(obj)
        return v.price if v else 0

    def get_base_price(self, obj):
        v = self._first_variant(obj)
        return v.base_price if v else 0

    def get_gst_rate(self, obj):
        v = self._first_variant(obj)
        return v.gst_rate if v else 0

    def get_commission_rate(self, obj):
        v = self._first_variant(obj)
        return v.commission_rate if v else 0

    def get_stock(self, obj):
        v = self._first_variant(obj)
        return v.stock if v else 0

    def get_weight(self, obj):
        v = self._first_variant(obj)
        return v.weight if v else 0

    def get_length(self, obj):
        v = self._first_variant(obj)
        return v.length if v else 0

    def get_width(self, obj):
        v = self._first_variant(obj)
        return v.width if v else 0

    def get_height(self, obj):
        v = self._first_variant(obj)
        return v.height if v else 0

    def create(self, validated_data):
        variants_data = validated_data.pop('variants', [])
        images_data = validated_data.pop('images', [])
        category_id = validated_data.pop('category_id', None)
        category_ids = validated_data.pop('category_ids', None)
        sub_category_id = validated_data.pop('sub_category_id', None)
        sub_category_ids = validated_data.pop('sub_category_ids', None)
        seller_id = validated_data.pop('seller_id', None)
        if seller_id and 'seller' not in validated_data:
            try:
                validated_data['seller'] = User.objects.get(id=seller_id)
            except User.DoesNotExist:
                raise serializers.ValidationError({'seller_id': 'User not found'})

        product = Product.objects.create(**validated_data)

        cat_ids = []
        if category_ids:
            cat_ids.extend(category_ids)
        if category_id and category_id not in cat_ids:
            cat_ids.append(category_id)
        if cat_ids:
            product.categories.set(Category.objects.filter(id__in=cat_ids))

        sub_ids = []
        if sub_category_ids:
            sub_ids.extend(sub_category_ids)
        if sub_category_id and sub_category_id not in sub_ids:
            sub_ids.append(sub_category_id)

        if sub_ids:
            sub_cats = SubCategory.objects.filter(id__in=sub_ids)
            product.sub_categories.set(sub_cats)
            for sc in sub_cats:
                if not product.categories.filter(id=sc.category_id).exists():
                    product.categories.add(sc.category)

        created_variants = []
        for variant_data in variants_data:
            created_variants.append(ProductVariant.objects.create(product=product, **variant_data))

        for image_data in images_data:
            variant_id = image_data.pop('variant_id', None)
            target_variant = None
            if variant_id is not None and variant_id != "":
                try:
                    idx = int(variant_id)
                    if 0 <= idx < len(created_variants):
                        target_variant = created_variants[idx]
                except (ValueError, TypeError):
                    try:
                        target_variant = ProductVariant.objects.get(id=variant_id, product=product)
                    except (ProductVariant.DoesNotExist, ValueError):
                        pass
            ProductImage.objects.create(product=product, variant=target_variant, **image_data)

        return product

    def update(self, instance, validated_data):
        _missing = object()
        variants_data = validated_data.pop('variants', _missing)
        images_data = validated_data.pop('images', _missing)
        category_id = validated_data.pop('category_id', _missing)
        category_ids = validated_data.pop('category_ids', _missing)
        sub_category_id = validated_data.pop('sub_category_id', _missing)
        sub_category_ids = validated_data.pop('sub_category_ids', _missing)
        seller_id = validated_data.pop('seller_id', None)
        if seller_id:
            try:
                instance.seller = User.objects.get(id=seller_id)
            except User.DoesNotExist:
                raise serializers.ValidationError({'seller_id': 'User not found'})

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        cat_ids = []
        should_replace_cats = False
        if category_ids is not _missing:
            should_replace_cats = True
            if category_ids:
                cat_ids.extend(category_ids)
        if category_id is not _missing:
            should_replace_cats = True
            if category_id and category_id not in cat_ids:
                cat_ids.append(category_id)
        if should_replace_cats:
            instance.categories.set(Category.objects.filter(id__in=cat_ids))

        sub_ids = []
        should_update = False
        if sub_category_ids is not _missing:
            should_update = True
            if sub_category_ids:
                sub_ids.extend(sub_category_ids)
        if sub_category_id is not _missing:
            should_update = True
            if sub_category_id and sub_category_id not in sub_ids:
                sub_ids.append(sub_category_id)

        if should_update:
            sub_cats = SubCategory.objects.filter(id__in=sub_ids)
            instance.sub_categories.set(sub_cats)
            for sc in sub_cats:
                if not instance.categories.filter(id=sc.category_id).exists():
                    instance.categories.add(sc.category)

        if variants_data is not _missing:
            existing_variants = {str(v.id): v for v in instance.variants.all()}
            incoming_variant_ids = []
            for v_data in variants_data:
                v_id = str(v_data.pop('id')) if v_data.get('id') else None
                for key in ('product', 'is_deleted', 'deleted_at', 'created_at'):
                    v_data.pop(key, None)
                if v_id and v_id in existing_variants:
                    v_obj = existing_variants[v_id]
                    for attr, value in v_data.items():
                        setattr(v_obj, attr, value)
                    v_obj.save()
                    incoming_variant_ids.append(v_id)
                else:
                    incoming_variant_ids.append(str(ProductVariant.objects.create(product=instance, **v_data).id))
            for v_id, v_obj in existing_variants.items():
                if v_id not in incoming_variant_ids:
                    v_obj.delete()

        if images_data is not _missing:
            existing_images = {img.id: img for img in instance.images.all()}
            incoming_image_ids = []
            for img_data in images_data:
                img_id = img_data.pop('id', None)
                variant_id = img_data.pop('variant_id', None)
                for key in ('product', 'is_deleted', 'deleted_at', 'created_at'):
                    img_data.pop(key, None)
                target_variant = None
                if variant_id:
                    try:
                        target_variant = ProductVariant.objects.get(id=variant_id)
                    except (ProductVariant.DoesNotExist, ValueError):
                        pass
                if img_id and img_id in existing_images:
                    ProductImage.objects.filter(id=img_id).update(variant=target_variant, **img_data)
                    incoming_image_ids.append(img_id)
                else:
                    ProductImage.objects.create(product=instance, variant=target_variant, **img_data)
            for img_id, img_obj in existing_images.items():
                if img_id not in incoming_image_ids:
                    img_obj.delete()

        return instance

class ProductListSerializer(serializers.ModelSerializer):
    """
    Optimized version of ProductSerializer for listing pages.
    Provides necessary fields for ProductCard while minimizing database hits.
    """
    image = serializers.SerializerMethodField()
    seller = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    # We include minimal variant info for cart compatibility
    variants = serializers.SerializerMethodField()
    base_price = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'slug', 'scientific_name', 'care_level',
            'growth_rate', 'origin', 'is_rare', 'is_active',
            'price', 'image', 'seller', 'category_name', 'category', 'rating',
            'variants', 'base_price', 'stock'
        )

    def get_image(self, obj):
        images = obj.images.all()
        # Try to get primary image first, then fall back to first image
        primary_img = next((img for img in images if img.is_primary), None)
        target_img = primary_img or (images[0] if images else None)
        return target_img.image_url if target_img and target_img.image_url else None

    def _first_variant(self, obj):
        if not hasattr(obj, '_cached_first_variant'):
            variants = obj.variants.all()
            obj._cached_first_variant = variants[0] if variants else None
        return obj._cached_first_variant

    def get_base_price(self, obj):
        v = self._first_variant(obj)
        return v.base_price if v else 0

    def get_stock(self, obj):
        v = self._first_variant(obj)
        return v.stock if v else 0

    def get_seller(self, obj):
        # Return minimal, public-safe seller info — store identity only, no PII
        try:
            profile = obj.seller.seller_profile
            return {
                'id': str(obj.seller.id),
                'seller_profile': {
                    'store_name': profile.store_name,
                    'slug': profile.slug,
                    'brand_color': profile.brand_color,
                    'icon_url': profile.icon_url,
                }
            }
        except Exception:
            return {'id': str(obj.seller.id), 'seller_profile': None}

    def get_category_name(self, obj):
        subcategories = obj.sub_categories.all()
        if subcategories:
            return subcategories[0].name
        categories = obj.categories.all()
        return categories[0].name if categories else None

    def get_category(self, obj):
        # Always return the top-level (parent) category name, so the client
        # can branch on it (e.g. show care_level only for "Aquatic Plants").
        categories = obj.categories.all()
        if categories:
            return categories[0].name
        subcategories = obj.sub_categories.all()
        if subcategories:
            return subcategories[0].category.name
        return None

    def get_variants(self, obj):
        # Return only what ProductCard needs: id, price, stock, compare_at_price
        return [
            {
                'id': str(v.id),
                'name': v.name,
                'variant_type': v.variant_type,
                'price': str(v.price),
                'base_price': str(v.base_price),
                'stock': v.stock,
                'compare_at_price': str(v.compare_at_price) if v.compare_at_price else None,
            }
            for v in obj.variants.all()
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not data.get('price'):
            v = instance.variants.all()
            data['price'] = str(v[0].price) if v else "0"
        return data

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    variant = ProductVariantSerializer(read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = ('id', 'product', 'variant', 'quantity', 'created_at', 'subtotal')

    def get_subtotal(self, obj):
        price = obj.variant.price if obj.variant else obj.product.price
        return price * obj.quantity

class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_items = serializers.SerializerMethodField()
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ('id', 'items', 'total_items', 'subtotal', 'updated_at')

    def get_total_items(self, obj):
        return sum(item.quantity for item in obj.items.all())

    def get_subtotal(self, obj):
        return sum(
            (item.variant.price if item.variant else item.product.price) * item.quantity 
            for item in obj.items.all()
        )

class BugReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = BugReport
        fields = ('id', 'user', 'contact_info', 'description', 'images', 'status', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at', 'user', 'images')
