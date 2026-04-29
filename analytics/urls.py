from django.urls import path
from .views import AdminDashboardView, SuperAdminDashboardView, GSTDashboardView, SellerGSTDashboardView

urlpatterns = [
    path('dashboard/', AdminDashboardView.as_view(), name='admin_dashboard'),
    path('super-admin/dashboard/', SuperAdminDashboardView.as_view(), name='super_admin_dashboard'),
    path('super-admin/gst-invoices/', GSTDashboardView.as_view(), name='super_admin_gst'),
    path('seller/gst-invoice/', SellerGSTDashboardView.as_view(), name='seller_gst_invoice'),
]
