from django.apps import AppConfig


class WorkshopsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'workshops'
    verbose_name = 'Workshops'

    def ready(self):
        from orders.models import OrderType
        from orders.order_types import register_order_type
        from .services import WorkshopOrderHandler

        register_order_type(OrderType.WORKSHOP, WorkshopOrderHandler())
