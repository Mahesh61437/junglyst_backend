from celery import shared_task
from .services import NimbuspostService
from orders.models import Order
from .models import Shipment
import logging

logger = logging.getLogger(__name__)

@shared_task
def create_nimbuspost_shipment(order_id):
    try:
        order = Order.objects.get(id=order_id)
        # Prepare shipment data from order
        shipment_data = {
            "order_number": str(order.id),
            "consignee_name": f"{order.user.first_name} {order.user.last_name}",
            "consignee_address": order.shipping_address,
            "consignee_pincode": order.shipping_pincode,
            # ... add other fields like weight, dimensions from product variants
        }
        
        result = NimbuspostService.create_shipment(shipment_data)
        if result and result.get('status'):
            shipment_id = result.get('data', {}).get('shipment_id')
            Shipment.objects.create(
                order=order,
                nimbuspost_id=shipment_id,
                status='created'
            )
            return f"Shipment created for order {order_id}: {shipment_id}"
        return f"Failed to create shipment for order {order_id}"
    except Exception as e:
        logger.error(f"Error in create_nimbuspost_shipment: {str(e)}")
        return str(e)

@shared_task
def generate_and_save_label(shipment_id):
    try:
        label_url = NimbuspostService.generate_label(shipment_id)
        if label_url:
            shipment = Shipment.objects.get(nimbuspost_id=shipment_id)
            shipment.label_url = label_url
            shipment.save()
            return f"Label generated for shipment {shipment_id}"
        return f"Failed to generate label for shipment {shipment_id}"
    except Exception as e:
        logger.error(f"Error in generate_and_save_label: {str(e)}")
        return str(e)

@shared_task
def sync_all_shipment_statuses():
    """
    Periodic task to sync statuses of active shipments from Nimbuspost.
    """
    active_shipments = Shipment.objects.exclude(status__in=['delivered', 'cancelled', 'returned'])
    count = 0
    for shipment in active_shipments:
        if shipment.awb_number:
            tracking_info = NimbuspostService.track_shipment(shipment.awb_number)
            if tracking_info and tracking_info.get('status'):
                new_status = tracking_info.get('data', {}).get('status')
                if new_status and new_status != shipment.status:
                    shipment.status = new_status
                    shipment.save()
                    count += 1
    return f"Synced {count} shipment statuses."
