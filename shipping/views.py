from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from .models import ShippingAddress
from .serializers import ShippingAddressSerializer
from .services import NimbuspostService

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
    permission_classes = (permissions.AllowAny,)

    def create(self, request):
        """
        Check serviceability and shipping rates.
        """
        origin = request.data.get('origin_pincode', "560001")
        destination = request.data.get('destination_pincode')
        weight = request.data.get('weight', 0.5)
        
        if not destination:
            return Response({"error": "Destination pincode required"}, status=status.HTTP_400_BAD_REQUEST)
            
        result = NimbuspostService.check_serviceability(origin, destination, weight)
        if result:
            return Response(result)
        return Response({"error": "Logistics service unreachable"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], url_path='create-shipment')
    def create_shipment(self, request):
        order_id = request.data.get('order_id')
        if not order_id:
            return Response({"error": "Order ID required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Trigger Celery Task
        from .tasks import create_nimbuspost_shipment
        create_nimbuspost_shipment.delay(order_id)
        
        return Response({"message": "Shipment creation initiated"}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='generate-label')
    def generate_label(self, request):
        shipment_id = request.data.get('shipment_id')
        if not shipment_id:
            return Response({"error": "Shipment ID required"}, status=status.HTTP_400_BAD_REQUEST)
        
        from .tasks import generate_and_save_label
        generate_and_save_label.delay(shipment_id)
        
        return Response({"message": "Label generation initiated"}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['get'], url_path='shipment-status/(?P<order_id>[^/.]+)')
    def shipment_status(self, request, order_id=None):
        from .models import Shipment
        try:
            # For the current seller
            shipment = Shipment.objects.get(order_id=order_id, seller=request.user)
            return Response({
                "status": shipment.status,
                "nimbuspost_id": shipment.nimbuspost_id,
                "awb_number": shipment.awb_number,
                "label_url": shipment.label_url,
                "pickup_scheduled_at": shipment.pickup_scheduled_at
            })
        except Shipment.DoesNotExist:
            return Response({"status": "pending"})
