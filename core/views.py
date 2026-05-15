import uuid
from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.text import slugify
from .models import Product, Category, SubCategory, CategoryShippingRate, ProductVariant, ProductImage, ProductReview, WishlistItem
from cart.models import Cart, CartItem
from orders.models import Order
from .serializers import (
    RegisterSerializer, CustomTokenObtainPairSerializer, UserSerializer,
    ProductSerializer, ProductListSerializer, ProductReviewSerializer, CategorySerializer, SubCategorySerializer,
    CategoryShippingRateSerializer, CartSerializer
)
from .storage import upload_to_firebase

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_cookie
from django.core.cache import cache
import random
from django.core.mail import send_mail
from django.conf import settings

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

class ForgotPasswordView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    COOLDOWN = 60  # seconds between allowed OTP requests

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        # 60-second cooldown — reject if a request was made too recently
        cooldown_key = f"otp_cooldown_{email}"
        if cache.get(cooldown_key):
            ttl = cache.ttl(cooldown_key) if hasattr(cache, 'ttl') else self.COOLDOWN
            return Response(
                {"error": f"Please wait {ttl} second(s) before requesting another OTP.", "retry_after": ttl},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Set cooldown even for unknown emails to prevent email enumeration via timing
            cache.set(cooldown_key, True, timeout=self.COOLDOWN)
            return Response({"message": "If an account with that email exists, an OTP has been sent."}, status=status.HTTP_200_OK)

        # Generate OTP and set both the OTP and cooldown in cache
        otp = str(random.randint(100000, 999999))
        cache.set(f"password_reset_otp_{email}", otp, timeout=600)
        cache.set(cooldown_key, True, timeout=self.COOLDOWN)

        try:
            send_mail(
                subject='Your Password Reset OTP - Junglyst',
                message=f'Your OTP for password reset is: {otp}. This code is valid for 10 minutes.',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
        except Exception:
            return Response({"error": "Failed to send email. Please try again later."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "If an account with that email exists, an OTP has been sent."}, status=status.HTTP_200_OK)

class ResetPasswordView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        email = request.data.get('email')
        otp = request.data.get('otp')
        new_password = request.data.get('new_password')

        if not all([email, otp, new_password]):
            return Response({"error": "Email, OTP, and new password are required."}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"password_reset_otp_{email}"
        cached_otp = cache.get(cache_key)

        if not cached_otp or cached_otp != str(otp):
            return Response({"error": "Invalid or expired OTP."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
            user.set_password(new_password)
            user.save()
            # Delete OTP from cache after successful reset
            cache.delete(cache_key)
            return Response({"message": "Password reset successfully."}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

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


def _product_list_queryset():
    """Lighter queryset for the shop list page"""
    return Product.objects.select_related(
        'seller', 'seller__seller_profile', 'sub_category'
    ).prefetch_related(
        'variants', 'images', 'categories'
    )


class ProductListView(generics.ListAPIView):
    serializer_class = ProductListSerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_fields = ('categories', 'sub_category', 'seller', 'is_active', 'is_rare')
    search_fields = ('name', 'tags__name', 'scientific_name')
    ordering_fields = ('created_at', 'rating')
    
    def get(self, request, *args, **kwargs):
        # We only cache the main list. If there are seller queries, we do not cache.
        if request.query_params.get('seller') or request.query_params.get('seller_slug'):
            return super().get(request, *args, **kwargs)
        
        # Manually cache the response
        from django.core.cache import cache
        from django.utils.cache import get_cache_key
        
        cache_key = get_cache_key(request)
        if cache_key:
            cached_response = cache.get(cache_key)
            if cached_response:
                return cached_response

        response = super().get(request, *args, **kwargs)
        if cache_key and response.status_code == 200:
            if hasattr(response, 'render') and callable(response.render):
                response.render()
            cache.set(cache_key, response, 60 * 60)
        return response

    def get_queryset(self):
        queryset = _product_list_queryset()

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

    def paginate_queryset(self, queryset):
        if self.request.query_params.get('no_pagination'):
            return None
        return super().paginate_queryset(queryset)

class ProductReviewListCreateView(generics.ListCreateAPIView):
    serializer_class = ProductReviewSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        queryset = ProductReview.objects.select_related('product').all()
        product_id = self.request.query_params.get('productId')
        if product_id:
            queryset = queryset.filter(product__id=product_id)
        return queryset.order_by('-created_at')

    def list(self, request, *args, **kwargs):
        product_id = request.query_params.get('productId')
        if not product_id:
            return super().list(request, *args, **kwargs)

        cache_key = f'reviews_{product_id}'
        data = cache.get(cache_key)
        if data is None:
            response = super().list(request, *args, **kwargs)
            cache.set(cache_key, response.data, 60 * 5)  # 5-minute TTL
            return response
        return Response(data)

    def perform_create(self, serializer):
        instance = serializer.save()
        # Bust the cache when a new review is posted
        cache.delete(f'reviews_{instance.product_id}')
        return instance

class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProductSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    lookup_field = 'id'

    @method_decorator(cache_page(60 * 60)) # Cache for 1 hour
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

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

class IsAdminOrSuperAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and (
            request.user.is_staff or request.user.is_superuser or
            request.user.role == 'admin'
        )


# ── Public read ───────────────────────────────────────────────────────────────

class CategoryListView(generics.ListAPIView):
    queryset = Category.objects.prefetch_related('subcategories', 'shipping_rates').all()
    serializer_class = CategorySerializer
    permission_classes = (permissions.AllowAny,)

    _CACHE_KEY = 'categories_list_v1'
    _CACHE_TTL = 60 * 60 * 24  # 24 hours

    def get(self, request, *args, **kwargs):
        # Explicit cache.get/set — cache_page is incompatible with DRF's
        # Vary: Accept header and never produces cache hits.
        data = cache.get(self._CACHE_KEY)
        if data is None:
            response = super().get(request, *args, **kwargs)
            cache.set(self._CACHE_KEY, response.data, self._CACHE_TTL)
            return response
        return Response(data)


class SubCategoryListView(generics.ListAPIView):
    queryset = SubCategory.objects.select_related('category').prefetch_related('shipping_rates').all()
    serializer_class = SubCategorySerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ('category',)


# ── Admin CRUD — Categories ───────────────────────────────────────────────────

class CategoryAdminView(generics.ListCreateAPIView):
    """GET all categories (public) / POST new category (admin)."""
    serializer_class = CategorySerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        return Category.objects.prefetch_related('subcategories', 'shipping_rates').all()

    def post(self, request, *args, **kwargs):
        self.permission_classes = (IsAdminOrSuperAdmin,)
        self.check_permissions(request)
        data = request.data.copy()
        if not data.get('slug') and data.get('name'):
            data['slug'] = slugify(data['name'])
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CategoryAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET /PUT /PATCH /DELETE a single category (admin only for write)."""
    serializer_class = CategorySerializer
    permission_classes = (IsAdminOrSuperAdmin,)

    def get_queryset(self):
        return Category.objects.prefetch_related('subcategories', 'shipping_rates').all()

    def perform_update(self, serializer):
        data = self.request.data
        if not data.get('slug') and data.get('name'):
            serializer.save(slug=slugify(data['name']))
        else:
            serializer.save()

    def perform_destroy(self, instance):
        instance.delete()  # soft-delete via SoftDeleteModel


# ── Admin CRUD — SubCategories ────────────────────────────────────────────────

class SubCategoryAdminView(generics.ListCreateAPIView):
    serializer_class = SubCategorySerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ('category',)

    def get_permissions(self):
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return [IsAdminOrSuperAdmin()]

    def get_queryset(self):
        return SubCategory.objects.select_related('category').prefetch_related('shipping_rates').all()

    def create(self, request, *args, **kwargs):
        data = request.data.copy()
        if not data.get('slug') and data.get('name'):
            base = slugify(data['name'])
            slug = base
            counter = 1
            while SubCategory.objects.filter(slug=slug).exists():
                slug = f"{base}-{counter}"
                counter += 1
            data['slug'] = slug
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SubCategoryAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = SubCategorySerializer
    permission_classes = (IsAdminOrSuperAdmin,)

    def get_queryset(self):
        return SubCategory.objects.select_related('category').prefetch_related('shipping_rates').all()

    def perform_update(self, serializer):
        data = self.request.data
        if not data.get('slug') and data.get('name'):
            serializer.save(slug=slugify(data['name']))
        else:
            serializer.save()

    def perform_destroy(self, instance):
        instance.delete()


# ── Admin CRUD — Shipping Rates ───────────────────────────────────────────────

class ShippingRateAdminView(generics.ListCreateAPIView):
    serializer_class = CategoryShippingRateSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ('category', 'sub_category')

    def get_queryset(self):
        return CategoryShippingRate.objects.select_related('category', 'sub_category').all()


class ShippingRateAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CategoryShippingRateSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
    queryset = CategoryShippingRate.objects.all()

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


class HomeDataView(generics.GenericAPIView):
    """
    Single aggregate endpoint for the home page.
    Returns featured sellers, latest 8 products, and platform stats in one round trip.
    """
    permission_classes = (permissions.AllowAny,)

    @method_decorator(cache_page(60 * 10)) # Cache for 10 minutes
    def get(self, request):
        from sellers.models import SellerProfile
        from sellers.serializers import SellerProfileSerializer
        from django.contrib.auth import get_user_model
        User = get_user_model()

        featured_sellers = SellerProfile.objects.select_related('user').filter(
            is_active=True, is_featured=True
        ).order_by('sort_order', '-rating')

        products_qs = _product_queryset().filter(is_active=True).order_by('-created_at')[:8]

        stats = {
            'total_sellers': SellerProfile.objects.filter(is_active=True).count(),
            'total_products': Product.objects.filter(is_active=True).count(),
            'total_users': User.objects.filter(is_active=True).count(),
        }

        return Response({
            'featured_sellers': SellerProfileSerializer(featured_sellers, many=True).data,
            'products': ProductSerializer(products_qs, many=True).data,
            'stats': stats,
        })
