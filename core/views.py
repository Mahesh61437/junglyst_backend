from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.text import slugify
from .models import Product, Category, SubCategory, ProductVariant, ProductImage
from cart.models import Cart, CartItem
from orders.models import Order
from .serializers import (
    RegisterSerializer, CustomTokenObtainPairSerializer, UserSerializer,
    ProductSerializer, CategorySerializer, SubCategorySerializer, CartSerializer
)
from .storage import upload_to_firebase
import random
from django.core.cache import cache
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

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Return success even if user doesn't exist for security reasons (prevent email enumeration)
            return Response({"message": "If an account with that email exists, an OTP has been sent."}, status=status.HTTP_200_OK)

        # Generate OTP
        otp = str(random.randint(100000, 999999))
        
        # Save to cache (valid for 10 minutes)
        cache_key = f"password_reset_otp_{email}"
        cache.set(cache_key, otp, timeout=600)

        # Send Email
        try:
            send_mail(
                subject='Your Password Reset OTP - JungLyst',
                message=f'Your OTP for password reset is: {otp}. This code is valid for 10 minutes.',
                from_email=settings.EMAIL_HOST_USER or 'noreply@junglyst.com',
                recipient_list=[email],
                fail_silently=False,
            )
        except Exception as e:
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

class ProductListView(generics.ListAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_fields = ('categories', 'sub_category', 'seller', 'is_active', 'is_rare')
    search_fields = ('name', 'description', 'tags__name')
    ordering_fields = ('created_at', 'rating')

    def get_queryset(self):
        queryset = super().get_queryset()
        seller_slug = self.request.query_params.get('seller_slug')
        if seller_slug:
            queryset = queryset.filter(seller__seller_profile__slug=seller_slug)

        # Only show active products unless the seller is explicitly filtering their own
        # (sellers pass seller=<their_id> param from the dashboard)
        seller_id = self.request.query_params.get('seller')
        is_active_param = self.request.query_params.get('is_active')
        if seller_id and is_active_param is None:
            # Seller viewing their own products: show everything (active + archived)
            pass
        else:
            # Public marketplace: only show active products by default
            if is_active_param is None:
                queryset = queryset.filter(is_active=True)

        return queryset

class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    lookup_field = 'id' 

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
