from rest_framework import serializers
from .models import ShippingAddress, Shipment

class ShippingAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingAddress
        fields = '__all__'
        read_only_fields = ('user',)

class ShipmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shipment
        fields = ('id', 'seller', 'nimbuspost_id', 'status', 'label_url', 'awb_number')
