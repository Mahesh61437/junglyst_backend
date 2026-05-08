from django.urls import path
from .views import RazorpayWebhookView

urlpatterns = [
    path('webhook/razorpay/', RazorpayWebhookView.as_view(), name='razorpay_webhook'),
]
