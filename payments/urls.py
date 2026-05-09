from django.urls import path
from .views import CashfreeWebhookView

urlpatterns = [
    path('webhook/cashfree/', CashfreeWebhookView.as_view(), name='cashfree_webhook'),
]
