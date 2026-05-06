import logging
from datetime import date
from celery import shared_task
from django.conf import settings
from .services import NimbuspostService
from orders.models import Order, OrderItem
from .models import Shipment

logger = logging.getLogger(__name__)

# HSN code for live plants / nursery stock in India
PLANT_HSN_CODE = "0602"


def _build_shipment_payload(order: Order, seller, courier_id: str) -> dict:
    """Build the full NimbusPost B2B shipment creation payload from a Junglyst order."""
    addr = order.shipping_address or {}

    consignee_address = addr.get("address_line1", "")
    if addr.get("address_line2"):
        consignee_address += f", {addr['address_line2']}"

    try:
        profile = seller.seller_profile
    except Exception:
        profile = None

    pickup_phone = (
        getattr(profile, "phone", None) or
        getattr(seller, "phone", None) or
        "9999999999"
    )
    pickup_city = getattr(profile, "location_city", None) or "Bangalore"
    pickup_state = getattr(profile, "location_state", None) or "Karnataka"
    pickup_pincode = getattr(profile, "location_pincode", None) or "560001"
    pickup_address_str = getattr(profile, "pickup_address", None) or pickup_city
    warehouse_name = getattr(profile, "store_name", None) or settings.NIMBUSPOST_WAREHOUSE_NAME

    items = OrderItem.objects.filter(
        order=order, seller=seller
    ).select_related("variant", "product")

    products = []
    total_weight_gram = 0
    for item in items:
        v = item.variant
        w = float(getattr(v, "weight", 0.5) or 0.5) * 1000  # kg → grams
        l = float(getattr(v, "length", 10) or 10)
        b = float(getattr(v, "width", 10) or 10)
        h = float(getattr(v, "height", 10) or 10)
        total_weight_gram += w * item.quantity
        products.append({
            "product_name": item.product_name[:200],
            "product_hsn_code": PLANT_HSN_CODE,
            "product_lbh_unit": "cm",
            "no_of_box": str(item.quantity),
            "product_tax_per": str(item.gst_percentage or "0"),
            "product_price": str(item.unit_price * item.quantity),
            "product_weight_unit": "gram",
            "product_length": str(l),
            "product_breadth": str(b),
            "product_height": str(h),
            "product_weight": int(w),
        })

    if not products:
        products = [{
            "product_name": f"Order {order.order_number}",
            "product_hsn_code": PLANT_HSN_CODE,
            "product_lbh_unit": "cm",
            "no_of_box": "1",
            "product_tax_per": "0",
            "product_price": str(order.total_amount),
            "product_weight_unit": "gram",
            "product_length": "15",
            "product_breadth": "15",
            "product_height": "15",
            "product_weight": 500,
        }]

    invoice_value = sum(
        float(item.unit_price * item.quantity) for item in items
    ) or float(order.total_amount)

    return {
        "order_id": f"{order.order_number}-{str(seller.id)[:8]}",
        "payment_method": "prepaid",
        "consignee_name": addr.get("full_name", "Customer"),
        "consignee_company_name": addr.get("full_name", "Customer"),
        "consignee_phone": str(addr.get("phone", "9999999999")),
        "consignee_email": addr.get("email") or order.guest_email or "",
        "consignee_address": consignee_address or "India",
        "consignee_pincode": str(addr.get("pincode", "")),
        "consignee_city": addr.get("city", ""),
        "consignee_state": addr.get("state", ""),
        "no_of_invoices": "1",
        "no_of_boxes": 1,
        "courier_id": str(courier_id),
        "request_auto_pickup": "Yes",
        "invoice": [{
            "invoice_number": order.order_number,
            "invoice_date": date.today().strftime("%d-%m-%Y"),
            "invoice_value": str(round(invoice_value, 2)),
        }],
        "pickup": {
            "warehouse_name": warehouse_name[:20],
            "name": seller.get_full_name() or seller.username,
            "address": pickup_address_str[:200],
            "city": pickup_city,
            "state": pickup_state,
            "pincode": str(pickup_pincode),
            "phone": str(pickup_phone).replace("+91", "").replace(" ", "")[-10:],
        },
        "products": products,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_nimbuspost_shipment(self, order_id: str, seller_id: str, courier_id: str = None):
    """
    Create a NimbusPost B2B shipment for a specific seller's items in an order.
    If courier_id is not supplied, the cheapest available courier is auto-selected.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        order = Order.objects.get(id=order_id)
        seller = User.objects.get(id=seller_id)
    except (Order.DoesNotExist, User.DoesNotExist) as exc:
        logger.error("create_nimbuspost_shipment: not found – %s", exc)
        return str(exc)

    # Prevent duplicate
    if Shipment.objects.filter(order=order, seller=seller).exclude(status='pending').exists():
        return f"Shipment already created for order {order_id} / seller {seller_id}"

    # Auto-select courier if not provided
    if not courier_id:
        addr = order.shipping_address or {}
        dest_pincode = str(addr.get("pincode", ""))
        try:
            profile = seller.seller_profile
            origin_pincode = str(getattr(profile, "location_pincode", None) or "560001")
        except Exception:
            origin_pincode = "560001"

        items = OrderItem.objects.filter(order=order, seller=seller).select_related("variant")
        total_weight_kg = sum(
            float(getattr(i.variant, "weight", 0.5) or 0.5) * i.quantity for i in items
        ) or 0.5

        svc = NimbuspostService.check_serviceability(
            origin_pincode=origin_pincode,
            destination_pincode=dest_pincode,
            weight_kg=total_weight_kg,
            order_value=float(order.total_amount),
        )
        if svc and svc.get("status") and svc.get("data"):
            sorted_couriers = sorted(svc["data"], key=lambda c: float(c.get("courier_charges", 9999)))
            if sorted_couriers:
                courier_id = sorted_couriers[0]["courier_id"]
                logger.info("Auto-selected courier %s for order %s", courier_id, order_id)

        if not courier_id:
            msg = f"No couriers available for {dest_pincode}"
            logger.error(msg)
            try:
                raise self.retry(exc=Exception(msg))
            except self.MaxRetriesExceededError:
                return f"Failed after retries: {msg}"

    payload = _build_shipment_payload(order, seller, courier_id)
    result = NimbuspostService.create_shipment(payload)

    if not result or not result.get("status"):
        msg = (result or {}).get("message", "No response from NimbusPost")
        logger.error("Shipment creation failed for order %s: %s", order_id, msg)
        try:
            raise self.retry(exc=Exception(msg))
        except self.MaxRetriesExceededError:
            return f"Failed after retries: {msg}"

    data = result["data"]
    awb = str(data.get("awb_number", ""))
    np_shipment_id = str(data.get("shipment_id", ""))
    np_order_id = str(data.get("order_id", ""))
    label = data.get("label", "")
    manifest = data.get("manifest", "")
    courier = data.get("courier_name", "")

    Shipment.objects.update_or_create(
        order=order,
        seller=seller,
        defaults={
            "nimbuspost_id": np_shipment_id or None,
            "nimbuspost_order_id": np_order_id or None,
            "awb_number": awb or None,
            "courier_name": courier or None,
            "status": data.get("status", "booked"),
            "label_url": label or None,
            "manifest_url": manifest or None,
        },
    )

    if awb and not order.awb_number:
        Order.objects.filter(id=order_id).update(
            awb_number=awb,
            courier_name=courier,
            status="shipped",
        )

    logger.info("Shipment created: order=%s seller=%s AWB=%s", order_id, seller_id, awb)
    return {"awb_number": awb, "shipment_id": np_shipment_id, "label_url": label, "courier_name": courier}


@shared_task
def sync_all_shipment_statuses():
    """Periodic task: sync statuses for all active shipments (run hourly via Celery Beat)."""
    active = Shipment.objects.exclude(
        status__in=["delivered", "cancelled", "returned", "pending"]
    ).filter(awb_number__isnull=False)

    updated = 0
    for shipment in active:
        result = NimbuspostService.track_shipment(shipment.awb_number)
        if result and result.get("status"):
            new_status = result.get("data", {}).get("status")
            if new_status and new_status != shipment.status:
                shipment.status = new_status
                shipment.save(update_fields=["status", "updated_at"])
                Order.objects.filter(id=shipment.order_id).update(status=new_status)
                updated += 1

    return f"Synced {updated} shipment statuses."


@shared_task
def generate_manifest_for_awbs(awb_numbers: list):
    """Generate a NimbusPost manifest PDF for the given AWBs and return the URL."""
    result = NimbuspostService.generate_manifest(awb_numbers)
    if result and result.get("status"):
        manifest_url = result.get("data")
        if manifest_url:
            Shipment.objects.filter(awb_number__in=awb_numbers).update(manifest_url=manifest_url)
            return manifest_url
    return None
