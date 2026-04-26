from django.urls import path
from .views import (
    GrowerDashboardView, SellerStoreView, SellerProfileListView, 
    CheckSellerApprovalView, AllowedSellerListCreateView, AllowedSellerDestroyView
)

urlpatterns = [
    path('dashboard/', GrowerDashboardView.as_view(), name='grower_dashboard'),
    path('store/<slug:slug>/', SellerStoreView.as_view(), name='seller_store'),
    path('profiles/', SellerProfileListView.as_view(), name='seller_profiles'),
    path('check-approval/', CheckSellerApprovalView.as_view(), name='check_seller_approval'),
    
    # Admin curated registry management
    path('profiles/admin/allowed/', AllowedSellerListCreateView.as_view(), name='admin_allowed_list'),
    path('profiles/admin/allowed/<int:pk>/', AllowedSellerDestroyView.as_view(), name='admin_allowed_delete'),
]
