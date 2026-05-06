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
        fields = (
            'id', 'seller', 'order', 'nimbuspost_id', 'nimbuspost_order_id',
            'awb_number', 'courier_name', 'status',
            'label_url', 'manifest_url', 'package_image_url', 'pickup_scheduled_at',
            'created_at', 'updated_at',
        )
