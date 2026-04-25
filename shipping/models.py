from django.db import models
from core.models import User, SoftDeleteModel
from orders.models import Order

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

    def __str__(self):
        return f"{self.full_name} - {self.city}"

class Shipment(SoftDeleteModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='shipments')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='grower_shipments')
    
    nimbuspost_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    awb_number = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(max_length=50, default='pending')
    label_url = models.URLField(max_length=1000, null=True, blank=True)
    pickup_scheduled_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('order', 'seller')

    def __str__(self):
        return f"Shipment for Order {self.order.order_number} by {self.seller.username}"
