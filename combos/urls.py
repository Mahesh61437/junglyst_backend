from django.urls import path

from .views import ComboListView, ComboDetailView

urlpatterns = [
    path('', ComboListView.as_view(), name='combo-list'),
    path('<slug:slug>/', ComboDetailView.as_view(), name='combo-detail'),
]
