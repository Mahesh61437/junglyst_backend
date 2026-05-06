from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ShippingAddressViewSet, LogisticsViewSet, PackageImageUploadView

router = DefaultRouter()
router.register(r'addresses', ShippingAddressViewSet, basename='address')
router.register(r'logistics', LogisticsViewSet, basename='logistics')

urlpatterns = [
    path('', include(router.urls)),
    path('package-image/', PackageImageUploadView.as_view(), name='package_image_upload'),
]
