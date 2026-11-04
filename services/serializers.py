from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from core.models import User
from shipping.regions import STATE_NAMES

from . import validators
from .permissions import assignable_sellers
from .models import (
    MONEY_MAX_DIGITS, UNPAID_STATUSES, AdditionalCharge, QuoteStatus, QuoteEvent, QuoteRequest, ServiceFormField, ServiceGroup, ServiceOffering,
)


def _money_field(**kwargs):
    return serializers.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2, **kwargs)


# ── Public catalogue ─────────────────────────────────────────────────────────

class ServiceFormFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceFormField
        fields = ('key', 'label', 'field_type', 'required', 'choices', 'min_value', 'max_value',
                  'max_length', 'help_text', 'sort_order')


class OfferingCardSerializer(serializers.ModelSerializer):
    group_slug = serializers.CharField(source='group.slug', read_only=True)
    delivery_state_names = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = ServiceOffering
        fields = ('name', 'slug', 'description', 'image_url', 'group_slug', 'suggested_min_budget', 'delivery_state_names')


class OfferingDetailSerializer(OfferingCardSerializer):
    form_fields = ServiceFormFieldSerializer(many=True, read_only=True)
    group_name = serializers.CharField(source='group.name', read_only=True)
    delivery_states = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta(OfferingCardSerializer.Meta):
        fields = OfferingCardSerializer.Meta.fields + ('form_fields', 'group_name', 'delivery_states')


class GroupSerializer(serializers.ModelSerializer):
    """A group with its active offerings (prefetched by the view as `active_offerings`)."""
    offerings = OfferingCardSerializer(source='active_offerings', many=True, read_only=True)

    class Meta:
        model = ServiceGroup
        fields = ('name', 'slug', 'description', 'image_url', 'show_on_home', 'home_sort_order', 'offerings')


# ── Customer: quote request ──────────────────────────────────────────────────

class QuoteRequestCreateSerializer(serializers.Serializer):
    contact_name = serializers.CharField(max_length=120)
    contact_phone = serializers.CharField(max_length=20)
    contact_email = serializers.EmailField()
    state = serializers.CharField(max_length=60)
    budget_min = _money_field()
    budget_max = _money_field()
    answers = serializers.DictField(required=False, default=dict)
    reference_images = serializers.ListField(child=serializers.CharField(max_length=500), required=False, default=list)
    customer_notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default='')
    # Honeypot: real users never see or fill this.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_contact_name(self, value):
        return validators.validate_person_name(value)

    def validate_contact_phone(self, value):
        return validators.normalise_phone(value)

    def validate_contact_email(self, value):
        return value.strip().lower()

    def validate_state(self, value):
        return validators.validate_state(value)

    def validate_reference_images(self, value):
        return validators.validate_reference_images(value)

    def validate_website(self, value):
        if value:
            raise serializers.ValidationError('Invalid submission.')
        return value

    def validate(self, attrs):
        offering = self.context['offering']
        validators.validate_budget(attrs['budget_min'], attrs['budget_max'])
        if offering.delivery_states and attrs['state'] not in offering.delivery_states:
            names = ', '.join(offering.delivery_state_names)
            raise serializers.ValidationError({'state': f'We currently deliver this only to: {names}.'})
        snapshot = validators.snapshot_form(offering.form_fields.all())
        attrs['answers'] = validators.validate_answers(snapshot, attrs.get('answers', {}))
        attrs['form_snapshot'] = snapshot
        attrs.pop('website', None)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        state = validated_data.pop('state')
        return QuoteRequest.objects.create(
            offering=self.context['offering'], user=self.context.get('user'), state_code=state, **validated_data,
        )


def _answer_rows(quote):
    """Questions with the customer's answer, in form order, for display and email."""
    return [
        {'label': field['label'], 'value': quote.answers.get(field['key'])}
        for field in quote.form_snapshot if field['key'] in quote.answers
    ]


class QuoteInclusionSerializer(serializers.Serializer):
    label = serializers.CharField()
    amount = serializers.CharField(allow_null=True)


class SellerBriefSerializer(serializers.ModelSerializer):
    store_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'store_name')

    def get_store_name(self, user):
        profile = getattr(user, 'seller_profile', None)
        return profile.store_name if profile else (user.get_full_name() or user.email)


class AdditionalChargeSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = AdditionalCharge
        fields = ('id', 'description', 'amount', 'status', 'status_label', 'created_at', 'paid_at')


class AdditionalChargeStaffSerializer(AdditionalChargeSerializer):
    """What staff see: also the seller's share and whether it still has to be paid separately."""
    payout_status_label = serializers.CharField(source='get_payout_status_display', read_only=True)

    class Meta(AdditionalChargeSerializer.Meta):
        fields = AdditionalChargeSerializer.Meta.fields + ('seller_payout', 'payout_status', 'payout_status_label')


class AdditionalChargeCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200)
    amount = _money_field()

    def validate_description(self, value):
        value = ' '.join(value.split())
        if len(value) < 3:
            raise serializers.ValidationError('Describe what the extra payment is for.')
        return value

    def validate_amount(self, value):
        return validators.validate_money(value, 'amount', allow_zero=False)


class QuoteRequestSerializer(serializers.ModelSerializer):
    """Customer's own request. Shows the quote only once the pay request is sent."""
    offering_name = serializers.CharField(source='offering.name', read_only=True)
    group_name = serializers.CharField(source='offering.group.name', read_only=True)
    state = serializers.SerializerMethodField()
    answer_rows = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    quote = serializers.SerializerMethodField()
    order_number = serializers.SerializerMethodField()
    additional_charges = AdditionalChargeSerializer(many=True, read_only=True)
    can_cancel = serializers.SerializerMethodField()
    delivery_states = serializers.ListField(source='offering.delivery_states', child=serializers.CharField(), read_only=True)
    delivery_state_names = serializers.ListField(source='offering.delivery_state_names', child=serializers.CharField(), read_only=True)

    class Meta:
        model = QuoteRequest
        fields = ('id', 'reference', 'offering_name', 'group_name', 'status', 'status_label', 'state',
                  'budget_min', 'budget_max', 'answer_rows', 'reference_images', 'customer_notes',
                  'quote', 'order_number', 'additional_charges', 'delivery_states', 'delivery_state_names',
                  'pay_by', 'paid_at', 'cancel_reason', 'cancel_requested_at', 'can_cancel', 'created_at')

    def get_state(self, quote):
        return STATE_NAMES.get(quote.state_code, quote.state_code)

    def get_answer_rows(self, quote):
        return _answer_rows(quote)

    def get_order_number(self, quote):
        return quote.order.order_number if quote.order_id else None

    def get_can_cancel(self, quote):
        """Unpaid requests can be cancelled outright; a paid one can be asked about, once."""
        if quote.status == QuoteStatus.PAID:
            return quote.cancel_requested_at is None
        return quote.status in UNPAID_STATUSES

    def get_quote(self, quote):
        if quote.quote_sent_at is None:
            return None
        return _quote_figures(quote)


def _quote_figures(quote):
    return {
        'item_price': quote.item_price,
        'shipping_fee': quote.shipping_fee,
        'total_amount': quote.total_amount,
        'shape': quote.shape,
        'length_cm': quote.length_cm,
        'width_cm': quote.width_cm,
        'height_cm': quote.height_cm,
        'inclusions': quote.inclusions,
        'notes': quote.quote_notes,
    }


# ── Quote details (super admin or assigned seller) ───────────────────────────

class QuoteDetailsSerializer(serializers.Serializer):
    """Partial update of the quote figures. Totals are always computed server-side."""
    item_price = _money_field(required=False)
    shipping_fee = _money_field(required=False)
    shape = serializers.CharField(max_length=60, required=False, allow_blank=True)
    length_cm = serializers.DecimalField(max_digits=7, decimal_places=1, required=False, allow_null=True)
    width_cm = serializers.DecimalField(max_digits=7, decimal_places=1, required=False, allow_null=True)
    height_cm = serializers.DecimalField(max_digits=7, decimal_places=1, required=False, allow_null=True)
    inclusions = serializers.ListField(child=serializers.DictField(), required=False)
    quote_notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def validate_item_price(self, value):
        return validators.validate_money(value, 'item_price', allow_zero=False)

    def validate_shipping_fee(self, value):
        return validators.validate_money(value, 'shipping_fee')

    def validate_length_cm(self, value):
        return validators.validate_dimension(value, 'length_cm')

    def validate_width_cm(self, value):
        return validators.validate_dimension(value, 'width_cm')

    def validate_height_cm(self, value):
        return validators.validate_dimension(value, 'height_cm')

    def validate_inclusions(self, value):
        return validators.validate_inclusions(value)

    def validate_shape(self, value):
        return ' '.join(value.split())


