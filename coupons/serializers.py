from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from core.models import User, Category
from .models import Coupon


class CouponAdminSerializer(serializers.ModelSerializer):
    """Full read/write serializer for the super-admin coupons page."""
    seller = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_verified_seller=True), allow_null=True, required=False)
    categories = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all(), many=True, required=False)
    seller_name = serializers.SerializerMethodField()
    category_names = serializers.SerializerMethodField()
    discount_label = serializers.CharField(source='describe_discount', read_only=True)
    times_used = serializers.SerializerMethodField()
    is_live = serializers.BooleanField(read_only=True)

    class Meta:
        model = Coupon
        fields = (
            'id', 'code', 'name', 'description',
            'discount_type', 'discount_value', 'max_discount_amount', 'funded_by',
            'seller', 'seller_name', 'categories', 'category_names', 'min_cart_value',
            'valid_from', 'valid_until', 'is_active',
            'usage_limit_total', 'usage_limit_per_user',
            'discount_label', 'times_used', 'is_live', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_seller_name(self, obj):
        if not obj.seller_id:
            return None
        try:
            return obj.seller.seller_profile.store_name
        except Exception:
            return obj.seller.email

    def get_category_names(self, obj):
        return [c.name for c in obj.categories.all()]

    def get_times_used(self, obj):
        return obj.redemptions_counted().count()

    def validate_code(self, value):
        value = (value or '').strip().upper()
        if not value:
            raise serializers.ValidationError('Code is required.')
        qs = Coupon.all_objects.filter(code=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A coupon with this code already exists.')
        return value

    def validate(self, attrs):
        # Run the model's own rules (percent range, date order, seller-funded needs seller)
        probe = Coupon(**{k: v for k, v in attrs.items() if k != 'categories'})
        if self.instance:
            for f in ('discount_type', 'discount_value', 'valid_from', 'valid_until', 'funded_by', 'seller'):
                if f not in attrs:
                    setattr(probe, f, getattr(self.instance, f))
        try:
            probe.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message_dict if hasattr(e, 'message_dict') else e.messages)
        return attrs
