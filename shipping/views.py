from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from .models import ShippingAddress
from .serializers import ShippingAddressSerializer
from .services import NimbuspostService

class ShippingAddressViewSet(viewsets.ModelViewSet):
    serializer_class = ShippingAddressSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return ShippingAddress.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
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
