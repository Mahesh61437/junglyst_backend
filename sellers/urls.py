from django.urls import path
from .views import (
    GrowerDashboardView, SellerStoreView, SellerProfileListView,
    CheckSellerApprovalView, AllowedSellerListCreateView, AllowedSellerDestroyView,
    PlatformStatsView, VerifiedCuratorDirectoryView, FeaturedCuratorView,
    SellerPromotionView, AdminSellerProfileEditView,
)

urlpatterns = [
    path('dashboard/', GrowerDashboardView.as_view(), name='grower_dashboard'),
    path('store/<slug:slug>/', SellerStoreView.as_view(), name='seller_store'),
    path('profiles/', SellerProfileListView.as_view(), name='seller_profiles'),
    path('verified-directory/', VerifiedCuratorDirectoryView.as_view(), name='verified-directory'),
    path('featured-curator/', FeaturedCuratorView.as_view(), name='featured-curator'),
    path('check-approval/', CheckSellerApprovalView.as_view(), name='check_seller_approval'),
    path('platform-stats/', PlatformStatsView.as_view(), name='platform_stats'),

    # Admin: seller promotion control
    path('profiles/<int:pk>/promote/', SellerPromotionView.as_view(), name='seller_promote'),
    path('profiles/<int:pk>/admin-edit/', AdminSellerProfileEditView.as_view(), name='admin_seller_edit'),

    # Admin curated registry management
    path('profiles/admin/allowed/', AllowedSellerListCreateView.as_view(), name='admin_allowed_list'),
    path('profiles/admin/allowed/<int:pk>/', AllowedSellerDestroyView.as_view(), name='admin_allowed_delete'),
]
