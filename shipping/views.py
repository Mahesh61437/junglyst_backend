import logging
from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from rest_framework.throttling import BaseThrottle
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .models import ShippingAddress, Shipment, LogisticsProviderSettings, LogisticsProvider
from .serializers import ShippingAddressSerializer, ShipmentSerializer
from .services import get_logistics_service, check_pincode_deliverable
from .pincode_zones import classify_pincode

logger = logging.getLogger(__name__)


class NoThrottle(BaseThrottle):
    """Bypass all throttling for inbound courier webhooks."""
    def allow_request(self, request, view):
        return True
    def wait(self):
        return None


def _sync_order_status(order_id):
    """
    Derive the canonical Order.status from the current statuses of all its SubOrders
    and write it back to the Order row.  Call this after any SubOrder status change.

    Priority (evaluated top-to-bottom, first match wins):
      cancelled   – every sub-order is cancelled / booking_failed
      delivered   – every *active* sub-order is delivered (cancelled ones ignored)
      shipped     – at least one sub-order is in the delivery / RTO pipeline
      processing  – at least one sub-order is confirmed / booked / packing
    If no rule matches (e.g. all still 'pending') the Order row is left untouched.
    """
    from orders.models import SubOrder, Order

    statuses = list(
        SubOrder.objects.filter(order_id=order_id).values_list("status", flat=True)
    )
    if not statuses:
        return

    terminal      = {"cancelled", "booking_failed"}
    delivery_pipe = {"shipped", "in_transit", "out_for_delivery", "delivery_failed", "doa_raised"}
    process_pipe  = {"booked", "packing", "confirmed", "placed"}
    status_set    = set(statuses)

    if status_set <= terminal:
        new_order_status = "cancelled"
    elif all(s in {"delivered", "cancelled", "booking_failed"} for s in statuses) \
            and any(s == "delivered" for s in statuses):
        new_order_status = "delivered"
    elif status_set & delivery_pipe:
        new_order_status = "shipped"
    elif status_set & process_pipe:
        new_order_status = "processing"
    else:
        return  # all pending — leave Order status unchanged

    Order.objects.filter(id=order_id).update(status=new_order_status)


class ShippingAddressViewSet(viewsets.ModelViewSet):
    serializer_class = ShippingAddressSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return ShippingAddress.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        if ShippingAddress.objects.filter(user=self.request.user).count() >= 5:
            raise ValidationError("You can only save up to 5 addresses.")
        serializer.save(user=self.request.user)


