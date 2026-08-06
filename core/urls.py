from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    RegisterView, CustomTokenObtainPairView, UserProfileView,
    ProductListView, ProductDetailView, ProductReviewListCreateView, CategoryListView, SubCategoryListView,
    CategoryAdminView, CategoryAdminDetailView,
    SubCategoryAdminView, SubCategoryAdminDetailView,
    ShippingRateAdminView, ShippingRateAdminDetailView,
    ImageUploadView, GrowerProductCreateView, AdminProductCreateView,
    ProductCopyView, ProductBulkActionView, BulkStockUpdateView, SyncCartView, WishlistView, HomeDataView,
    ForgotPasswordView, ResetPasswordView,
    BugReportListCreateView, BugReportDetailView,
    PublicConfigurationView, ConfigurationAdminView, ConfigurationAdminDetailView,
    FeatureFlagsView, ProductRecommendationsView,
)

urlpatterns = [
    # Auth
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('forgot-password/', ForgotPasswordView.as_view(), name='forgot_password'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset_password'),
    path('me/', UserProfileView.as_view(), name='user_profile'),
    path('sync-cart/', SyncCartView.as_view(), name='sync_cart'),

    # Discovery (public)
    path('products/', ProductListView.as_view(), name='product_list'),
    path('products/create/', GrowerProductCreateView.as_view(), name='grower_product_create'),
    path('products/admin-create/', AdminProductCreateView.as_view(), name='admin_product_create'),
    path('products/bulk-action/', ProductBulkActionView.as_view(), name='product_bulk_action'),
    path('products/bulk-stock-update/', BulkStockUpdateView.as_view(), name='product_bulk_stock_update'),
    path('products/id/<uuid:id>/copy/', ProductCopyView.as_view(), name='product_copy'),
    path('products/id/<uuid:id>/', ProductDetailView.as_view(), name='product_detail_id'),
    path('products/<slug:slug>/recommendations/', ProductRecommendationsView.as_view(), name='product_recommendations'),
    path('products/<slug:slug>/', ProductDetailView.as_view(lookup_field='slug'), name='product_detail_slug'),
    path('reviews/', ProductReviewListCreateView.as_view(), name='product_reviews'),

    # Categories — public list + admin CRUD
    path('categories/', CategoryAdminView.as_view(), name='category_list'),
    path('categories/<int:pk>/', CategoryAdminDetailView.as_view(), name='category_detail'),
    path('subcategories/', SubCategoryAdminView.as_view(), name='subcategory_list'),
    path('subcategories/<int:pk>/', SubCategoryAdminDetailView.as_view(), name='subcategory_detail'),

    # Shipping rates — admin CRUD
    path('shipping-rates/', ShippingRateAdminView.as_view(), name='shipping_rate_list'),
    path('shipping-rates/<int:pk>/', ShippingRateAdminDetailView.as_view(), name='shipping_rate_detail'),

    # Utilities
    path('upload/', ImageUploadView.as_view(), name='image_upload'),

    # Wishlist
    path('wishlist/', WishlistView.as_view(), name='wishlist'),
    path('wishlist/<uuid:product_id>/', WishlistView.as_view(), name='wishlist_remove'),

    # Home page aggregate
    path('home/', HomeDataView.as_view(), name='home_data'),

    # Bug Reports
    path('bug-reports/', BugReportListCreateView.as_view(), name='bug_report_list'),
    path('bug-reports/<uuid:pk>/', BugReportDetailView.as_view(), name='bug_report_detail'),

    # Feature flags — public read of off-switches for half-built modules
    path('features/', FeatureFlagsView.as_view(), name='feature_flags'),

    # Configuration — generic key/value settings
    path('config/public/<str:name>/', PublicConfigurationView.as_view(), name='config_public'),
    path('config/', ConfigurationAdminView.as_view(), name='config_list'),
    path('config/<str:name>/', ConfigurationAdminDetailView.as_view(), name='config_detail'),
]
