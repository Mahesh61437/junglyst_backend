from django.urls import path
from .views import ApplyCouponView, CouponAdminListCreateView, CouponAdminDetailView

urlpatterns = [
    path('apply/', ApplyCouponView.as_view(), name='coupon_apply'),
    path('admin/', CouponAdminListCreateView.as_view(), name='coupon_admin_list'),
    path('admin/<uuid:pk>/', CouponAdminDetailView.as_view(), name='coupon_admin_detail'),
]
