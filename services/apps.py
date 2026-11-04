from django.apps import AppConfig


class ServicesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'services'
    verbose_name = 'Services'

    def ready(self):
        from orders.models import OrderType
        from orders.order_types import register_order_type
        from .workflow import ServiceOrderHandler

        register_order_type(OrderType.SERVICE, ServiceOrderHandler())
        from . import signals  # noqa: F401  (connects the sub-order receiver)
