from django.utils.text import slugify
from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from core.models import (
    Category, SubCategory, Tag, Product, ProductVariant, ProductImage
)

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    seller_profile = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ('id', 'email', 'username', 'phone', 'role', 'is_verified_seller', 'is_guest', 'full_name', 'avatar_url', 'location', 'seller_profile')
        read_only_fields = ('id', 'is_verified_seller')

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
    
    class Meta:
        model = User
        fields = ('email', 'username', 'password', 'phone', 'role')

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['username'],
            password=validated_data['password'],
            phone=validated_data.get('phone'),
            role=validated_data.get('role', 'collector')
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
    class Meta:
        model = ProductVariant
        fields = '__all__'
        read_only_fields = ['product', 'price']

class ProductImageSerializer(serializers.ModelSerializer):
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
        variants_data = validated_data.pop('variants', [])
        images_data = validated_data.pop('images', [])
        category_id = validated_data.pop('category_id', None)
        sub_category_id = validated_data.pop('sub_category_id', None)

        # Update core fields
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

        # Handle variants
        if variants_data:
            existing_variant_ids = [v.id for v in instance.variants.all()]
            incoming_variant_ids = [v_data.get('id') for v_data in variants_data if v_data.get('id')]
            
            # Simple approach: deactivate or delete missing variants if needed
            # For now, we update existing or create new
            for v_data in variants_data:
                v_id = v_data.get('id')
                if v_id and v_id in existing_variant_ids:
                    ProductVariant.objects.filter(id=v_id, product=instance).update(**v_data)
                else:
                    ProductVariant.objects.create(product=instance, **v_data)

        # Handle images
        if images_data:
            # Clear old images for this simple update or match by ID
            # instance.images.all().delete() 
            for img_data in images_data:
                img_id = img_data.get('id')
                variant_id = img_data.pop('variant_id', None)
                
                target_variant = None
                if variant_id:
                    try:
                        target_variant = ProductVariant.objects.get(id=variant_id, product=instance)
                    except (ProductVariant.DoesNotExist, ValueError):
                        pass

                if img_id:
                    ProductImage.objects.filter(id=img_id, product=instance).update(variant=target_variant, **img_data)
                else:
                    ProductImage.objects.create(product=instance, variant=target_variant, **img_data)

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
