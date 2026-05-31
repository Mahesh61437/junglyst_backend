import uuid
from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.text import slugify
from .models import Product, Category, SubCategory, CategoryShippingRate, ProductVariant, ProductImage, ProductReview, WishlistItem, BugReport, Configuration
from cart.models import Cart, CartItem
from orders.models import Order
from .serializers import (
    RegisterSerializer, CustomTokenObtainPairSerializer, UserSerializer,
    ProductSerializer, ProductListSerializer, ProductReviewSerializer, CategorySerializer, SubCategorySerializer,
    CategoryShippingRateSerializer, CartSerializer, BugReportSerializer, ConfigurationSerializer
)
from .storage import upload_to_firebase

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_cookie
from django.core.cache import cache
import random
from django.core.mail import send_mail
from django.conf import settings
from .feed import (
    get_ordered_product_ids, get_filtered_ordered_ids,
    get_sorted_feed, compute_sorted_feed, SORTABLE_FIELDS,
)

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
    ).prefetch_related(
        'variants', 'images', 'categories',
        'categories__subcategories', 'tags', 'sub_categories', 'sub_categories__category'
    )


def _product_list_queryset():
    """Lighter queryset for the shop list page"""
    return Product.objects.select_related(
        'seller', 'seller__seller_profile'
    ).prefetch_related(
        'variants', 'images', 'categories',
        'sub_categories', 'sub_categories__category',
    )


