from django.urls import path
from .views import GrowerDashboardView, SellerStoreView, SellerProfileListView

urlpatterns = [
    path('dashboard/', GrowerDashboardView.as_view(), name='grower_dashboard'),
    path('store/<slug:slug>/', SellerStoreView.as_view(), name='seller_store'),
    path('profiles/', SellerProfileListView.as_view(), name='seller_profiles'),
]
