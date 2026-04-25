from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ShippingAddressViewSet, LogisticsViewSet

router = DefaultRouter()
router.register(r'addresses', ShippingAddressViewSet, basename='address')
router.register(r'logistics', LogisticsViewSet, basename='logistics')

urlpatterns = [
    path('', include(router.urls)),
]
