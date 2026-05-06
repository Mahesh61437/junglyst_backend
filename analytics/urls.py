from django.urls import path
from .views import AdminDashboardView, SuperAdminDashboardView, GSTDashboardView, SellerGSTDashboardView, AuthorizeGrowerView, RejectGrowerView

urlpatterns = [
    path('dashboard/', AdminDashboardView.as_view(), name='admin_dashboard'),
    path('super-admin/dashboard/', SuperAdminDashboardView.as_view(), name='super_admin_dashboard'),
    path('super-admin/gst-invoices/', GSTDashboardView.as_view(), name='super_admin_gst'),
    path('super-admin/authorize-grower/<uuid:pk>/', AuthorizeGrowerView.as_view(), name='authorize_grower'),
    path('super-admin/reject-grower/<uuid:pk>/', RejectGrowerView.as_view(), name='reject_grower'),
    path('seller/gst-invoice/', SellerGSTDashboardView.as_view(), name='seller_gst_invoice'),
]
