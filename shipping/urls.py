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

    # Webhook endpoints register both with and without trailing slash so
    # provider dashboards work either way. Django's APPEND_SLASH 301-redirect
    # downgrades POST → GET in most clients, turning real webhook deliveries
    # into 405 Method Not Allowed responses.
    path('webhook/nimbuspost/', NimbusPostWebhookView.as_view(), name='nimbuspost_webhook'),
    path('webhook/nimbuspost',  NimbusPostWebhookView.as_view()),

    # Shiprocket webhook — register this URL in your Shiprocket dashboard:
    #   https://<domain>/api/shipping/webhook/courierservice/
    #
    # NOTE: Shiprocket rejects webhook URLs that contain the words
    # "shiprocket", "kartrocket", "sr", or "kr" — so the path must
    # stay as "courierservice" (or any other neutral word).
    path('webhook/courierservice/', ShiprocketWebhookView.as_view(), name='shiprocket_webhook'),
    path('webhook/courierservice',  ShiprocketWebhookView.as_view()),
]