class ProductListView(generics.ListAPIView):
    serializer_class = ProductListSerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_fields = ('categories', 'sub_categories', 'seller', 'is_active', 'is_rare')
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
        params = self.request.query_params

        seller_slug = params.get('seller_slug')
        if seller_slug:
            queryset = queryset.filter(seller__seller_profile__slug=seller_slug)

        seller_id = params.get('seller')
        is_active_param = params.get('is_active')
        is_draft_param = params.get('is_draft')

        if seller_id:
            # Seller-scoped: respect explicit filters
            if is_active_param is not None and is_active_param != 'all':
                queryset = queryset.filter(is_active=is_active_param in ('true', '1', 'True'))
            if is_draft_param is not None:
                queryset = queryset.filter(is_draft=is_draft_param in ('true', '1', 'True'))
            # If is_active='all' is passed, show everything (drafts + published + archived)
        else:
            # Public shop: only published, non-draft products
            if is_active_param is None:
                queryset = queryset.filter(is_active=True, is_draft=False)

        # In-stock filter
        if params.get('in_stock') in ('true', '1', 'True'):
            queryset = queryset.filter(variants__stock__gt=0).distinct()

        # Low stock filter
        stock_lt = params.get('stock_lt')
        if stock_lt is not None:
            try:
                queryset = queryset.filter(variants__stock__lt=int(stock_lt)).distinct()
            except ValueError:
                pass

        # Price range filter (based on first variant price)
        min_price = params.get('min_price')
        max_price = params.get('max_price')
        if min_price:
            try:
                queryset = queryset.filter(variants__price__gte=float(min_price)).distinct()
            except ValueError:
                pass
        if max_price:
            try:
                queryset = queryset.filter(variants__price__lte=float(max_price)).distinct()
            except ValueError:
                pass

        # Subcategory filter
        sub_cat_id = params.get('sub_category_id')
        if sub_cat_id:
            queryset = queryset.filter(sub_categories__id=sub_cat_id).distinct()

        # Category filter by name (comma-separated for multi-select)
        category_name = params.get('category')
        if category_name:
            names = [n.strip() for n in category_name.split(',') if n.strip()]
            queryset = queryset.filter(categories__name__in=names).distinct()

        # Care level filter (comma-separated, e.g. "Easy,Medium")
        care_level = params.get('care_level')
        if care_level:
            levels = [l.strip() for l in care_level.split(',') if l.strip()]
            queryset = queryset.filter(care_level__in=levels)

        # Annotate with has_stock so filter_queryset can use it for ordering
        from django.db.models import Exists, OuterRef
        queryset = queryset.annotate(
            has_stock=Exists(ProductVariant.objects.filter(product=OuterRef('pk'), stock__gt=0))
        )

        return queryset

    # Query params that narrow the product set and require ID filtering.
    # 'category'      → filter by Category name  (custom, comma-separated)
    # 'categories'    → filter by Category ID     (DjangoFilterBackend)
    # 'sub_categories'→ filter by SubCategory ID  (DjangoFilterBackend)
    # 'sub_category_id'→ filter by SubCategory ID (custom)
    _FILTER_PARAMS = frozenset({
        'category', 'categories', 'sub_categories', 'sub_category_id',
        'care_level', 'in_stock', 'min_price', 'max_price', 'search', 'is_rare',
        'stock_lt',
    })

    def filter_queryset(self, queryset):
        """In-stock first; seller-fair cached feed for the public shop."""
        qs = super().filter_queryset(queryset)
        params = self.request.query_params

        ordering_param = params.get('ordering', '').strip()

        # 1. Seller-scoped pages (seller dashboard / storefront) — newest first
        if params.get('seller') or params.get('seller_slug'):
            return qs.order_by('-has_stock', '-created_at')

        # 2. Explicit sort requested by the user
        if ordering_param:
            if ordering_param in SORTABLE_FIELDS:
                # Use pre-warmed sorted+fair feed (seller-fair tiebreaking within
                # equal-value groups, e.g. 1521 products all rated 5.0)
                ordered_ids = get_sorted_feed(ordering_param)
                if ordered_ids is None:
                    # Cache cold — compute on the fly and store
                    master = get_ordered_product_ids()
                    ordered_ids = compute_sorted_feed(ordering_param, master)
                # Narrow to filtered subset if any filters are active
                active_filters = {k: params[k] for k in self._FILTER_PARAMS if params.get(k)}
                if active_filters:
                    valid_ids = frozenset(qs.values_list('id', flat=True))
                    ordered_ids = [i for i in ordered_ids if i in valid_ids]
                self._preordered_ids = ordered_ids
                return qs
            else:
                # Unknown sort field — fall back to DB ordering (seller dashboard edge cases)
                secondary = [f.strip() for f in ordering_param.split(',')]
                if any(f.lstrip('-') == 'price' for f in secondary):
                    from django.db.models import Min
                    qs = qs.annotate(price=Min('variants__price'))
                return qs.order_by('-has_stock', *secondary)

        # 3. Default public shop browse — seller-fair feed
        active_filters = {k: params[k] for k in self._FILTER_PARAMS if params.get(k)}
        if active_filters:
            ordered_ids = get_filtered_ordered_ids(active_filters, qs)
        else:
            ordered_ids = get_ordered_product_ids()

        self._preordered_ids = ordered_ids
        return qs

    def paginate_queryset(self, queryset):
        if self.request.query_params.get('no_pagination'):
            return None

        if hasattr(self, '_preordered_ids'):
            # Drop cached IDs that no longer match the live queryset (e.g. products
            # deactivated/deleted since the Redis master feed was built). Without
            # this, DRF reports count=N but the page-1 slice can return [] when the
            # first page_size IDs all happen to be stale.
            import uuid
            self._preordered_ids = [
                uuid.UUID(pid) if isinstance(pid, str) else pid
                for pid in self._preordered_ids
            ]
            valid_ids = frozenset(queryset.values_list('id', flat=True))
            self._preordered_ids = [pid for pid in self._preordered_ids if pid in valid_ids]

            # Paginate the Python ID list — DRF uses len() for total count
            page_ids = self.paginator.paginate_queryset(
                self._preordered_ids, self.request, view=self
            )
            if page_ids is None:
                return None
            # Fetch only these page_size products by primary key (indexed, fast)
            id_to_pos = {pid: idx for idx, pid in enumerate(page_ids)}
            products = list(queryset.filter(id__in=page_ids))
            products.sort(key=lambda p: id_to_pos[p.id])
            return products

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

    # ------------------------------------------------------------------ cache
    _DETAIL_TTL    = 60 * 60 * 12   # 12 h — signals invalidate on any change

    @staticmethod
    def _detail_key(product_id: str) -> str:
        return f'product:detail:{product_id}'

    @staticmethod
    def _slug_key(slug: str) -> str:
        return f'product:slug_to_id:{slug}'

    # ------------------------------------------------------------------ GET
    def get(self, request, *args, **kwargs):
        # Authenticated users (sellers, admins) always get fresh data
        if request.user.is_authenticated:
            return super().get(request, *args, **kwargs)

        product_id = str(kwargs.get('id', ''))
        slug       = kwargs.get('slug', '')

        # Resolve slug → id via a tiny cache entry so we reuse the same data key
        if not product_id and slug:
            product_id = cache.get(self._slug_key(slug), '')

        # Try the data cache
        if product_id:
            cached = cache.get(self._detail_key(product_id))
            if cached is not None:
                return Response(cached)

        # Cache miss — hit the DB
        product    = self.get_object()
        serializer = self.get_serializer(product)
        data       = serializer.data

        pid = str(product.id)
        cache.set(self._detail_key(pid), data, self._DETAIL_TTL)
        if product.slug:
            cache.set(self._slug_key(product.slug), pid, self._DETAIL_TTL)

        return Response(data)

    # ------------------------------------------------------------------ write
    def get_queryset(self):
        return _product_queryset()

    def get_permissions(self):
        if self.request.method in ['PUT', 'PATCH', 'DELETE']:
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def perform_update(self, serializer):
        product = self.get_object()
        if self.request.user != product.seller and self.request.user.role != 'admin':
            raise permissions.PermissionDenied("You do not have permission to edit this specimen.")
        instance = serializer.save()
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

        new_product.sub_categories.set(original.sub_categories.all())

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
    serializer_class = CategorySerializer
    permission_classes = (permissions.AllowAny,)
    pagination_class = None  # always return all categories in one response

    _CACHE_KEY = 'categories_list_v2'
    _CACHE_TTL = 60 * 60 * 24  # 24 hours

    def get_queryset(self):
        # select_related('category') on subcategories eliminates N+1 queries caused by
        # SubCategory.effective_gst and .effective_commission accessing the parent FK.
        from django.db.models import Prefetch
        from .models import SubCategory, CategoryShippingRate
        return Category.objects.prefetch_related(
            Prefetch(
                'subcategories',
                queryset=SubCategory.objects.select_related('category').prefetch_related('shipping_rates'),
            ),
            'shipping_rates',
        ).all()

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
        from django.db.models import Prefetch
        from .models import SubCategory
        return Category.objects.prefetch_related(
            Prefetch(
                'subcategories',
                queryset=SubCategory.objects.select_related('category').prefetch_related('shipping_rates'),
            ),
            'shipping_rates',
        ).all()

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
        from django.db.models import Prefetch
        from .models import SubCategory
        return Category.objects.prefetch_related(
            Prefetch(
                'subcategories',
                queryset=SubCategory.objects.select_related('category').prefetch_related('shipping_rates'),
            ),
            'shipping_rates',
        ).all()

    def perform_update(self, serializer):
        data = self.request.data
        if not data.get('slug') and data.get('name'):
            serializer.save(slug=slugify(data['name']))
        else:
            serializer.save()
        cache.delete(CategoryListView._CACHE_KEY)

    def perform_destroy(self, instance):
        instance.delete()
        cache.delete(CategoryListView._CACHE_KEY)


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
        cache.delete(CategoryListView._CACHE_KEY)
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
        cache.delete(CategoryListView._CACHE_KEY)

    def perform_destroy(self, instance):
        instance.delete()
        cache.delete(CategoryListView._CACHE_KEY)


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


