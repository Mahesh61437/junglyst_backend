from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ShippingAddressViewSet, LogisticsViewSet, LogisticsProviderSettingsView,
    PackageImageUploadView, PincodeCheckView, NimbusPostWebhookView, ShiprocketWebhookView,
)

router = DefaultRouter()
router.register(r'addresses', ShippingAddressViewSet, basename='address')
router.register(r'logistics', LogisticsViewSet, basename='logistics')

urlpatterns = [
    path('', include(router.urls)),
    path('provider-settings/', LogisticsProviderSettingsView.as_view(), name='logistics_provider_settings'),
    path('package-image/', PackageImageUploadView.as_view(), name='package_image_upload'),
    path('pincode-check/', PincodeCheckView.as_view(), name='pincode_check'),
    path('webhook/nimbuspost/', NimbusPostWebhookView.as_view(), name='nimbuspost_webhook'),
    path('webhook/courierservice/', ShiprocketWebhookView.as_view(), name='shiprocket_webhook'),
]
