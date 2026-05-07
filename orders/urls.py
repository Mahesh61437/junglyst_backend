from django.urls import path
from .views import (
    CheckoutView, VerifyPaymentView, OrderListView, OrderDetailView,
    SellerOrderListView, ShipNowView,
    SellerSubOrderListView, ConfirmSubOrderView, UploadPackagingPhotoView,
    UpdateShipmentDetailsView, SubOrderShipView, UpdateSubOrderStatusView,
)

urlpatterns = [
    path('', OrderListView.as_view(), name='order_list'),
    path('<uuid:pk>/', OrderDetailView.as_view(), name='order_detail'),
    path('checkout/', CheckoutView.as_view(), name='checkout'),
    path('checkout/verify/', VerifyPaymentView.as_view(), name='verify_payment'),
    path('seller/', SellerOrderListView.as_view(), name='seller_order_list'),
    path('seller/sub-orders/', SellerSubOrderListView.as_view(), name='seller_suborder_list'),
    path('seller/sub-orders/<uuid:pk>/confirm/', ConfirmSubOrderView.as_view(), name='suborder_confirm'),
    path('seller/sub-orders/<uuid:pk>/upload-photo/', UploadPackagingPhotoView.as_view(), name='suborder_upload_photo'),
    path('seller/sub-orders/<uuid:pk>/shipment-details/', UpdateShipmentDetailsView.as_view(), name='suborder_shipment_details'),
    path('seller/sub-orders/<uuid:pk>/ship/', SubOrderShipView.as_view(), name='suborder_ship'),
    path('seller/sub-orders/<uuid:pk>/status/', UpdateSubOrderStatusView.as_view(), name='suborder_update_status'),
    path('ship-now/', ShipNowView.as_view(), name='ship_now'),
]
