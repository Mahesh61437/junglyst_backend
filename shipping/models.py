from django.db import models
from core.models import User, SoftDeleteModel
from orders.models import Order


class LogisticsProvider(models.TextChoices):
    NIMBUSPOST = 'nimbuspost', 'NimbusPost'
    SHIPROCKET = 'shiprocket', 'Shiprocket'


class LogisticsProviderSettings(models.Model):
    """
    Singleton settings row controlling which logistics provider is active.
    Mirrors PaymentGatewaySettings pattern.
    """
    active_provider = models.CharField(
        max_length=20,
        choices=LogisticsProvider.choices,
        default=LogisticsProvider.NIMBUSPOST,
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    class Meta:
        verbose_name = 'Logistics Provider Settings'
        verbose_name_plural = 'Logistics Provider Settings'

class ShippingAddress(SoftDeleteModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='addresses')
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=15)
    email = models.EmailField()
    
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(max_length=10)
    country = models.CharField(max_length=100, default='India')
    
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.is_default and self.user:
            ShippingAddress.objects.filter(user=self.user).update(is_default=False)
        elif not self.is_default and self.user:
            # If no other addresses exist, make this the default
            if not ShippingAddress.objects.filter(user=self.user).exists():
                self.is_default = True
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.full_name} - {self.city}"

class Shipment(SoftDeleteModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='shipments')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='grower_shipments')

    # NimbusPost identifiers
    nimbuspost_id = models.CharField(max_length=100, unique=True, null=True, blank=True)    # NP shipment_id
    nimbuspost_order_id = models.CharField(max_length=100, null=True, blank=True)           # NP order_id
    awb_number = models.CharField(max_length=100, null=True, blank=True)
    courier_name = models.CharField(max_length=100, null=True, blank=True)

    status = models.CharField(max_length=50, default='pending')
    label_url = models.URLField(max_length=1000, null=True, blank=True)
    manifest_url = models.URLField(max_length=1000, null=True, blank=True)
    package_image_url = models.URLField(max_length=1000, null=True, blank=True)
    pickup_scheduled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('order', 'seller')

    def __str__(self):
        return f"Shipment for Order {self.order.order_number} by {self.seller.username}"