# ── Configuration — generic key/value settings ────────────────────────────────

# Names that are safe to read without authentication (frontend may need them
# to render public-facing copy like the competition result-announcement date).
PUBLIC_CONFIGURATION_NAMES = {
    'competition_settings',
}


class PublicConfigurationView(generics.GenericAPIView):
    """Public read of a whitelisted configuration entry by name."""
    permission_classes = (permissions.AllowAny,)
    serializer_class = ConfigurationSerializer

    def get(self, request, name):
        if name not in PUBLIC_CONFIGURATION_NAMES:
            return Response({'error': 'Configuration not publicly accessible.'}, status=404)
        try:
            config = Configuration.objects.get(name=name)
        except Configuration.DoesNotExist:
            return Response({'name': name, 'data': {}}, status=200)
        return Response(ConfigurationSerializer(config).data)


class ConfigurationAdminView(generics.ListCreateAPIView):
    """List or create configuration entries. Super-admin only."""
    serializer_class = ConfigurationSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
    queryset = Configuration.objects.all().order_by('name')


class ConfigurationAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve/update/delete a configuration by name. Super-admin only."""
    serializer_class = ConfigurationSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
    queryset = Configuration.objects.all()
    lookup_field = 'name'


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
        except Exception:
            # Firebase exceptions can include service-account paths or bucket
            # internals — never echo them to the client.
            import logging
            logging.getLogger(__name__).exception("Firebase image upload failed for user %s", request.user.id)
            return Response({"error": "Image upload failed. Please try again."}, status=500)

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


class ProductBulkActionView(generics.GenericAPIView):
    """
    POST /core/products/bulk-action/
    Body: { "action": "publish"|"archive"|"unarchive"|"delete", "ids": ["uuid1", ...] }
    Sellers can only act on their own products. Admins can act on any.
    """
    permission_classes = (permissions.IsAuthenticated,)

    VALID_ACTIONS = {'publish', 'archive', 'unarchive', 'delete'}

    def post(self, request):
        action = request.data.get('action')
        ids = request.data.get('ids', [])

        if not action or action not in self.VALID_ACTIONS:
            return Response({'error': f'action must be one of: {", ".join(self.VALID_ACTIONS)}'}, status=400)
        if not ids:
            return Response({'error': 'ids list is required'}, status=400)

        user = request.user
        is_admin = user.is_staff or user.is_superuser or user.role == 'admin'

        qs = Product.objects.filter(id__in=ids)
        if not is_admin:
            qs = qs.filter(seller=user)

        count = qs.count()
        if count == 0:
            return Response({'error': 'No matching products found'}, status=404)

        if action == 'publish':
            qs.update(is_active=True, is_draft=False)
        elif action == 'archive':
            ProductVariant.objects.filter(product_id__in=ids).update(stock=0)
            qs.update(is_active=False, is_draft=False)
        elif action == 'unarchive':
            qs.update(is_active=True, is_draft=False)
        elif action == 'delete':
            qs.update(is_deleted=True)

        return Response({'success': True, 'affected': count, 'action': action})


class BulkStockUpdateView(generics.GenericAPIView):
    """
    POST /core/products/bulk-stock-update/
    Body: { "updates": [{ "variant_id": "<uuid>", "stock": <int> }, ...] }
    Updates stock for many variants (across many products) in a single request.
    Sellers can only update variants on their own products. Admins can update any.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        updates = request.data.get('updates', [])
        if not isinstance(updates, list) or not updates:
            return Response({'error': 'updates must be a non-empty list'}, status=400)

        # Normalise + validate payload
        normalised = {}  # variant_id -> stock
        for entry in updates:
            vid = entry.get('variant_id') or entry.get('id')
            stock = entry.get('stock')
            if not vid:
                return Response({'error': 'Each update needs variant_id'}, status=400)
            try:
                stock_int = int(stock)
            except (TypeError, ValueError):
                return Response({'error': f'Invalid stock for variant {vid}'}, status=400)
            if stock_int < 0:
                return Response({'error': f'Stock must be >= 0 for variant {vid}'}, status=400)
            normalised[str(vid)] = stock_int

        user = request.user
        is_admin = user.is_staff or user.is_superuser or getattr(user, 'role', None) == 'admin'

        qs = ProductVariant.objects.filter(id__in=list(normalised.keys()))
        if not is_admin:
            qs = qs.filter(product__seller=user)

        # Detect ownership / not-found mismatches
        found_ids = set(str(v_id) for v_id in qs.values_list('id', flat=True))
        missing = [vid for vid in normalised.keys() if vid not in found_ids]
        if missing:
            return Response(
                {'error': 'Some variants were not found or not owned by you', 'missing': missing},
                status=404,
            )

        # Persist
        updated = 0
        for variant in qs:
            variant.stock = normalised[str(variant.id)]
            variant.save(update_fields=['stock'])
            updated += 1

        return Response({'success': True, 'updated': updated})


