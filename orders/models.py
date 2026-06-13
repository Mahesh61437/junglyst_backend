from django.db import models
from core.models import User, Product, ProductVariant, SoftDeleteModel
from django.utils.translation import gettext_lazy as _
import uuid
from django.utils import timezone

class OrderStatus(models.TextChoices):
    PENDING = 'pending', _('Pending')           # order created, awaiting payment
    CONFIRMED = 'confirmed', _('Confirmed')     # payment successful
    FAILED = 'failed', _('Failed')             # payment failed
    PROCESSING = 'processing', _('Processing') # seller(s) actively processing
    SHIPPED = 'shipped', _('Shipped')          # at least one sub-order shipped
    DELIVERED = 'delivered', _('Delivered')    # all sub-orders delivered
    CANCELLED = 'cancelled', _('Cancelled')
    RETURNED = 'returned', _('Returned')

class PaymentStatus(models.TextChoices):
    PENDING = 'pending', _('Pending')
    FAILED = 'failed', _('Failed')
    COMPLETED = 'completed', _('Completed')
    REFUNDED = 'refunded', _('Refunded')

class Order(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=20, unique=True)
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    guest_email = models.EmailField(null=True, blank=True)
    guest_phone = models.CharField(max_length=15, null=True, blank=True)
    
    shipping_address = models.JSONField()
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING)
    
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    awb_number = models.CharField(max_length=100, blank=True, null=True)
    courier_name = models.CharField(max_length=100, blank=True, null=True)
    estimated_delivery = models.DateField(null=True, blank=True)
    
    is_paid = models.BooleanField(default=False)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    payment_gateway = models.CharField(max_length=20, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Order {self.order_number}"

class SubOrderStatus(models.TextChoices):
    PENDING = 'pending', _('Pending')
    PLACED = 'placed', _('Placed')
    CONFIRMED = 'confirmed', _('Confirmed')
    PACKING = 'packing', _('Packing')
    BOOKED = 'booked', _('Courier Booked')          # AWB assigned, awaiting courier pickup
    BOOKING_FAILED = 'booking_failed', _('Booking Failed')  # Auto-booking exhausted retries
    SHIPPED = 'shipped', _('Shipped')               # Courier picked up the package
    IN_TRANSIT = 'in_transit', _('In Transit')
    OUT_FOR_DELIVERY = 'out_for_delivery', _('Out for Delivery')
    DELIVERED = 'delivered', _('Delivered')
    DELIVERY_FAILED = 'delivery_failed', _('Delivery Failed')
    DOA_RAISED = 'doa_raised', _('DOA Complaint Raised')
    CANCELLED = 'cancelled', _('Cancelled')


class SubOrder(SoftDeleteModel):
    """One sub-order per seller within a master Order (SHIP-003)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='sub_orders')
    sub_order_number = models.CharField(max_length=30, unique=True)  # e.g. JNG-2026-00123-A
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sub_orders')

    status = models.CharField(max_length=30, choices=SubOrderStatus.choices, default=SubOrderStatus.PENDING)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    seller_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Dispatch tracking
    confirmed_at = models.DateTimeField(null=True, blank=True)
    dispatch_deadline = models.DateTimeField(null=True, blank=True)  # confirmed_at + 48 h

    # Snapshot of the shipping promise shown to the buyer at checkout
    promised_ship_date = models.DateField(null=True, blank=True)
    promised_delivery_min = models.DateField(null=True, blank=True)
    promised_delivery_max = models.DateField(null=True, blank=True)

    # Packaging photos (stored as JSON list of URLs)
    packaging_photos = models.JSONField(default=list)

    # Actual shipment measurements (filled by seller before shipping)
    actual_weight_grams = models.PositiveIntegerField(null=True, blank=True)
    actual_length_cm = models.PositiveIntegerField(null=True, blank=True)
    actual_breadth_cm = models.PositiveIntegerField(null=True, blank=True)
    actual_height_cm = models.PositiveIntegerField(null=True, blank=True)

    # Shipment
    awb_number = models.CharField(max_length=100, null=True, blank=True)
    courier_name = models.CharField(max_length=100, null=True, blank=True)
    booking_failure_reason = models.TextField(null=True, blank=True)  # set when status=booking_failed

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sub_order_number']

    def __str__(self):
        return self.sub_order_number


class OrderItem(SoftDeleteModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    sub_order = models.ForeignKey(SubOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True)
    
    product_name = models.CharField(max_length=255)
    variant_name = models.CharField(max_length=100)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    quantity = models.PositiveIntegerField()
    
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='order_items')

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class SettlementStatus(models.TextChoices):
    PENDING   = 'pending',   _('Pending')
    COMPLETED = 'completed', _('Completed')


class SellerSettlement(models.Model):
    """Tracks whether a delivered sub-order's payout has been transferred to the seller."""
    sub_order = models.OneToOneField(SubOrder, on_delete=models.CASCADE, related_name='settlement')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='settlements')
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # = sub_order.seller_total

    status = models.CharField(max_length=10, choices=SettlementStatus.choices, default=SettlementStatus.PENDING)
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='settlements_marked')
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def is_settled(self):
        return self.status == SettlementStatus.COMPLETED

    def mark_settled(self, admin_user, notes=''):
        self.status = SettlementStatus.COMPLETED
        self.settled_at = timezone.now()
        self.settled_by = admin_user
        self.notes = notes
        self.save()

    def __str__(self):
        return f"Settlement {self.sub_order.sub_order_number} [{self.status}]"
