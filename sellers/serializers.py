from rest_framework import serializers
from .models import SellerProfile
from core.models import User

class SellerUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'email', 'username', 'full_name', 'phone', 'role', 'created_at')
        extra_kwargs = {'full_name': {'source': 'get_full_name', 'read_only': True}}

class SellerProfileSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = SellerProfile
        fields = '__all__'
        read_only_fields = ('user', 'total_sales', 'rating')

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.username