class HomeDataView(generics.GenericAPIView):
    """
    Single aggregate endpoint for the home page.
    Returns featured sellers, latest 8 products, and platform stats in one round trip.
    """
    permission_classes = (permissions.AllowAny,)

    _CACHE_KEY = 'home_data_v2'
    _CACHE_TTL = 60 * 10  # 10 minutes

    def get(self, request):
        from sellers.models import SellerProfile
        from sellers.serializers import SellerProfileSerializer
        from django.contrib.auth import get_user_model
        User = get_user_model()

        cached = cache.get(self._CACHE_KEY)
        if cached is not None:
            return Response(cached)

        featured_sellers = SellerProfile.objects.select_related('user').filter(
            is_active=True, is_featured=True
        ).order_by('sort_order', '-rating')

        home_ids = get_ordered_product_ids()[:8]
        id_to_pos = {pid: idx for idx, pid in enumerate(home_ids)}
        _home_products = list(
            _product_list_queryset().filter(is_active=True, is_draft=False, id__in=home_ids)
        )
        _home_products.sort(key=lambda p: id_to_pos.get(p.id, 999))
        products_qs = _home_products

        stats = {
            'total_sellers': SellerProfile.objects.filter(is_active=True).count(),
            'total_products': Product.objects.filter(is_active=True, is_draft=False).count(),
            'total_users': User.objects.filter(is_active=True).count(),
        }

        data = {
            'featured_sellers': SellerProfileSerializer(featured_sellers, many=True).data,
            'products': ProductListSerializer(products_qs, many=True).data,
            'stats': stats,
        }
        cache.set(self._CACHE_KEY, data, self._CACHE_TTL)
        return Response(data)

