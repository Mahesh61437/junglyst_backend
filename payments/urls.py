from django.urls import path
from .views import CashfreeWebhookView, PaymentGatewaySettingsView

urlpatterns = [
    path('webhook/cashfree/', CashfreeWebhookView.as_view(), name='cashfree_webhook'),
    path('gateway-settings/', PaymentGatewaySettingsView.as_view(), name='payment_gateway_settings'),
]
