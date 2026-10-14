"""
Order-type registry.

Product orders are fulfilled by the original checkout/shipping pipeline
(SubOrders, OrderItems, stock, couriers). Every other kind of order —
workshops today, services tomorrow — reuses the same Order + Payment rows so
the payment gateways, webhooks and reconcile tasks work unchanged, and plugs
its own fulfilment in here.

An app registers a handler for its OrderType in its AppConfig.ready():

    register_order_type(OrderType.WORKSHOP, WorkshopOrderHandler())

The handler is driven by Order.post_save (see the receiver below), because a
payment can be captured from several places (verify endpoint, status poll,
both webhooks, the reconcile task). Hooking the save means none of those paths
need to know about non-product orders. Handlers must therefore be idempotent.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order, OrderStatus, OrderType

logger = logging.getLogger(__name__)

FAILED_ORDER_STATUSES = (OrderStatus.FAILED, OrderStatus.CANCELLED)


class OrderTypeHandler:
    """Base class for non-product order fulfilment. Override what you need."""

    # Reverse relations from Order to this type's booking row, select_related
    # by order list/detail views so summarize() costs no extra queries.
    order_select_related = ()
    order_prefetch_related = ()

    def summarize(self, order, public=False):
        """Compact dict describing what was booked, embedded in order APIs.
        `public=True` is for the unauthenticated tracking page: no personal or
        contact details."""
        return None

    def on_paid(self, order):
        """Payment captured. Confirm the booking, notify people. Idempotent."""

    def on_failed(self, order):
        """Payment failed or the order was cancelled. Release holds. Idempotent."""


_handlers = {}


def register_order_type(order_type, handler):
    _handlers[order_type] = handler


def get_order_type_handler(order_type):
    return _handlers.get(order_type)


def is_product_order(order):
    return getattr(order, 'order_type', OrderType.PRODUCT) == OrderType.PRODUCT


def booking_select_related():
    """select_related paths for every registered order type's booking row."""
    return [path for handler in _handlers.values() for path in handler.order_select_related]


def booking_prefetch_related():
    """prefetch_related paths summarize() needs, for every registered type."""
    return [path for handler in _handlers.values() for path in handler.order_prefetch_related]


def summarize_booking(order, public=False):
    if is_product_order(order):
        return None
    handler = get_order_type_handler(order.order_type)
    return handler.summarize(order, public=public) if handler else None


@receiver(post_save, sender=Order)
def _dispatch_order_type_lifecycle(sender, instance, created, **kwargs):
    if created or is_product_order(instance):
        return
    handler = get_order_type_handler(instance.order_type)
    if handler is None:
        logger.error("No order-type handler registered for %s (order %s)",
                     instance.order_type, instance.order_number)
        return
    order_id = instance.pk
    if instance.is_paid:
        # Run after the capture transaction commits so the handler sees the
        # committed state and its emails never go out for a rolled-back capture.
        transaction.on_commit(lambda: handler.on_paid(Order.objects.get(pk=order_id)))
    elif instance.status in FAILED_ORDER_STATUSES:
        transaction.on_commit(lambda: handler.on_failed(Order.objects.get(pk=order_id)))