from django.http import HttpResponse

def robots_txt(request):
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:5173').rstrip('/')
    lines = [
        "User-agent: *",
        "Disallow: /checkout/",
        "Disallow: /cart/",
        "Disallow: /account/",
        "Disallow: /search?q=*",
        "Disallow: /api/",
        "",
        f"Sitemap: {frontend_url}/sitemap.xml"
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

def sitemap_xml(request):
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:5173').rstrip('/')
    
    products = Product.objects.filter(is_active=True).order_by('-created_at')[:1000] # Limit to 1000 for simplicity
    
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    
    for product in products:
        slug = product.slug or product.id
        date = product.updated_at.strftime('%Y-%m-%d') if hasattr(product, 'updated_at') else (product.created_at.strftime('%Y-%m-%d') if hasattr(product, 'created_at') else '2023-10-25')
        xml.append(f"""   <url>
      <loc>{frontend_url}/product/{slug}</loc>
      <lastmod>{date}</lastmod>
      <changefreq>weekly</changefreq>
      <priority>0.8</priority>
   </url>""")
        
    xml.append('</urlset>')
    return HttpResponse("\n".join(xml), content_type="application/xml")

class BugReportListCreateView(generics.ListCreateAPIView):
    queryset = BugReport.objects.all()
    serializer_class = BugReportSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated and (user.is_staff or user.role == 'admin' or user.is_superuser):
            return super().get_queryset()
        return BugReport.objects.none()

    def create(self, request, *args, **kwargs):
        data = request.data.copy()
        
        # Handle up to 5 images
        images = []
        user_id = request.user.id if request.user.is_authenticated else 'guest'
        
        for i in range(5):
            file_obj = request.FILES.get(f'image_{i}')
            if file_obj:
                try:
                    url = upload_to_firebase(file_obj, user_id, 'bug_report')
                    images.append(url)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).exception("BugReport image upload failed")
        
        # We need to parse description and contact_info from request.data
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        
        # Save images in serializer's validated_data or just save it directly
        serializer.validated_data['images'] = images
        
        if request.user.is_authenticated:
            serializer.save(user=request.user, images=images)
        else:
            serializer.save(images=images)
            
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

class BugReportDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BugReport.objects.all()
    serializer_class = BugReportSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
