from django.urls import path
from .views import (
    GrowerDashboardView, SellerStoreView, SellerProfileListView,
    CheckSellerApprovalView, CheckEmailAllowedView, AllowedSellerListCreateView, AllowedSellerDestroyView,
    PlatformStatsView, FeaturedCuratorView,
    SellerPromotionView, AdminSellerProfileEditView, BankDetailsView,
    SellerShippingConfigListCreateView, SellerShippingConfigDetailView,
)

urlpatterns = [
    path('dashboard/', GrowerDashboardView.as_view(), name='grower_dashboard'),
    path('store/<slug:slug>/', SellerStoreView.as_view(), name='seller_store'),

    # Main seller list — supports ?featured=true to filter by is_featured, ordered by sort_order
    path('', SellerProfileListView.as_view(), name='seller_list'),
    path('profiles/', SellerProfileListView.as_view(), name='seller_profiles_list'),

    path('bank-details/', BankDetailsView.as_view(), name='bank_details'),
    path('check-approval/', CheckSellerApprovalView.as_view(), name='check_seller_approval'),
    path('check-email/', CheckEmailAllowedView.as_view(), name='check_email_allowed'),
    path('platform-stats/', PlatformStatsView.as_view(), name='platform_stats'),
    path('featured-curator/', FeaturedCuratorView.as_view(), name='featured-curator'),

    # Admin: seller promotion control
    path('profiles/<int:pk>/promote/', SellerPromotionView.as_view(), name='seller_promote'),
    path('profiles/<int:pk>/admin-edit/', AdminSellerProfileEditView.as_view(), name='admin_seller_edit'),
    path('profiles/admin/allowed/', AllowedSellerListCreateView.as_view(), name='admin_allowed_list'),
    path('profiles/admin/allowed/<int:pk>/', AllowedSellerDestroyView.as_view(), name='admin_allowed_delete'),

    # Superadmin: per-seller shipping fee tier management
    path('shipping-configs/', SellerShippingConfigListCreateView.as_view(), name='shipping_config_list'),
    path('shipping-configs/<int:pk>/', SellerShippingConfigDetailView.as_view(), name='shipping_config_detail'),
]
