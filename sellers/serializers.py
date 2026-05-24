from rest_framework import serializers
from .models import SellerProfile, SellerBlackoutDate
from core.models import User


class SellerBlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SellerBlackoutDate
        fields = ('id', 'start_date', 'end_date', 'reason', 'created_at')
        read_only_fields = ('id', 'created_at')

    def validate(self, attrs):
        start = attrs.get('start_date')
        end = attrs.get('end_date')
        if start and end and end < start:
            raise serializers.ValidationError({'end_date': 'end_date must be on or after start_date'})
        return attrs


class SellerUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'email', 'username', 'full_name', 'phone', 'role', 'created_at')
        extra_kwargs = {'full_name': {'source': 'get_full_name', 'read_only': True}}


class SellerProfileSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()
    next_shipping_date = serializers.SerializerMethodField()
    blackout_dates = SellerBlackoutDateSerializer(many=True, read_only=True)

    class Meta:
        model = SellerProfile
        fields = '__all__'
        read_only_fields = ('user', 'total_sales', 'rating')

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.username

    def get_next_shipping_date(self, obj):
        d = obj.get_next_shipping_date()
        return d.isoformat() if d else None
