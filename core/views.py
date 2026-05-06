import uuid
from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.text import slugify
from .models import Product, Category, SubCategory, ProductVariant, ProductImage, WishlistItem
from cart.models import Cart, CartItem
from orders.models import Order
from .serializers import (
    RegisterSerializer, CustomTokenObtainPairSerializer, UserSerializer,
    ProductSerializer, CategorySerializer, SubCategorySerializer, CartSerializer
)
from .storage import upload_to_firebase

User = get_user_model()

def link_ghost_orders(user):
    """Link any previous guest orders to this user based on email/phone."""
    updated_count = 0
    if user.email:
        updated_count += Order.objects.filter(user__isnull=True, guest_email=user.email).update(user=user)
    if hasattr(user, 'phone_number') and user.phone_number:
        updated_count += Order.objects.filter(user__isnull=True, guest_phone=user.phone_number).update(user=user)
    return updated_count

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (permissions.AllowAny,)
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Link ghost orders immediately upon registration
        link_ghost_orders(user)
        
        return Response({
            "user": UserSerializer(user).data,
            "message": "User created successfully",
        }, status=status.HTTP_201_CREATED)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            # Look up user to link orders on every login (just in case)
            username = request.data.get('email') # Serializer handles email/username
            try:
                user = User.objects.get(email=username) if '@' in username else User.objects.get(username=username)
                link_ghost_orders(user)
            except User.DoesNotExist:
                pass
        return response

class UserProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_object(self):
        return self.request.user

def _product_queryset():
    return Product.objects.select_related(
        'seller', 'seller__seller_profile',
        'sub_category', 'sub_category__category',
    ).prefetch_related(
        'variants', 'images', 'categories',
        'categories__subcategories', 'tags',
    )


class ProductListView(generics.ListAPIView):
    serializer_class = ProductSerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_fields = ('categories', 'sub_category', 'seller', 'is_active', 'is_rare')
    search_fields = ('name', 'description', 'tags__name')
    ordering_fields = ('created_at', 'rating')

    def get_queryset(self):
        queryset = _product_queryset()

        seller_slug = self.request.query_params.get('seller_slug')
        if seller_slug:
            queryset = queryset.filter(seller__seller_profile__slug=seller_slug)

        seller_id = self.request.query_params.get('seller')
        is_active_param = self.request.query_params.get('is_active')
        if seller_id and is_active_param is None:
            pass
        else:
            if is_active_param is None:
                queryset = queryset.filter(is_active=True)

        return queryset

class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProductSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    lookup_field = 'id'

    def get_queryset(self):
        return _product_queryset()

    def get_permissions(self):
        if self.request.method in ['PUT', 'PATCH', 'DELETE']:
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def perform_update(self, serializer):
        # Ensure only the seller or admin can update
        product = self.get_object()
        if self.request.user != product.seller and self.request.user.role != 'admin':
            raise permissions.PermissionDenied("You do not have permission to edit this specimen.")
        instance = serializer.save()
        # When archiving: zero out all variant stocks (keep images & prices intact)
        if not instance.is_active:
            instance.variants.all().update(stock=0)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        if self.request.user != instance.seller and self.request.user.role != 'admin':
            raise permissions.PermissionDenied("You do not have permission to archive this specimen.")
        instance.is_active = False
        instance.variants.all().update(stock=0)
        instance.save()


