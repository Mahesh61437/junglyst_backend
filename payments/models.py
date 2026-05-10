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

    # ── Complaint / dispute tracking fields ──────────────────────────────

    # Bank reference number (UPI UTR / NEFT ref / IMPS ref)
    # This is the proof customers cite when saying "money debited but order not placed"
    bank_reference = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="UPI UTR / NEFT reference number from bank."
    )

    # Raw gateway status (e.g. 'SUCCESS', 'FAILED', 'USER_DROPPED', 'PENDING')
    # Separate from our internal `status` field — helps debug mismatches
    gateway_status = models.CharField(
        max_length=50, blank=True, null=True,
        help_text="Raw payment status from the gateway API."
    )

    # Error details — why a payment failed
    error_code = models.CharField(max_length=100, blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    # Full gateway response (for investigating disputes / chargebacks)
    gateway_response = models.JSONField(
        blank=True, null=True,
        help_text="Full raw JSON response from the gateway (for dispute investigation)."
    )

    # Timestamps for status transitions
    paid_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the payment was confirmed as captured."
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Payment {self.id} | {self.gateway} | {self.status} | ₹{self.amount}"


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
