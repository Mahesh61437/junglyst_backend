from django.urls import path
from .views import CheckoutView, VerifyPaymentView, OrderListView

urlpatterns = [
    path('', OrderListView.as_view(), name='order_list'),
    path('checkout/', CheckoutView.as_view(), name='checkout'),
    path('checkout/verify/', VerifyPaymentView.as_view(), name='verify_payment'),
]