class GrowerProductCreateView(generics.CreateAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = ProductSerializer

    def perform_create(self, serializer):
        if self.request.user.role not in ['grower', 'admin']:
            raise permissions.PermissionDenied("Only growers can list products")
        serializer.save(seller=self.request.user)


class AdminProductCreateView(generics.CreateAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = ProductSerializer

    def create(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.role == 'admin'):
            return Response({'error': 'Admin access required'}, status=403)
        if not request.data.get('seller_id'):
            return Response({'error': 'seller_id is required'}, status=400)
        return super().create(request, *args, **kwargs)


class ProductCopyView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, id):
        if not (request.user.is_staff or request.user.role == 'admin'):
            return Response({'error': 'Admin access required'}, status=403)

        try:
            original = Product.objects.prefetch_related(
                'variants', 'images', 'categories', 'tags'
            ).get(id=id)
        except Product.DoesNotExist:
            return Response({'error': 'Not found'}, status=404)

        base_name = f"{original.name} (Copy)"
        base_slug = slugify(base_name)
        slug = base_slug
        counter = 1
        while Product.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        seller = original.seller
        seller_id = request.data.get('seller_id')
        if seller_id:
            try:
                seller = User.objects.get(id=seller_id)
            except User.DoesNotExist:
                pass

        new_product = Product(
            name=base_name,
            slug=slug,
            tagline=original.tagline,
            description=original.description,
            seller=seller,
            sub_category=original.sub_category,
            scientific_name=original.scientific_name,
            care_level=original.care_level,
            light_requirements=original.light_requirements,
            growth_rate=original.growth_rate,
            is_rare=original.is_rare,
            origin=original.origin,
            water_temperature=original.water_temperature,
            ph_range=original.ph_range,
            is_active=False,
            co2_requirement=original.co2_requirement,
        )
        new_product.save()

        for cat in original.categories.all():
            new_product.categories.add(cat)

        variant_map = {}
        for v in original.variants.all():
            old_id = v.id
            v.pk = None
            v.sku = None
            v.product = new_product
            v.save()
            variant_map[old_id] = v

        for img in original.images.all():
            old_variant_id = img.variant_id
            img.pk = None
            img.product = new_product
            img.variant = variant_map.get(old_variant_id)
            img.save()

        from .serializers import ProductSerializer as PS
        return Response(PS(new_product, context={'request': request}).data, status=201)

class CategoryListView(generics.ListAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = (permissions.AllowAny,)

class SubCategoryListView(generics.ListAPIView):
    queryset = SubCategory.objects.all()
    serializer_class = SubCategorySerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ('category',)

class ImageUploadView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        file_obj = request.FILES.get('image')
        file_type = request.data.get('type', 'asset')
        
        if not file_obj:
            return Response({"error": "No image provided"}, status=400)
            
        try:
            url = upload_to_firebase(file_obj, request.user.id, file_type)
            return Response({"url": url}, status=201)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

class SyncCartView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = CartSerializer

    def post(self, request):
        items = request.data.get('items', [])
        cart, _ = Cart.objects.get_or_create(user=request.user)
        
        for item_data in items:
            p_id = item_data.get('product_id')
            v_id = item_data.get('variant_id')
            qty = item_data.get('quantity', 1)
            
            try:
                product = Product.objects.get(id=p_id)
                variant = ProductVariant.objects.get(id=v_id) if v_id else None
                
                cart_item, created = CartItem.objects.get_or_create(
                    cart=cart,
                    product=product,
                    variant=variant,
                    defaults={'quantity': qty}
                )
                if not created:
                    cart_item.quantity += qty
                    cart_item.save()
            except (Product.DoesNotExist, ProductVariant.DoesNotExist):
                continue
                
        return Response(CartSerializer(cart).data)

class WishlistView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        items = WishlistItem.objects.filter(user=request.user).select_related(
            'product', 'product__seller', 'product__seller__seller_profile'
        ).prefetch_related('product__variants', 'product__images')
        data = []
        for item in items:
            p = item.product
            variants = p.variants.all()
            images = p.images.all()
            first_variant = variants[0] if variants else None
            data.append({
                'id': str(p.id),
                'name': p.name,
                'price': str(first_variant.price) if first_variant else '0',
                'image_url': images[0].image_url if images else None,
                'seller': {
                    'id': str(p.seller.id),
                    'store_name': getattr(getattr(p.seller, 'seller_profile', None), 'store_name', p.seller.username),
                },
                'added_at': item.added_at.isoformat(),
            })
        return Response(data)

    def post(self, request):
        product_id = request.data.get('product_id')
        if not product_id:
            return Response({'error': 'product_id required'}, status=400)
        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=404)

        item, created = WishlistItem.objects.get_or_create(user=request.user, product=product)
        if not created:
            item.delete()
            return Response({'status': 'removed', 'product_id': product_id})
        return Response({'status': 'added', 'product_id': product_id}, status=201)

    def delete(self, request, product_id):
        WishlistItem.objects.filter(user=request.user, product_id=product_id).delete()
        return Response({'status': 'removed'})
