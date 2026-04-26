from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.text import slugify
from .models import Product, Category, SubCategory, ProductVariant, ProductImage
from .serializers import (
    RegisterSerializer, CustomTokenObtainPairSerializer, UserSerializer,
    ProductSerializer, CategorySerializer, SubCategorySerializer
)
from .storage import upload_to_firebase

User = get_user_model()

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (permissions.AllowAny,)
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            "user": UserSerializer(user).data,
            "message": "User created successfully",
        }, status=status.HTTP_201_CREATED)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

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
