from django.core.cache import cache
from django.db import transaction
from rest_framework import serializers
from core.models import Category
from shipping.regions import REGION_CODES, STATE_CODES, STATE_REGIONS
from .models import (
    SellerProfile, SellerBlackoutDate, SellerExcludedState, SellerCategoryRegion, REGION_SHIPS_FIELDS,
)
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


class AdminSellerProfileSerializer(SellerProfileSerializer):
    """Admin-only variant — surfaces the seller's commission config from the
    linked User. NEVER use this in seller-facing or buyer-facing responses.
    """
    seller_commission_rate = serializers.DecimalField(
        source='user.seller_commission_rate', max_digits=5, decimal_places=2, required=False,
    )
    buyer_commission_rate = serializers.DecimalField(
        source='user.buyer_commission_rate', max_digits=5, decimal_places=2, required=False,
    )
    price_is_buyer_final = serializers.BooleanField(
        source='user.price_is_buyer_final', required=False,
    )

    def update(self, instance, validated_data):
        user_data = validated_data.pop('user', {})
        if user_data:
            user = instance.user
            for attr, value in user_data.items():
                setattr(user, attr, value)
            # Run model-level validation (e.g., s+b<100 when toggle ON).
            user.full_clean(exclude=[f.name for f in user._meta.fields if f.name not in user_data])
            user.save(update_fields=list(user_data.keys()))
        return super().update(instance, validated_data)


MAX_REGION_DELIVERY_FEE = 5000


MAX_CATEGORY_RULES = 200


class CategoryRegionRuleSerializer(serializers.Serializer):
    """One category restricted to a subset of regions."""
    category_id = serializers.IntegerField(min_value=1)
    regions = serializers.ListField(
        child=serializers.ChoiceField(choices=REGION_CODES), allow_empty=False, max_length=len(REGION_CODES),
    )


class SellerDeliveryRegionsSerializer(serializers.ModelSerializer):
    """
    Delivery settings for one seller (used by the seller and by superadmins):
    regions they ship to, states inside those regions they won't deliver to,
    per-category region restrictions, and an optional flat fee per region.
    `excluded_states` and `category_regions` each replace the whole set on write.
    """
    service_regions = serializers.ListField(read_only=True)
    excluded_states = serializers.ListField(
        child=serializers.ChoiceField(choices=STATE_CODES), required=False, allow_empty=True,
        max_length=len(STATE_CODES), source='excluded_state_codes',
    )
    # Written as [{category_id, regions}]; read back with category names (see to_representation)
    category_regions = CategoryRegionRuleSerializer(
        many=True, required=False, write_only=True, max_length=MAX_CATEGORY_RULES,
    )

    class Meta:
        model = SellerProfile
        fields = (
            'ships_to_south', 'ships_to_north', 'ships_to_north_east',
            'delivery_fee_south', 'delivery_fee_north', 'delivery_fee_north_east',
            'excluded_states', 'category_regions', 'service_regions',
        )
        extra_kwargs = {
            fee_field: {'min_value': 0, 'max_value': MAX_REGION_DELIVERY_FEE, 'allow_null': True, 'required': False}
            for fee_field in ('delivery_fee_south', 'delivery_fee_north', 'delivery_fee_north_east')
        }

    def _validate_whole_rupees(self, fee):
        # Checkout charges whole rupees (slab fees are ints too), so reject paise
        # rather than silently truncating what the buyer was shown.
        if fee is not None and fee != fee.to_integral_value():
            raise serializers.ValidationError('Enter the fee in whole rupees.')
        return fee

    def validate_delivery_fee_south(self, fee):
        return self._validate_whole_rupees(fee)

    def validate_delivery_fee_north(self, fee):
        return self._validate_whole_rupees(fee)

    def validate_delivery_fee_north_east(self, fee):
        return self._validate_whole_rupees(fee)

    def validate(self, attrs):
        ships_fields = ('ships_to_south', 'ships_to_north', 'ships_to_north_east')
        ships_any = any(
            attrs.get(field, getattr(self.instance, field, True)) for field in ships_fields
        )
        if not ships_any:
            raise serializers.ValidationError('Select at least one region you deliver to.')

        excluded = set(attrs.get('excluded_state_codes', self._current_exclusions()))
        served_states = {
            code for code, region in STATE_REGIONS.items()
            if attrs.get(REGION_SHIPS_FIELDS[region], getattr(self.instance, REGION_SHIPS_FIELDS[region], True))
        }
        if not served_states - excluded:
            raise serializers.ValidationError('Keep at least one state you deliver to.')
        return attrs

    def validate_category_regions(self, rules):
        # PATCH is partial, and DRF then skips `required` on nested fields too
        if any('category_id' not in rule or not rule.get('regions') for rule in rules):
            raise serializers.ValidationError('Each rule needs a category_id and at least one region.')
        category_ids = [rule['category_id'] for rule in rules]
        if len(category_ids) != len(set(category_ids)):
            raise serializers.ValidationError('Each category can only have one delivery rule.')
        known = set(Category.objects.filter(id__in=category_ids).values_list('id', flat=True))
        unknown = sorted(set(category_ids) - known)
        if unknown:
            raise serializers.ValidationError(f'Unknown category id(s): {", ".join(map(str, unknown))}.')
        return rules

    def _current_exclusions(self):
        return self.instance.excluded_state_codes if self.instance else []

    @staticmethod
    def _rule_map(rules) -> dict:
        """{category_id: frozenset(regions)}, dropping rules that allow every region (= no restriction)."""
        result = {}
        for rule in rules:
            regions = frozenset(rule['regions'])
            if regions != frozenset(REGION_CODES):
                result[rule['category_id']] = regions
        return result

    def _current_rule_map(self, instance) -> dict:
        return {rule.category_id: frozenset(rule.regions) for rule in instance.category_regions.all()}

    def _replace_category_rules(self, instance, rules) -> bool:
        desired = self._rule_map(rules)
        if desired == self._current_rule_map(instance):
            return False
        instance.category_regions.all().delete()
        SellerCategoryRegion.objects.bulk_create([
            SellerCategoryRegion(
                seller=instance, category_id=category_id,
                **{field: region in regions for region, field in REGION_SHIPS_FIELDS.items()},
            )
            for category_id, regions in desired.items()
        ])
        return True

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['category_regions'] = [
            {'category_id': rule.category_id, 'category_name': rule.category.name, 'regions': rule.regions}
            for rule in instance.category_regions.select_related('category').order_by('category__name')
        ]
        # Categories the seller lists products in (shown first in the UI)…
        data['seller_categories'] = list(
            Category.objects.filter(products__seller_id=instance.user_id)
            .distinct().order_by('name').values('id', 'name')
        )
        # …and every category, so rules can be set before listing products in one
        data['all_categories'] = list(Category.objects.order_by('name').values('id', 'name'))
        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        new_exclusions = validated_data.pop('excluded_state_codes', None)
        new_category_rules = validated_data.pop('category_regions', None)
        instance = super().update(instance, validated_data)
        feeds_changed = False
        if new_exclusions is not None and set(new_exclusions) != set(self._current_exclusions()):
            instance.excluded_states.all().delete()
            SellerExcludedState.objects.bulk_create([
                SellerExcludedState(seller=instance, state_code=code) for code in sorted(set(new_exclusions))
            ])
            feeds_changed = True
        if new_category_rules is not None:
            feeds_changed = self._replace_category_rules(instance, new_category_rules) or feeds_changed
        if feeds_changed:
            # bulk_create skips signals; region/state-filtered shop feeds must refresh
            cache.clear()
        return instance
