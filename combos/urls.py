from django.urls import path

from .views import (
    ComboListView, ComboDetailView,
    ComboAdminListCreateView, ComboAdminDetailView,
)

urlpatterns = [
    # Admin routes must precede the <slug:slug>/ catch-all below, otherwise
    # 'admin/' would be swallowed as a slug value.
    path('admin/', ComboAdminListCreateView.as_view(), name='combo-admin-list'),
    path('admin/<uuid:id>/', ComboAdminDetailView.as_view(), name='combo-admin-detail'),

    path('', ComboListView.as_view(), name='combo-list'),
    path('<slug:slug>/', ComboDetailView.as_view(), name='combo-detail'),
]