class AdminQuoteDetailsSerializer(QuoteDetailsSerializer):
    admin_notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)


# ── Admin / seller views of a request ────────────────────────────────────────

class QuoteEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = QuoteEvent
        fields = ('action', 'changes', 'actor_name', 'created_at')

    def get_actor_name(self, event):
        if event.actor is None:
            return 'System'
        return event.actor.get_full_name() or event.actor.email


class QuoteListSerializer(serializers.ModelSerializer):
    offering_name = serializers.CharField(source='offering.name', read_only=True)
    group_name = serializers.CharField(source='offering.group.name', read_only=True)
    state = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    assigned_seller = SellerBriefSerializer(read_only=True)
    total_amount = serializers.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=2, read_only=True)

    class Meta:
        model = QuoteRequest
        fields = ('id', 'reference', 'offering_name', 'group_name', 'status', 'status_label', 'contact_name',
                  'contact_phone', 'state', 'budget_min', 'budget_max', 'assigned_seller', 'total_amount',
                  'cancel_requested_at', 'created_at')

    def get_state(self, quote):
        return STATE_NAMES.get(quote.state_code, quote.state_code)


class QuoteStaffDetailSerializer(QuoteListSerializer):
    """Full request for the admin and the assigned seller."""
    answer_rows = serializers.SerializerMethodField()
    quote = serializers.SerializerMethodField()
    order_number = serializers.SerializerMethodField()
    events = QuoteEventSerializer(many=True, read_only=True)
    additional_charges = AdditionalChargeStaffSerializer(many=True, read_only=True)

    class Meta(QuoteListSerializer.Meta):
        fields = QuoteListSerializer.Meta.fields + (
            'contact_email', 'answer_rows', 'reference_images', 'customer_notes', 'quote', 'admin_notes',
            'quote_sent_at', 'pay_by', 'cancel_reason', 'cancel_request_reason', 'paid_at', 'order_number', 'events',
            'additional_charges',
        )

    def get_answer_rows(self, quote):
        return _answer_rows(quote)

    def get_quote(self, quote):
        return _quote_figures(quote)

    def get_order_number(self, quote):
        return quote.order.order_number if quote.order_id else None


# Sellers quote and ship; customer contact stays with Junglyst (delivery details reach the
# seller through the normal fulfilment flow once the order is paid).
SELLER_HIDDEN_FIELDS = ('admin_notes', 'events', 'contact_phone', 'contact_email', 'cancel_requested_at', 'cancel_request_reason')


class SellerQuoteListSerializer(QuoteListSerializer):
    class Meta(QuoteListSerializer.Meta):
        fields = tuple(f for f in QuoteListSerializer.Meta.fields if f not in SELLER_HIDDEN_FIELDS)


class SellerQuoteDetailSerializer(QuoteStaffDetailSerializer):
    """No private admin notes, history log or customer contact details for sellers."""

    class Meta(QuoteStaffDetailSerializer.Meta):
        fields = tuple(f for f in QuoteStaffDetailSerializer.Meta.fields if f not in SELLER_HIDDEN_FIELDS)


class AssignSellerSerializer(serializers.Serializer):
    seller_id = serializers.UUIDField()

    def validate_seller_id(self, value):
        seller = assignable_sellers().filter(id=value).first()
        if seller is None:
            raise serializers.ValidationError('Choose a verified, active seller.')
        self.seller = seller
        return value


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class DeclineSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


# ── Pay: delivery address ────────────────────────────────────────────────────

class PayAddressSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)
    address_line1 = serializers.CharField(min_length=5, max_length=200)
    address_line2 = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    city = serializers.CharField(min_length=2, max_length=80)
    state = serializers.CharField(max_length=60)
    pincode = serializers.CharField(max_length=6)

    def validate_full_name(self, value):
        return validators.validate_person_name(value)

    def validate_phone(self, value):
        return validators.normalise_phone(value)

    def validate_state(self, value):
        return validators.validate_state(value)

    def validate(self, attrs):
        quote = self.context['quote']
        try:
            attrs['pincode'] = validators.validate_pincode(attrs['pincode'], attrs['state'])
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({'pincode': exc.detail})
        delivery_states = quote.offering.delivery_states
        if delivery_states and attrs['state'] not in delivery_states:
            raise serializers.ValidationError(
                {'state': f'We currently deliver this only to: {", ".join(quote.offering.delivery_state_names)}.'}
            )
        if not validators.seller_ships_to(quote.assigned_seller, attrs['state']):
            raise serializers.ValidationError(
                {'state': f'The seller preparing this order cannot deliver to {STATE_NAMES[attrs["state"]]}.'}
            )
        return attrs

    def shipping_address(self):
        data = self.validated_data
        return {
            'full_name': data['full_name'], 'phone': data['phone'],
            'email': data.get('email') or self.context['quote'].contact_email,
            'address_line1': data['address_line1'], 'address_line2': data['address_line2'],
            'city': data['city'], 'state': STATE_NAMES[data['state']], 'state_code': data['state'],
            'pincode': data['pincode'],
        }


# ── Super admin: catalogue configuration ─────────────────────────────────────

def clean_state_codes(value):
    if not isinstance(value, list):
        raise serializers.ValidationError('Provide a list of state codes.')
    unknown = [code for code in value if code not in STATE_NAMES]
    if unknown:
        raise serializers.ValidationError(f'Unknown state codes: {", ".join(map(str, unknown))}.')
    return list(dict.fromkeys(value))


class AdminFormFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceFormField
        fields = ('id', 'key', 'label', 'field_type', 'required', 'choices', 'min_value', 'max_value',
                  'max_length', 'help_text', 'sort_order')

    def validate(self, attrs):
        validators.validate_form_field_definition(
            attrs.get('field_type'), attrs.get('choices', []), attrs.get('min_value'), attrs.get('max_value'),
        )
        return attrs


class AdminOfferingSerializer(serializers.ModelSerializer):
    form_fields = AdminFormFieldSerializer(many=True, required=False)
    group_name = serializers.CharField(source='group.name', read_only=True)

    class Meta:
        model = ServiceOffering
        fields = ('id', 'group', 'group_name', 'name', 'slug', 'description', 'image_url', 'is_active',
                  'sort_order', 'allowed_states', 'suggested_min_budget', 'commission_percent', 'form_fields')

    def validate_allowed_states(self, value):
        return clean_state_codes(value)

    def validate_commission_percent(self, value):
        if value > Decimal('100'):
            raise serializers.ValidationError('Commission cannot exceed 100%.')
        return value

    def validate_form_fields(self, fields):
        keys = [field['key'] for field in fields]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError('Each question needs a unique key.')
        return fields

    @transaction.atomic
    def create(self, validated_data):
        fields = validated_data.pop('form_fields', [])
        offering = ServiceOffering.objects.create(**validated_data)
        ServiceFormField.objects.bulk_create(ServiceFormField(offering=offering, **field) for field in fields)
        return offering

    @transaction.atomic
    def update(self, offering, validated_data):
        """`form_fields`, when sent, replaces the whole question list (old requests keep their snapshot)."""
        fields = validated_data.pop('form_fields', None)
        for attr, value in validated_data.items():
            setattr(offering, attr, value)
        offering.save()
        if fields is not None:
            offering.form_fields.all().delete()
            ServiceFormField.objects.bulk_create(ServiceFormField(offering=offering, **field) for field in fields)
        return offering


class AdminGroupSerializer(serializers.ModelSerializer):
    offering_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ServiceGroup
        fields = ('id', 'name', 'slug', 'description', 'image_url', 'is_active', 'show_on_home',
                  'home_sort_order', 'allowed_states', 'offering_count')

    def validate_allowed_states(self, value):
        return clean_state_codes(value)
