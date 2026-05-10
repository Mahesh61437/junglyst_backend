from django.db import models
from orders.models import Order
from core.models import SoftDeleteModel

class PaymentGateway(models.TextChoices):
    CASHFREE = 'cashfree', 'Cashfree'
    RAZORPAY = 'razorpay', 'Razorpay'

class Payment(SoftDeleteModel):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='payment')
    gateway = models.CharField(max_length=20, choices=PaymentGateway.choices, default=PaymentGateway.CASHFREE)

    # Cashfree (PG)
    cashfree_order_id = models.CharField(max_length=100, unique=True, blank=True, null=True)
    cashfree_session_id = models.CharField(max_length=255, blank=True, null=True)
    cashfree_payment_id = models.CharField(max_length=100, blank=True, null=True)

    # Razorpay
    razorpay_order_id = models.CharField(max_length=100, unique=True, blank=True, null=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=255, blank=True, null=True)
    
    method = models.CharField(max_length=50, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default='created')
    
    created_at = models.DateTimeField(auto_now_add=True)


class PaymentGatewaySettings(models.Model):
    """
    Singleton settings row controlling which gateway Checkout should use.
    """
    active_gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.CASHFREE,
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj
