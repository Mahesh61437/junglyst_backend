import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='orders.tasks.send_order_confirmation_emails_task', max_retries=2, default_retry_delay=60)
def send_order_confirmation_emails_task(order_id):
    from orders.models import Order
    from orders.email_utils import send_order_confirmation_emails
    try:
        order = Order.objects.get(id=order_id)
        send_order_confirmation_emails(order)
        logger.info("Emails sent for order %s", order.order_number)
    except Exception as exc:
        logger.error("Email task failed for order %s: %s", order_id, exc)
        raise


@shared_task(name='orders.tasks.create_order_notifications_task')
def create_order_notifications_task(order_id):
    from orders.models import Order
    from notifications.models import AppNotification
    try:
        order = Order.objects.select_related('user').prefetch_related(
            'sub_orders__seller', 'sub_orders__items'
        ).get(id=order_id)

        if order.user:
            AppNotification.objects.create(
                user=order.user,
                title="Order Placed!",
                message=f"Your order {order.order_number} has been successfully placed and is being prepared.",
            )

        seller_notifs = []
        for sub in order.sub_orders.select_related('seller').all():
            item_count = sub.items.count()
            seller_notifs.append(AppNotification(
                user=sub.seller,
                title="New Order Received!",
                message=(
                    f"Sub-order {sub.sub_order_number} has been placed with "
                    f"{item_count} item{'s' if item_count != 1 else ''}. "
                    f"Please confirm within 48 hours."
                ),
            ))
        if seller_notifs:
            AppNotification.objects.bulk_create(seller_notifs)

        logger.info("Notifications created for order %s", order.order_number)
    except Exception as exc:
        logger.error("Notification task failed for order %s: %s", order_id, exc)


def clear_ordered_cart_items(order):
    """Remove only the rows this order actually covered.

    An order no longer necessarily spans the whole cart: checkout can exclude
    items stock couldn't fulfil, and the buyer is told those stay in their cart.
    Wiping the cart wholesale would silently discard them. Falls back to a full
    clear only when the order has no resolvable variants.
    """
    from cart.models import Cart, CartItem
    from django.utils import timezone

    if not order.user_id:
        return
    Cart.objects.filter(user_id=order.user_id).update(updated_at=timezone.now())
    ordered_variant_ids = [
        vid for vid in order.items.values_list('variant_id', flat=True) if vid
    ]
    rows = CartItem.objects.filter(cart__user_id=order.user_id)
    if ordered_variant_ids:
        rows = rows.filter(variant_id__in=ordered_variant_ids)
    rows.delete()


@shared_task(name='orders.tasks.clear_buyer_cart_task')
def clear_buyer_cart_task(user_id, order_id=None):
    from cart.models import Cart, CartItem
    from django.utils import timezone
    from .models import Order
    try:
        if order_id:
            order = Order.objects.filter(id=order_id).first()
            if order:
                clear_ordered_cart_items(order)
                logger.info("Ordered cart rows cleared for user %s (order %s)", user_id, order_id)
                return
        Cart.objects.filter(user_id=user_id).update(updated_at=timezone.now())
        CartItem.objects.filter(cart__user_id=user_id).delete()
        logger.info("Cart cleared for user %s", user_id)
    except Exception as exc:
        logger.error("Cart clear failed for user %s: %s", user_id, exc)
