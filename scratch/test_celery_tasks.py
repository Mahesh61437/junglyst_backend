import os
import django
import sys

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from shipping.tasks import sync_all_shipment_statuses, create_nimbuspost_shipment
from shipping.models import Shipment
from orders.models import Order
from core.models import User

def test_tasks():
    print("Testing Celery Tasks (Synchronous Mode)...")
    
    # 1. Test Sync Task
    try:
        result = sync_all_shipment_statuses()
        print(f"Sync Task Result: {result}")
    except Exception as e:
        print(f"Sync Task Failed: {str(e)}")

    # 2. Test Shipment Creation (Mock Order)
    order = Order.objects.first()
    if order:
        print(f"Attempting to create shipment for Order: {order.id}")
        result = create_nimbuspost_shipment(order.id)
        print(f"Create Shipment Result: {result}")
    else:
        print("No orders found to test shipment creation.")

if __name__ == "__main__":
    test_tasks()