class LogisticsViewSet(viewsets.ViewSet):

    # ── Serviceability / Rate Check ──────────────────────────────────────────

    def create(self, request):
        """
        POST /api/shipping/logistics/
        Check B2B serviceability and get courier rates.
        """
        origin = request.data.get("origin_pincode", "560001")
        destination = request.data.get("destination_pincode")
        weight = float(request.data.get("weight", 0.5))
        length = float(request.data.get("length", 10))
        breadth = float(request.data.get("breadth", 10))
        height = float(request.data.get("height", 10))
        order_value = float(request.data.get("order_value", 0))

        if not destination:
            return Response(
                {"error": "destination_pincode required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        svc = get_logistics_service()
        result = svc.check_serviceability(
            origin_pincode=origin,
            destination_pincode=destination,
            weight_kg=weight,
            order_value=order_value,
            length=length,
            breadth=breadth,
            height=height,
        )
        if result:
            return Response(result)
        return Response(
            {"error": "Logistics service unreachable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # ── Create Shipment ───────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="create-shipment",
            permission_classes=[permissions.IsAuthenticated])
    def create_shipment(self, request):
        """
        POST /api/shipping/logistics/create-shipment/
        Body: { order_id, seller_id, courier_id (optional) }
        Triggers async Celery task; responds 202.
        """
        order_id = request.data.get("order_id")
        seller_id = request.data.get("seller_id")
        courier_id = request.data.get("courier_id")

        if not order_id or not seller_id:
            return Response(
                {"error": "order_id and seller_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .tasks import create_shipment_task
        create_shipment_task.delay(str(order_id), str(seller_id), courier_id)
        return Response({"message": "Shipment creation initiated"}, status=status.HTTP_202_ACCEPTED)

    # ── Generate / Fetch Label ────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="generate-label",
            permission_classes=[permissions.IsAuthenticated])
    def generate_label(self, request):
        """
        POST /api/shipping/logistics/generate-label/
        Body: { awb_numbers: ["awb1", "awb2", ...] }
        Returns PDF label URL for the given AWBs (max 500).
        """
        awb_numbers = request.data.get("awb_numbers", [])
        if isinstance(awb_numbers, str):
            awb_numbers = [awb_numbers]

        if not awb_numbers:
            return Response(
                {"error": "awb_numbers required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = get_logistics_service().generate_label(awb_numbers)
        if result and result.get("status"):
            label_url = result.get("data")
            Shipment.objects.filter(awb_number__in=awb_numbers).update(label_url=label_url)
            return Response({"label_url": label_url})
        return Response(
            {"error": (result or {}).get("message", "Label generation failed")},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # ── Generate Manifest ─────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="generate-manifest",
            permission_classes=[permissions.IsAuthenticated])
    def generate_manifest(self, request):
        """
        POST /api/shipping/logistics/generate-manifest/
        Body: { awb_numbers: ["awb1", ...] }
        Returns manifest PDF URL.
        """
        awb_numbers = request.data.get("awb_numbers", [])
        if isinstance(awb_numbers, str):
            awb_numbers = [awb_numbers]

        if not awb_numbers:
            return Response(
                {"error": "awb_numbers required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = get_logistics_service().generate_manifest(awb_numbers)
        if result and result.get("status"):
            manifest_url = result.get("data")
            Shipment.objects.filter(awb_number__in=awb_numbers).update(manifest_url=manifest_url)
            return Response({"manifest_url": manifest_url})
        return Response(
            {"error": (result or {}).get("message", "Manifest generation failed")},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # ── Cancel Shipment ───────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="cancel-shipment",
            permission_classes=[permissions.IsAuthenticated])
    def cancel_shipment(self, request):
        """
        POST /api/shipping/logistics/cancel-shipment/
        Body: { awb_number }
        """
        awb_number = request.data.get("awb_number")
        if not awb_number:
            return Response(
                {"error": "awb_number required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = get_logistics_service().cancel_shipment(awb_number)
        if result and result.get("status"):
            Shipment.objects.filter(awb_number=awb_number).update(status="cancelled")

            # Also mark every SubOrder carrying this AWB as cancelled and roll up Order
            from orders.models import SubOrder
            SubOrder.objects.filter(awb_number=awb_number).update(status="cancelled")
            shipment = Shipment.objects.filter(awb_number=awb_number).first()
            if shipment:
                _sync_order_status(shipment.order_id)

            return Response({"message": result.get("message", "Shipment cancelled")})
        return Response(
            {"error": (result or {}).get("message", "Cancellation failed")},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # ── Track Shipment ────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"],
            url_path=r"track/(?P<awb_number>[^/.]+)",
            permission_classes=[permissions.AllowAny])
    def track(self, request, awb_number=None):
        """
        GET /api/shipping/logistics/track/{awb_number}/
        Returns tracking history from NimbusPost.
        """
        result = get_logistics_service().track_shipment(awb_number)
        if result and result.get("status"):
            return Response(result["data"])
        return Response(
            {"error": "Tracking information unavailable"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    # ── Shipment Status (internal) ────────────────────────────────────────────

    @action(detail=False, methods=["get"],
            url_path=r"shipment-status/(?P<order_id>[^/.]+)",
            permission_classes=[permissions.IsAuthenticated])
    def shipment_status(self, request, order_id=None):
        """
        GET /api/shipping/logistics/shipment-status/{order_id}/
        Returns the stored Shipment record for the requesting seller.
        """
        try:
            shipment = Shipment.objects.get(order_id=order_id, seller=request.user)
            return Response(ShipmentSerializer(shipment).data)
        except Shipment.DoesNotExist:
            return Response({"status": "pending"})

    # ── Wallet Balance (admin) ────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="wallet-balance",
            permission_classes=[permissions.IsAuthenticated])
    def wallet_balance(self, request):
        """
        GET /api/shipping/logistics/wallet-balance/
        Returns NimbusPost wallet balance. Admin / staff only.
        """
        if not (request.user.is_staff or request.user.role == "admin"):
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        result = get_logistics_service().get_wallet_balance()
        if result and result.get("status"):
            return Response(result["data"])
        return Response(
            {"error": "Could not fetch wallet balance"},
            status=status.HTTP_502_BAD_GATEWAY,
        )


class LogisticsProviderSettingsView(APIView):
    """
    GET  /api/shipping/provider-settings/  — return active provider
    PATCH /api/shipping/provider-settings/ — switch provider (super_admin only)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        s = LogisticsProviderSettings.get_solo()
        return Response({"active_provider": s.active_provider})

    def patch(self, request):
        from django.contrib.auth import get_user_model
        from notifications.models import AppNotification
        if request.user.role not in ("admin", "super_admin") and not request.user.is_staff:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)
        provider = request.data.get("active_provider")
        valid = {LogisticsProvider.NIMBUSPOST, LogisticsProvider.SHIPROCKET}
        if provider not in valid:
            return Response(
                {"error": f"active_provider must be one of: {', '.join(valid)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        s = LogisticsProviderSettings.get_solo()
        previous = s.active_provider
        s.active_provider = provider
        s.save(update_fields=["active_provider", "updated_at"])

        if previous != provider:
            User = get_user_model()
            superadmins = User.objects.filter(is_staff=True, is_superuser=True, is_active=True)
            label = {"nimbuspost": "NimbusPost", "shiprocket": "Shiprocket"}.get(provider, provider)
            prev_label = {"nimbuspost": "NimbusPost", "shiprocket": "Shiprocket"}.get(previous, previous)
            actor = request.user.get_full_name() or request.user.username or request.user.email
            notifs = [
                AppNotification(
                    user=admin,
                    title="Logistics Provider Switched",
                    message=(
                        f"{actor} switched the logistics provider from "
                        f"{prev_label} to {label}. "
                        f"All new shipments will now be booked via {label}."
                    ),
                )
                for admin in superadmins
            ]
            if notifs:
                AppNotification.objects.bulk_create(notifs)

        return Response({"active_provider": s.active_provider})


class PackageImageUploadView(APIView):
    """
    POST /api/shipping/package-image/
    Body: { order_id, image_url }
    Associates a package photo URL with the seller's shipment for the given order.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        order_id = request.data.get("order_id")
        image_url = request.data.get("image_url")

        if not order_id or not image_url:
            return Response(
                {"error": "order_id and image_url are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = Shipment.objects.filter(
            order_id=order_id, seller=request.user
        ).update(package_image_url=image_url)

        if not updated:
            # Create a pending shipment record to hold the image
            from orders.models import Order
            try:
                order = Order.objects.get(id=order_id, items__seller=request.user)
            except Order.DoesNotExist:
                return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)
            shipment, _ = Shipment.objects.get_or_create(
                order=order,
                seller=request.user,
                defaults={"status": "pending", "package_image_url": image_url},
            )
            if not _:
                shipment.package_image_url = image_url
                shipment.save(update_fields=["package_image_url", "updated_at"])

        return Response({"message": "Package image saved", "image_url": image_url})


class NimbusPostWebhookView(APIView):
    """
    POST /api/shipping/webhook/nimbuspost/
    Receives real-time tracking updates from NimbusPost.
    Maps courier status -> SubOrder status and notifies buyer.
    """
    permission_classes  = (permissions.AllowAny,)
    throttle_classes    = [NoThrottle]   # courier server must never be rate-limited
    authentication_classes = []          # no auth overhead on inbound webhooks

    # NimbusPost status → SubOrder status mapping
    STATUS_MAP = {
        # Courier assigned / pickup pending → "Booking Courier" tab
        "booked":            "booked",
        "pickup_scheduled":  "booked",
        # Courier physically collected → "Shipped" tab
        "picked_up":         "shipped",
        "in_transit":        "in_transit",
        "out_for_delivery":  "out_for_delivery",
        "delivered":         "delivered",
        "delivery_failed":   "delivery_failed",
        "rto_initiated":     "delivery_failed",
        "rto_delivered":     "cancelled",
        "cancelled":         "cancelled",
        "doa":               "doa_raised",
    }

    BUYER_MESSAGES = {
        "in_transit":       ("Order In Transit", "Your order {ref} is in transit and on its way to you."),
        "out_for_delivery": ("Out for Delivery!", "Great news! Order {ref} is out for delivery today."),
        "delivered":        ("Order Delivered!", "Your order {ref} has been delivered. Enjoy your new botanical!"),
        "delivery_failed":  ("Delivery Attempt Failed", "Delivery for order {ref} was unsuccessful. The courier will retry or contact you."),
        "doa_raised":       ("DOA Complaint Noted", "We have received your DOA report for order {ref}. Our team will reach out soon."),
    }

    def post(self, request):
        logger.info("NimbusPost webhook received | payload: %s", request.data)

        awb = request.data.get("awb_number") or request.data.get("awb")
        raw_status = (request.data.get("status") or "").lower().replace(" ", "_")

        if not awb:
            logger.warning("NimbusPost webhook missing AWB | payload: %s", request.data)
            return Response({"error": "awb_number required"}, status=400)

        mapped = self.STATUS_MAP.get(raw_status)
        if not mapped:
            logger.warning("NimbusPost webhook: unmapped status '%s' for AWB %s", raw_status, awb)
            return Response({"message": f"Status '{raw_status}' not mapped, ignored"})

        # Update Shipment record
        Shipment.objects.filter(awb_number=awb).update(status=mapped)

        # Update SubOrder
        from orders.models import SubOrder
        sub_orders = SubOrder.objects.filter(awb_number=awb).select_related("order__user")
        for so in sub_orders:
            so.status = mapped
            so.save(update_fields=["status", "updated_at"])

            # Roll up the parent Order status from all its SubOrders
            _sync_order_status(so.order_id)

            buyer = so.order.user
            if buyer and mapped in self.BUYER_MESSAGES:
                title, msg_tpl = self.BUYER_MESSAGES[mapped]
                from notifications.models import AppNotification
                AppNotification.objects.create(
                    user=buyer,
                    title=title,
                    message=msg_tpl.format(ref=so.sub_order_number),
                )

        logger.info("NimbusPost webhook processed: AWB %s -> %s", awb, mapped)
        return Response({"message": f"Webhook processed: {awb} -> {mapped}"})


@method_decorator(csrf_exempt, name="dispatch")
class ShiprocketWebhookView(APIView):
    """
    POST /api/shipping/webhook/courierservice/

    Receives real-time tracking updates from Shiprocket.
    Maps courier status -> SubOrder status and notifies buyer.

    Register in Shiprocket dashboard:
      Shiprocket > Settings > API > Webhook URL
      https://<your-domain>/api/shipping/webhook/courierservice/

    IMPORTANT: Shiprocket rejects webhook URLs that contain the words
    "shiprocket", "kartrocket", "sr", or "kr" — keep the path as
    "courierservice" (or any other neutral word).
    """
    permission_classes     = (permissions.AllowAny,)
    throttle_classes       = [NoThrottle]   # courier server must never be rate-limited
    authentication_classes = []             # no auth overhead on inbound webhooks

    # Shiprocket current_status → SubOrder status mapping.
    # Shiprocket sends current_status in mixed/upper case; we .lower().strip() before lookup.
    # Source: Shiprocket tracking status master list (all known variants included).
    STATUS_MAP = {
        # ── Pickup phase ────────────────────────────────────────────────────────
        "awb assigned":                  "booked",   # AWB created, awaiting pickup
        "label generated":               "booked",
        "pickup scheduled":              "booked",
        "out for pickup":                "booked",
        "pickup rescheduled":            "booked",   # courier missed first attempt
        "pickup error":                  "booked",   # pickup failed, will retry
        "pickup exception":              "booked",

        # ── Pickup permanently cancelled (rare, treat as cancellation) ─────────
        "pickup cancelled":              "cancelled",

        # ── Courier collected / in transit ──────────────────────────────────────
        "picked up":                     "shipped",
        "shipped":                       "shipped",
        "dispatched":                    "shipped",
        "bag picked up":                 "shipped",
        "self fulfillment":              "shipped",

        "in transit":                    "in_transit",
        "reached at origin hub":         "in_transit",
        "reached at destination hub":    "in_transit",
        "reached at courier facility":   "in_transit",
        "delayed":                       "in_transit",
        "transit delay":                 "in_transit",
        "misrouted":                     "in_transit",
        "connection misses":             "in_transit",
        "hold":                          "in_transit",  # held at facility

        # ── Delivery phase ──────────────────────────────────────────────────────
        "out for delivery":              "out_for_delivery",
        "delivery attempted":            "delivery_failed",
        "delivery delayed":              "out_for_delivery",  # still going to attempt
        "delivered":                     "delivered",
        "partial delivery":              "delivered",

        # ── NDR — Non-Delivery Report (buyer unreachable / address issue) ───────
        # NDR is raised after a failed delivery attempt; we surface it as
        # delivery_failed so the buyer/admin can take action.
        "ndr":                           "delivery_failed",
        "ndr action required":           "delivery_failed",
        "ndr actionrequired":            "delivery_failed",
        "ndr closed":                    "delivery_failed",
        "undelivered":                   "delivery_failed",
        "delivery failed":               "delivery_failed",

        # ── RTO — Return to Origin pipeline ─────────────────────────────────────
        "rto initiated":                 "delivery_failed",
        "rto in transit":                "delivery_failed",
        "rto out for delivery":          "delivery_failed",
        "rto contact customer":          "delivery_failed",
        "rto delivered":                 "cancelled",  # returned to seller
        "rto acknowledged":              "cancelled",

        # ── Cancellation (Shiprocket uses several variants by stage) ───────────
        "cancelled":                     "cancelled",
        "canceled":                      "cancelled",  # US spelling
        "cancellation requested":        "cancelled",
        "shipment cancelled":            "cancelled",
        "order cancelled":               "cancelled",
        "force closed":                  "cancelled",

        # ── Loss / damage / destroyed ───────────────────────────────────────────
        "lost":                          "delivery_failed",
        "damaged":                       "delivery_failed",
        "destroyed":                     "delivery_failed",
        "disposed":                      "delivery_failed",
        "return":                        "delivery_failed",
        "return initiated":              "delivery_failed",
    }

    BUYER_MESSAGES = {
        "booked":           ("Courier Pickup Scheduled",   "Your order {ref} has been packed and a courier has been scheduled for pickup."),
        "shipped":          ("Order Picked Up!",           "Your order {ref} has been picked up by the courier and is on its way."),
        "in_transit":       ("Order In Transit",           "Your order {ref} is in transit and on its way to you."),
        "out_for_delivery": ("Out for Delivery!",          "Great news! Order {ref} is out for delivery today."),
        "delivered":        ("Order Delivered!",           "Your order {ref} has been delivered. Enjoy your new botanical!"),
        "delivery_failed":  ("Delivery Attempt Failed",    "Delivery for order {ref} was unsuccessful. The courier will retry or contact you soon."),
        "cancelled":        ("Order Cancelled",            "Your order {ref} has been cancelled. If you have any questions, please contact support."),
    }

    def post(self, request):
        logger.info("Shiprocket webhook received | payload: %s", request.data)

        data = request.data

        # ── Identifiers (priority order) ─────────────────────────────────────
        awb = (
            data.get("awb_code") or data.get("awb") or data.get("awb_number")
        ) or None
        sr_shipment_id   = str(data.get("shipment_id")    or "").strip() or None
        sr_order_id      = str(data.get("order_id")       or "").strip() or None
        channel_order_id = str(data.get("channel_order_id") or "").strip() or None  # our order number

        # ── Status ───────────────────────────────────────────────────────────
        raw_status = (
            data.get("current_status") or data.get("status") or ""
        ).lower().strip()

        mapped = self.STATUS_MAP.get(raw_status)
        if not mapped:
            logger.warning(
                "Shiprocket webhook: unmapped status '%s' | AWB=%s shipment_id=%s "
                "order_id=%s channel_order_id=%s | full payload: %s",
                raw_status, awb, sr_shipment_id, sr_order_id, channel_order_id, data,
            )
            return Response({"message": f"Status '{raw_status}' not mapped, ignored"})

        # ── Extra fields from payload ─────────────────────────────────────────
        etd_raw        = str(data.get("etd") or "").strip()          # estimated delivery
        delivered_date = str(data.get("delivered_date") or "").strip()

        # ── Resolve Shipment queryset (cascade of fallbacks) ─────────────────
        # Priority: AWB → Shiprocket shipment_id → Shiprocket order_id → channel_order_id
        #
        # Each Shiprocket shipment belongs to exactly ONE sub-order/seller.
        # AWB, shipment_id and order_id are all 1-to-1 with a sub-order, so using
        # them is always safe for multi-seller orders.
        #
        # channel_order_id is our Order number (e.g. JNG-2026-XXXXX) and maps to
        # the WHOLE order — which may have multiple sub-orders from different sellers.
        # We ONLY use it as a last resort AND only when there is exactly one shipment
        # under that order; if there are multiple we cannot tell which seller this
        # event belongs to, so we refuse rather than corrupt multiple sub-orders.
        shipments_qs = None

        if awb:
            shipments_qs = Shipment.objects.filter(awb_number=awb)

        if not shipments_qs or not shipments_qs.exists():
            if sr_shipment_id:
                shipments_qs = Shipment.objects.filter(nimbuspost_id=sr_shipment_id)

        if not shipments_qs or not shipments_qs.exists():
            if sr_order_id:
                shipments_qs = Shipment.objects.filter(nimbuspost_order_id=sr_order_id)

        if not shipments_qs or not shipments_qs.exists():
            if channel_order_id:
                from orders.models import Order as _Order
                try:
                    order_obj   = _Order.objects.get(order_number=channel_order_id)
                    candidate   = Shipment.objects.filter(order=order_obj)
                    count       = candidate.count()
                    if count == 1:
                        # Single-seller order — safe to use
                        shipments_qs = candidate
                    elif count > 1:
                        # Multi-seller order — AWB/shipment_id is required to know
                        # which sub-order this event is for; refuse to update blindly.
                        logger.error(
                            "Shiprocket webhook: channel_order_id=%s matches %d shipments "
                            "(multi-seller order). Cannot determine target sub-order without "
                            "awb/shipment_id. Payload: %s",
                            channel_order_id, count, data,
                        )
                        return Response(
                            {"error": "Multi-seller order requires awb or shipment_id"},
                            status=400,
                        )
                except _Order.DoesNotExist:
                    shipments_qs = Shipment.objects.none()

        if not shipments_qs or not shipments_qs.exists():
            logger.error(
                "Shiprocket webhook: no DB shipment found | AWB=%s shipment_id=%s "
                "order_id=%s channel_order_id=%s",
                awb, sr_shipment_id, sr_order_id, channel_order_id,
            )
            return Response({"error": "No matching shipment found"}, status=404)

        shipments_qs.update(status=mapped)

        # ── Resolve sub-orders ───────────────────────────────────────────────
        # Each sub-order represents an independent shipment from a different seller.
        # They have separate AWBs and separate tracking journeys, so each one gets
        # its own status update AND its own buyer notification.
        from orders.models import SubOrder, Order
        if awb:
            sub_orders = SubOrder.objects.filter(awb_number=awb).select_related("order__user")
        else:
            order_ids  = list(shipments_qs.values_list("order_id",  flat=True))
            seller_ids = list(shipments_qs.values_list("seller_id", flat=True))
            sub_orders = SubOrder.objects.filter(
                order_id__in=order_ids,
                seller_id__in=seller_ids,
            ).select_related("order__user")

        etd_written = set()   # write ETD once per parent order, not once per sub-order
        for so in sub_orders:
            so.status = mapped
            so.save(update_fields=["status", "updated_at"])

            # Roll up the parent Order status from all its SubOrders
            _sync_order_status(so.order_id)

            # ── Store ETD on Order (once per parent order) ───────────────────
            if etd_raw and so.order_id not in etd_written:
                try:
                    import datetime
                    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
                        try:
                            parsed = datetime.datetime.strptime(etd_raw, fmt).date()
                            Order.objects.filter(id=so.order_id).update(estimated_delivery=parsed)
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass  # etd parsing failure must never break the webhook
                etd_written.add(so.order_id)

            # ── Buyer notification — one per sub-order (independent tracker) ─
            # Multi-seller orders have separate AWBs and separate sub-order numbers.
            # Each sub-order update triggers its own notification so the buyer can
            # track each seller's shipment independently.
            buyer = so.order.user
            if buyer and mapped in self.BUYER_MESSAGES:
                title, msg_tpl = self.BUYER_MESSAGES[mapped]
                from notifications.models import AppNotification
                AppNotification.objects.create(
                    user=buyer,
                    title=title,
                    message=msg_tpl.format(ref=so.sub_order_number),
                )

        identifier = awb or sr_shipment_id or sr_order_id or channel_order_id
        logger.info("Shiprocket webhook processed: %s -> %s", identifier, mapped)
        return Response({"message": f"Webhook processed: {identifier} -> {mapped}"})


class PincodeCheckView(APIView):
    """
    GET /api/shipping/pincode-check/?pincode=XXXXXX
    Checks deliverability via the active logistics provider (Shiprocket or NimbusPost).
    Falls back to local zone classification if the external API is unavailable.
    Results are cached for 6 hours per provider.
    """
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        pincode = request.query_params.get('pincode', '').strip()
        if not pincode or not pincode.isdigit() or len(pincode) != 6:
            return Response(
                {"error": "Invalid pincode. Must be a 6-digit number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        deliverable, message = check_pincode_deliverable(pincode)
        # Build a response compatible with the existing CartContext shape
        zone_fallback = classify_pincode(pincode)
        result = {
            "pincode": pincode,
            "deliverable": deliverable,
            "zone": zone_fallback["zone"] if deliverable else "E",
            "city": zone_fallback.get("city"),
            "message": message,
        }
        return Response(result)
