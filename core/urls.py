from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    RegisterView, CustomTokenObtainPairView, UserProfileView,
    ProductListView, ProductDetailView, CategoryListView, SubCategoryListView,
    ImageUploadView, GrowerProductCreateView
)

urlpatterns = [
    # Auth
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', UserProfileView.as_view(), name='user_profile'),
    
    # Discovery
    path('products/', ProductListView.as_view(), name='product_list'),
    path('products/create/', GrowerProductCreateView.as_view(), name='grower_product_create'),
    path('products/id/<uuid:id>/', ProductDetailView.as_view(), name='product_detail_id'),
    path('products/<slug:slug>/', ProductDetailView.as_view(lookup_field='slug'), name='product_detail_slug'),
    path('categories/', CategoryListView.as_view(), name='category_list'),
    path('subcategories/', SubCategoryListView.as_view(), name='subcategory_list'),
    
    # Utilities
    path('upload/', ImageUploadView.as_view(), name='image_upload'),
]
