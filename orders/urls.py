from django.urls import path
from .views import CheckoutView, VerifyPaymentView, OrderListView, OrderDetailView, SellerOrderListView, ShipNowView

urlpatterns = [
    path('', OrderListView.as_view(), name='order_list'),
    path('<int:pk>/', OrderDetailView.as_view(), name='order_detail'),
    path('checkout/', CheckoutView.as_view(), name='checkout'),
    path('checkout/verify/', VerifyPaymentView.as_view(), name='verify_payment'),
    path('seller/', SellerOrderListView.as_view(), name='seller_order_list'),
    path('ship-now/', ShipNowView.as_view(), name='ship_now'),
]
