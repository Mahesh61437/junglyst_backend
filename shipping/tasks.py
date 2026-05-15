import logging
from datetime import date
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from .services import get_logistics_service
from orders.models import Order, OrderItem, SubOrder
from .models import Shipment

logger = logging.getLogger(__name__)

# HSN code for live plants / nursery stock in India
PLANT_HSN_CODE = "0602"


def _build_shipment_payload(order: Order, seller, courier_id: str, sub_order=None) -> dict:
    """Build the full NimbusPost shipment creation payload from a Junglyst sub-order."""
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

    item_qs = OrderItem.objects.filter(order=order, seller=seller).select_related("variant", "product")
    if sub_order:
        item_qs = item_qs.filter(sub_order=sub_order)
    items = list(item_qs)

    products = []
    total_weight_gram = 0
    for item in items:
        v = item.variant
        # Prefer chargeable_weight (packed vs volumetric max); fall back to legacy weight field
        cw = getattr(v, "chargeable_weight", None)
        if cw:
            w = float(cw)
        else:
            w = float(getattr(v, "packed_weight_grams", None) or getattr(v, "weight", 0.5) or 0.5) * (1 if getattr(v, "packed_weight_grams", None) else 1000)
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

    # Override with seller-confirmed actual measurements when available
    if sub_order and sub_order.actual_weight_grams:
        total_qty = sum(int(p["no_of_box"]) for p in products) or 1
        per_unit_weight = max(1, sub_order.actual_weight_grams // total_qty)
        l = sub_order.actual_length_cm or 15
        b = sub_order.actual_breadth_cm or 15
        h = sub_order.actual_height_cm or 15
        for p in products:
            p["product_weight"] = per_unit_weight
            p["product_length"] = str(l)
            p["product_breadth"] = str(b)
            p["product_height"] = str(h)
        total_weight_gram = sub_order.actual_weight_grams

    invoice_value = sum(
        float(item.unit_price * item.quantity) for item in items
    ) or float(order.total_amount)

    ref_number = sub_order.sub_order_number if sub_order else f"{order.order_number}-{str(seller.id)[:8]}"

    return {
        "order_id": ref_number,
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
            "invoice_number": ref_number,
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
def create_shipment_task(self, order_id: str, seller_id: str, courier_id: str = None, sub_order_id: str = None):
    """
    Create a NimbusPost shipment for a seller's sub-order.
    If courier_id is not supplied, the cheapest available courier is auto-selected.
    On failure after max retries, notifies admin and marks sub-order for manual AWB entry.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        order = Order.objects.get(id=order_id)
        seller = User.objects.get(id=seller_id)
    except (Order.DoesNotExist, User.DoesNotExist) as exc:
        logger.error("create_nimbuspost_shipment: not found – %s", exc)
        return str(exc)

    sub_order = None
    if sub_order_id:
        try:
            sub_order = SubOrder.objects.get(id=sub_order_id)
        except SubOrder.DoesNotExist:
            logger.warning("SubOrder %s not found, proceeding without sub-order link", sub_order_id)

    # Prevent duplicate booking
    if Shipment.objects.filter(order=order, seller=seller).exclude(status='pending').exists():
        return f"Shipment already created for order {order_id} / seller {seller_id}"

    # Auto-select cheapest courier if not provided
    if not courier_id:
        addr = order.shipping_address or {}
        dest_pincode = str(addr.get("pincode", ""))
        try:
            profile = seller.seller_profile
            origin_pincode = str(getattr(profile, "location_pincode", None) or "560001")
        except Exception:
            origin_pincode = "560001"

        item_qs = OrderItem.objects.filter(order=order, seller=seller)
        if sub_order:
            item_qs = item_qs.filter(sub_order=sub_order)
        items = list(item_qs.select_related("variant"))
        total_weight_kg = sum(
            float(getattr(i.variant, "chargeable_weight", None) or getattr(i.variant, "packed_weight_grams", None) or (float(getattr(i.variant, "weight", 0.5) or 0.5) * 1000)) / 1000 * i.quantity
            for i in items if i.variant
        ) or 0.5

        svc_client = get_logistics_service()
        svc = svc_client.check_serviceability(
            origin_pincode=origin_pincode,
            destination_pincode=dest_pincode,
            weight_kg=total_weight_kg,
            order_value=float(sub_order.seller_total if sub_order else order.total_amount),
        )
        if svc and svc.get("status") and svc.get("data"):
            sorted_couriers = sorted(svc["data"], key=lambda c: float(c.get("courier_charges", 9999)))
            if sorted_couriers:
                courier_id = sorted_couriers[0]["courier_id"]
                logger.info("Auto-selected courier %s for sub-order %s", courier_id, sub_order_id or order_id)

        if not courier_id:
            msg = f"No couriers available for {dest_pincode}"
            logger.error(msg)
            try:
                raise self.retry(exc=Exception(msg))
            except self.MaxRetriesExceededError:
                _notify_admin_booking_failure(order, sub_order, msg)
                return f"Failed after retries: {msg}"

    payload = _build_shipment_payload(order, seller, courier_id, sub_order)
    result = get_logistics_service().create_shipment(payload)

    if not result or not result.get("status"):
        msg = (result or {}).get("message", "No response from NimbusPost")
        logger.error("Shipment creation failed for %s: %s", sub_order_id or order_id, msg)
        try:
            raise self.retry(exc=Exception(msg))
        except self.MaxRetriesExceededError:
            _notify_admin_booking_failure(order, sub_order, msg)
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

    # Write AWB back to SubOrder
    if sub_order and awb:
        SubOrder.objects.filter(id=sub_order.id).update(
            awb_number=awb,
            courier_name=courier,
            status="shipped",
        )

    # Write AWB to master Order (first sub-order wins)
    if awb and not order.awb_number:
        Order.objects.filter(id=order_id).update(awb_number=awb, courier_name=courier, status="shipped")

    # Notify buyer
    buyer = order.user
    if buyer and awb:
        from notifications.models import AppNotification
        AppNotification.objects.create(
            user=buyer,
            title="Your order has been shipped!",
            message=f"Order {sub_order.sub_order_number if sub_order else order.order_number} is on its way. AWB: {awb} via {courier}.",
        )

    logger.info("Shipment created: %s AWB=%s", sub_order_id or order_id, awb)
    return {"awb_number": awb, "shipment_id": np_shipment_id, "label_url": label, "courier_name": courier}


def _notify_admin_booking_failure(order, sub_order, reason):
    """Email admin when NimbusPost auto-booking fails after all retries."""
    ref = sub_order.sub_order_number if sub_order else order.order_number
    admin_email = getattr(settings, "ADMIN_EMAIL", None) or getattr(settings, "DEFAULT_FROM_EMAIL", None)
    if not admin_email:
        logger.warning("No ADMIN_EMAIL configured — skipping failure notification for %s", ref)
        return
    try:
        send_mail(
            subject=f"[Junglyst] Shipment booking failed: {ref}",
            message=(
                f"NimbusPost auto-booking failed for {ref}.\n\n"
                f"Reason: {reason}\n\n"
                f"Please log into the seller dashboard and enter the AWB manually."
            ),
            from_email=admin_email,
            recipient_list=[admin_email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error("Failed to send admin notification: %s", e)


@shared_task
def sync_all_shipment_statuses():
    """Periodic task: sync statuses for all active shipments (run hourly via Celery Beat)."""
    NP_TO_SUBORDER = {
        "booked": "shipped", "pickup_scheduled": "shipped", "picked_up": "shipped",
        "in_transit": "in_transit", "out_for_delivery": "out_for_delivery",
        "delivered": "delivered", "delivery_failed": "delivery_failed",
        "rto_initiated": "delivery_failed", "rto_delivered": "cancelled",
        "cancelled": "cancelled",
    }

    active = Shipment.objects.exclude(
        status__in=["delivered", "cancelled", "returned", "pending"]
    ).filter(awb_number__isnull=False)

    updated = 0
    for shipment in active:
        result = get_logistics_service().track_shipment(shipment.awb_number)
        if result and result.get("status"):
            raw = (result.get("data", {}).get("status") or "").lower().replace(" ", "_")
            new_status = NP_TO_SUBORDER.get(raw, raw)
            if new_status and new_status != shipment.status:
                shipment.status = new_status
                shipment.save(update_fields=["status", "updated_at"])
                Order.objects.filter(id=shipment.order_id).update(status=new_status)
                SubOrder.objects.filter(awb_number=shipment.awb_number).update(status=new_status)
                updated += 1

    return f"Synced {updated} shipment statuses."


@shared_task
def generate_manifest_for_awbs(awb_numbers: list):
    """Generate a NimbusPost manifest PDF for the given AWBs and return the URL."""
    result = get_logistics_service().generate_manifest(awb_numbers)
    if result and result.get("status"):
        manifest_url = result.get("data")
        if manifest_url:
            Shipment.objects.filter(awb_number__in=awb_numbers).update(manifest_url=manifest_url)
            return manifest_url
    return None
