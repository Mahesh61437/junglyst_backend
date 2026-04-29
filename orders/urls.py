from django.urls import path
from .views import CheckoutView, VerifyPaymentView, OrderListView, OrderDetailView

urlpatterns = [
    path('', OrderListView.as_view(), name='order_list'),
    path('<int:pk>/', OrderDetailView.as_view(), name='order_detail'),
    path('checkout/', CheckoutView.as_view(), name='checkout'),
    path('checkout/verify/', VerifyPaymentView.as_view(), name='verify_payment'),
]
