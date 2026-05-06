from django.utils.text import slugify
from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from core.models import (
    Category, SubCategory, Tag, Product, ProductVariant, ProductImage
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
        return user

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        # The 'email' key will contain whatever the user typed (email or username)
        # Our custom backend core.backends.EmailOrUsernameModelBackend handles the lookup
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        return data

class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SubCategory
        fields = ('id', 'name', 'slug', 'description', 'image_url')

class CategorySerializer(serializers.ModelSerializer):
    subcategories = SubCategorySerializer(many=True, read_only=True)
    
    class Meta:
        model = Category
        fields = ('id', 'name', 'slug', 'description', 'image_url', 'gst_percentage', 'commission_rate', 'subcategories')

class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = '__all__'

class ProductVariantSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    
    class Meta:
        model = ProductVariant
        fields = '__all__'
        read_only_fields = ['product'] # price is now allowed to be passed but we can handle it in model save if needed

class ProductImageSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    
    class Meta:
        model = ProductImage
        fields = '__all__'
        read_only_fields = ['product']

class ProductSerializer(serializers.ModelSerializer):
    variants = ProductVariantSerializer(many=True, required=False)
    images = ProductImageSerializer(many=True, required=False)
    seller = UserSerializer(read_only=True)
    categories = CategorySerializer(many=True, read_only=True)
    sub_category = SubCategorySerializer(read_only=True)
    category_id = serializers.IntegerField(write_only=True, required=False)
    sub_category_id = serializers.IntegerField(write_only=True, required=False)
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

    def create(self, validated_data):
        variants_data = validated_data.pop('variants', [])
        images_data = validated_data.pop('images', [])
        category_id = validated_data.pop('category_id', None)
        sub_category_id = validated_data.pop('sub_category_id', None)
        
        # Auto-slugify
        name = validated_data.get('name')
        if name:
            slug = slugify(name)
            base_slug = slug
            counter = 1
            while Product.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            validated_data['slug'] = slug

        product = Product.objects.create(**validated_data)
        
        if category_id:
            try:
                category = Category.objects.get(id=category_id)
                product.categories.add(category)
            except Category.DoesNotExist:
                pass
        
        if sub_category_id:
            try:
                sub_cat = SubCategory.objects.get(id=sub_category_id)
                product.sub_category = sub_cat
                # Automatically add the parent category as well
                product.categories.add(sub_cat.category)
                product.save()
            except SubCategory.DoesNotExist:
                pass

        # Create variants and keep track of them for image mapping
        created_variants = []
        for variant_data in variants_data:
            variant = ProductVariant.objects.create(product=product, **variant_data)
            created_variants.append(variant)
            
        # Create images
        for image_data in images_data:
            variant_id = image_data.pop('variant_id', None)
            # Try to handle mapping by index (if string/int) or ID (if UUID)
            target_variant = None
            if variant_id is not None and variant_id != "":
                try:
                    # If it's an index (from new product flow)
                    idx = int(variant_id)
                    if 0 <= idx < len(created_variants):
                        target_variant = created_variants[idx]
                except (ValueError, TypeError):
                    # If it's a UUID (from edit flow)
                    try:
                        target_variant = ProductVariant.objects.get(id=variant_id, product=product)
                    except (ProductVariant.DoesNotExist, ValueError):
                        pass
            
            ProductImage.objects.create(product=product, variant=target_variant, **image_data)
            
        return product

    def update(self, instance, validated_data):
        # Use sentinel to distinguish "not sent" (partial PATCH) from "sent as empty list"
        _missing = object()
        variants_data = validated_data.pop('variants', _missing)
        images_data = validated_data.pop('images', _missing)
        category_id = validated_data.pop('category_id', None)
        sub_category_id = validated_data.pop('sub_category_id', None)

        # Update core fields (e.g. is_active from archive PATCH)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if category_id:
            instance.categories.clear()
            try:
                category = Category.objects.get(id=category_id)
                instance.categories.add(category)
            except Category.DoesNotExist:
                pass

        if sub_category_id:
            try:
                sub_cat = SubCategory.objects.get(id=sub_category_id)
                instance.sub_category = sub_cat
                # Ensure parent category is in categories
                if sub_cat.category not in instance.categories.all():
                    instance.categories.add(sub_cat.category)
                instance.save()
            except SubCategory.DoesNotExist:
                pass

        # Handle variants — only modify when variants were explicitly sent in the request
        if variants_data is not _missing:
            existing_variants = {str(v.id): v for v in instance.variants.all()}
            incoming_variant_ids = []

            for v_data in variants_data:
                v_id = str(v_data.pop('id')) if v_data.get('id') else None
                v_data.pop('product', None)
                v_data.pop('is_deleted', None)
                v_data.pop('deleted_at', None)
                v_data.pop('created_at', None)

                if v_id and v_id in existing_variants:
                    v_obj = existing_variants[v_id]
                    for attr, value in v_data.items():
                        setattr(v_obj, attr, value)
                    v_obj.save()
                    incoming_variant_ids.append(v_id)
                else:
                    new_v = ProductVariant.objects.create(product=instance, **v_data)
                    incoming_variant_ids.append(str(new_v.id))

            # Only delete variants that were explicitly omitted from a full update
            for v_id, v_obj in existing_variants.items():
                if v_id not in incoming_variant_ids:
                    v_obj.delete()

        # Handle images — only modify when images were explicitly sent in the request
        if images_data is not _missing:
            existing_images = {img.id: img for img in instance.images.all()}
            incoming_image_ids = []

            for img_data in images_data:
                img_id = img_data.pop('id', None)
                variant_id = img_data.pop('variant_id', None)
                img_data.pop('product', None)
                img_data.pop('is_deleted', None)
                img_data.pop('deleted_at', None)
                img_data.pop('created_at', None)

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

            # Only remove images that were explicitly omitted from a full update
            for img_id, img_obj in existing_images.items():
                if img_id not in incoming_image_ids:
                    img_obj.delete()

        return instance

    def get_variant(self, obj):
        return obj.variants.first()

    def get_price(self, obj):
        v = self.get_variant(obj)
        return v.price if v else 0

    def get_base_price(self, obj):
        v = self.get_variant(obj)
        return v.base_price if v else 0

    def get_gst_rate(self, obj):
        v = self.get_variant(obj)
        return v.gst_rate if v else 0

    def get_commission_rate(self, obj):
        v = self.get_variant(obj)
        return v.commission_rate if v else 0

    def get_stock(self, obj):
        v = self.get_variant(obj)
        return v.stock if v else 0

    def get_weight(self, obj):
        v = self.get_variant(obj)
        return v.weight if v else 0

    def get_length(self, obj):
        v = self.get_variant(obj)
        return v.length if v else 0

    def get_width(self, obj):
        v = self.get_variant(obj)
        return v.width if v else 0

    def get_height(self, obj):
        v = self.get_variant(obj)
        return v.height if v else 0
    def get_image(self, obj):
        first_image = obj.images.first()
        if first_image:
            return first_image.image_url
        return None
    def get_category(self, obj):
        first_cat = obj.categories.first()
        if first_cat:
            return first_cat.name
        return None

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    variant = ProductVariantSerializer(read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = ('id', 'product', 'variant', 'quantity', 'added_at', 'subtotal')

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
