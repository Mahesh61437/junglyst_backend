from django.urls import path
from .views import CashfreeWebhookView, RazorpayWebhookView, PaymentGatewaySettingsView

# Webhook endpoints are registered with AND without a trailing slash.
# Django's default APPEND_SLASH=True 301-redirects the no-slash form, and
# most HTTP clients downgrade POST → GET on 3xx, which turns a real webhook
# delivery into a 405 Method Not Allowed. Registering both forms avoids it.
urlpatterns = [
    path('webhook/cashfree/', CashfreeWebhookView.as_view(), name='cashfree_webhook'),
    path('webhook/cashfree',  CashfreeWebhookView.as_view()),

    # Razorpay webhook — register this URL in your Razorpay dashboard:
    #   https://<domain>/api/payments/webhook/razorpay/
    # The webhook secret you set in the dashboard MUST match the
    # RAZORPAY_WEBHOOK_SECRET env var on the backend.
    path('webhook/razorpay/', RazorpayWebhookView.as_view(), name='razorpay_webhook'),
    path('webhook/razorpay',  RazorpayWebhookView.as_view()),

    path('gateway-settings/', PaymentGatewaySettingsView.as_view(), name='payment_gateway_settings'),
]
