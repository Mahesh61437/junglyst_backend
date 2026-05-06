from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.views import APIView
from .models import ShippingAddress, Shipment
from .serializers import ShippingAddressSerializer, ShipmentSerializer
from .services import NimbuspostService


class ShippingAddressViewSet(viewsets.ModelViewSet):
    serializer_class = ShippingAddressSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return ShippingAddress.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
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

        result = NimbuspostService.check_serviceability(
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

        from .tasks import create_nimbuspost_shipment
        create_nimbuspost_shipment.delay(str(order_id), str(seller_id), courier_id)
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

        result = NimbuspostService.generate_label(awb_numbers)
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

        result = NimbuspostService.generate_manifest(awb_numbers)
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

        result = NimbuspostService.cancel_shipment(awb_number)
        if result and result.get("status"):
            Shipment.objects.filter(awb_number=awb_number).update(status="cancelled")
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
        result = NimbuspostService.track_shipment(awb_number)
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

        result = NimbuspostService.get_wallet_balance()
        if result and result.get("status"):
            return Response(result["data"])
        return Response(
            {"error": "Could not fetch wallet balance"},
            status=status.HTTP_502_BAD_GATEWAY,
        )


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
