"""
Keep CouponRedemption in step with the Order lifecycle without touching each
of the several payment-success / failure code paths:
  • order becomes paid      → record the redemption (idempotent, OneToOne)
  • order cancelled/failed  → drop it so the usage slot is released
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from orders.models import Order
from .models import CouponRedemption


@receiver(post_save, sender=Order)
def sync_coupon_redemption(sender, instance: Order, **kwargs):
    if not instance.coupon_id:
        return
    if instance.status in ('cancelled', 'failed'):
        CouponRedemption.objects.filter(order=instance).delete()
        return
    if instance.is_paid:
        CouponRedemption.objects.get_or_create(
            order=instance,
            defaults={
                'coupon_id': instance.coupon_id,
                'user': instance.user,
                'guest_email': instance.guest_email,
                'discount_amount': instance.discount_amount,
            },
        )
