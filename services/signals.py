from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from orders.models import SubOrder


@receiver(post_save, sender=SubOrder)
def deliver_quote_with_sub_order(sender, instance, **kwargs):
    """However a custom order's sub-order reaches delivered (seller, courier sync, admin), the request follows."""
    if instance.status != 'delivered':
        return
    from .models import QuoteRequest
    from .workflow import record_quote_delivered

    quote_id = QuoteRequest.objects.filter(order_id=instance.order_id).values_list('pk', flat=True).first()
    if quote_id:
        transaction.on_commit(lambda: record_quote_delivered(quote_id))
