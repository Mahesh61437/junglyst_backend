import re
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from core.models import User
from orders.bookings import PAID_BOOKING_STATUSES
from .models import (
    RegistrationState, Workshop, WorkshopBooking, WorkshopOption, WorkshopOrganiser, WorkshopPayout, WorkshopSession,
)
from .services import (
    organiser_contact, organiser_terms, paid_booking_count, recalculate_unsettled, unsettled_bookings,
)

MAX_LIST_ITEMS = 20
MAX_LIST_ITEM_LENGTH = 200
MAX_IMAGES = 12
PHONE_PATTERN = re.compile(r'^\+?[0-9 ]{10,15}$')


def _store_name(user):
    profile = getattr(user, 'seller_profile', None)
    return profile.store_name if profile else (user.get_full_name() or user.email)


def _clean_text_list(values, field_label, max_items=MAX_LIST_ITEMS):
    if not isinstance(values, list):
        raise serializers.ValidationError(f"{field_label} must be a list.")
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if len(cleaned) > max_items:
        raise serializers.ValidationError(f"{field_label} can have at most {max_items} entries.")
    if any(len(v) > MAX_LIST_ITEM_LENGTH for v in cleaned):
        raise serializers.ValidationError(f"Each {field_label.lower()} entry must be under {MAX_LIST_ITEM_LENGTH} characters.")
    return cleaned


# ── Public ───────────────────────────────────────────────────────────────────

class PublicOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkshopOption
        fields = ('id', 'name', 'shape', 'size', 'description', 'price')


class PublicSessionSerializer(serializers.ModelSerializer):
    seats_left = serializers.SerializerMethodField()

    class Meta:
        model = WorkshopSession
        fields = ('id', 'date', 'start_time', 'end_time', 'capacity', 'seats_left', 'note')

    def get_seats_left(self, obj):
        return max(obj.capacity - getattr(obj, 'seats_taken', 0), 0)


def _public_seller(user):
    profile = getattr(user, 'seller_profile', None)
    return {
        'name': _store_name(user),
        'slug': profile.slug if profile else None,
        'logo_url': getattr(profile, 'logo_url', None) if profile else None,
    }


def _organiser_users(workshop):
    """Co-hosts in display order (uses the prefetched rows); lead seller as fallback."""
    rows = list(workshop.organisers.all())
    return [o.seller for o in rows] if rows else [workshop.seller]


def _organiser_names(workshop):
    return ' & '.join(_store_name(u) for u in _organiser_users(workshop))


class OrganiserMixin(serializers.Serializer):
    # `organiser` (lead) stays for older clients; `organisers` lists every co-host.
    organiser = serializers.SerializerMethodField()
    organisers = serializers.SerializerMethodField()

    def get_organiser(self, obj):
        return _public_seller(obj.seller)

    def get_organisers(self, obj):
        return [_public_seller(u) for u in _organiser_users(obj)]


class WorkshopCardSerializer(OrganiserMixin, serializers.ModelSerializer):
    cover_image = serializers.CharField(read_only=True)
    price_from = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    next_session_date = serializers.DateField(read_only=True)
    seats_left = serializers.SerializerMethodField()
    upcoming_session_count = serializers.SerializerMethodField()
    registration_open = serializers.SerializerMethodField()
    registration_state = serializers.SerializerMethodField()

    class Meta:
        model = Workshop
        fields = (
            'id', 'slug', 'title', 'tagline', 'category', 'cover_image', 'city', 'venue_name',
            'duration_minutes', 'skill_level', 'is_featured', 'price_from', 'next_session_date',
            'seats_left', 'upcoming_session_count', 'registration_open', 'registration_state',
            'registration_opens_at', 'registration_closes_at', 'organiser', 'organisers',
        )

    def get_seats_left(self, obj):
        # Uses the prefetched, seat-annotated upcoming sessions (no extra queries).
        return sum(max(s.capacity - s.seats_taken, 0) for s in getattr(obj, 'upcoming_sessions', []))

    def get_upcoming_session_count(self, obj):
        return len(getattr(obj, 'upcoming_sessions', []))

    def get_registration_state(self, obj):
        return obj.registration_state(getattr(obj, 'upcoming_sessions', []))

    def get_registration_open(self, obj):
        return self.get_registration_state(obj) == RegistrationState.OPEN


class WorkshopDetailSerializer(OrganiserMixin, serializers.ModelSerializer):
    options = serializers.SerializerMethodField()
    sessions = serializers.SerializerMethodField()
    registration_open = serializers.SerializerMethodField()
    registration_state = serializers.SerializerMethodField()

    class Meta:
        model = Workshop
        fields = (
            'id', 'slug', 'title', 'tagline', 'category', 'description', 'highlights', 'what_to_bring',
            'images', 'venue_name', 'address', 'city', 'map_url', 'duration_minutes', 'skill_level',
            'min_age', 'max_seats_per_booking', 'cancellation_policy', 'registration_opens_at',
            'registration_closes_at', 'registration_open', 'registration_state', 'status', 'options',
            'sessions', 'organiser', 'organisers',
        )

    def get_registration_state(self, obj):
        return obj.registration_state(getattr(obj, 'upcoming_sessions', []))

    def get_registration_open(self, obj):
        return self.get_registration_state(obj) == RegistrationState.OPEN

    def get_options(self, obj):
        return PublicOptionSerializer([o for o in obj.options.all() if o.is_active], many=True).data

    def get_sessions(self, obj):
        # Pre-filtered + annotated upcoming sessions are attached by the view.
        return PublicSessionSerializer(getattr(obj, 'upcoming_sessions', []), many=True).data


class BookingRequestSerializer(serializers.Serializer):
    session_id = serializers.IntegerField()
    option_id = serializers.IntegerField()
    participants = serializers.IntegerField(min_value=1, max_value=50)
    attendee_name = serializers.CharField(max_length=120, trim_whitespace=True)
    attendee_phone = serializers.RegexField(PHONE_PATTERN, error_messages={
        'invalid': 'Enter a valid 10-digit mobile number.',
    })
    attendee_email = serializers.EmailField()
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


# ── Booking views (buyer / seller / admin) ──────────────────────────────────

class BookingSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    order_id = serializers.UUIDField(read_only=True)
    workshop_slug = serializers.CharField(source='workshop.slug', read_only=True)
    venue = serializers.SerializerMethodField()
    organiser_contact = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = WorkshopBooking
        fields = (
            'id', 'reference', 'order_id', 'status', 'status_label', 'workshop', 'workshop_title',
            'workshop_slug', 'option_name', 'session', 'session_date', 'session_start', 'session_end',
            'participants', 'attendee_name', 'attendee_phone', 'attendee_email', 'notes',
            'unit_price', 'total_amount', 'venue', 'organiser_contact', 'confirmed_at', 'created_at',
        )

    def get_venue(self, obj):
        w = obj.workshop
        return {'name': w.venue_name, 'city': w.city, 'address': w.address, 'map_url': w.map_url}

    def get_organiser_contact(self, obj):
        return organiser_contact(obj.workshop)


class SellerBookingSerializer(BookingSerializer):
    """The requesting seller's own slice of each booking (the view prefetches
    their split into `my_splits`)."""
    share_percent = serializers.SerializerMethodField()
    gross_share = serializers.SerializerMethodField()
    commission_percent = serializers.SerializerMethodField()
    commission_amount = serializers.SerializerMethodField()
    seller_payout = serializers.SerializerMethodField()

    class Meta(BookingSerializer.Meta):
        fields = BookingSerializer.Meta.fields + (
            'share_percent', 'gross_share', 'commission_percent', 'commission_amount', 'seller_payout',
        )

    @staticmethod
    def _split(obj):
        splits = getattr(obj, 'my_splits', None)
        return splits[0] if splits else None

    def get_share_percent(self, obj):
        split = self._split(obj)
        return split.share_percent if split else None

    def get_gross_share(self, obj):
        split = self._split(obj)
        return split.gross_amount if split else None

    def get_commission_percent(self, obj):
        split = self._split(obj)
        return split.commission_percent if split else None

    def get_commission_amount(self, obj):
        split = self._split(obj)
        return split.commission_amount if split else None

    def get_seller_payout(self, obj):
        split = self._split(obj)
        return split.seller_payout if split else None


class BookingSplitSerializer(serializers.Serializer):
    seller = serializers.UUIDField(source='seller_id')
    seller_name = serializers.SerializerMethodField()
    share_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    gross_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    commission_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    commission_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    seller_payout = serializers.DecimalField(max_digits=12, decimal_places=2)

    def get_seller_name(self, obj):
        return _store_name(obj.seller)


class AdminBookingSerializer(BookingSerializer):
    organiser_name = serializers.SerializerMethodField()
    payment_status = serializers.CharField(source='order.payment_status', read_only=True)
    splits = BookingSplitSerializer(many=True, read_only=True)

    class Meta(BookingSerializer.Meta):
        fields = BookingSerializer.Meta.fields + (
            'commission_percent', 'commission_amount', 'seller_payout', 'splits',
            'organiser_name', 'payment_status', 'admin_note', 'cancelled_at', 'user',
        )

    def get_organiser_name(self, obj):
        return ' & '.join(_store_name(split.seller) for split in obj.splits.all()) or _store_name(obj.seller)


class AdminBookingUpdateSerializer(serializers.ModelSerializer):
    """Admins can only move a paid booking between its post-payment states."""
    ALLOWED = ('confirmed', 'attended', 'no_show', 'cancelled')

    class Meta:
        model = WorkshopBooking
        fields = ('status', 'admin_note')

    def validate_status(self, value):
        if value not in self.ALLOWED:
            raise serializers.ValidationError("Status can only be set to confirmed, attended, no-show or cancelled.")
        if not self.instance.is_paid and value != self.instance.status:
            raise serializers.ValidationError("Only paid bookings can change status.")
        return value


# ── Admin workshop editor ───────────────────────────────────────────────────

class AdminOptionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = WorkshopOption
        fields = ('id', 'name', 'shape', 'size', 'description', 'price', 'is_active', 'sort_order')


class AdminSessionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    seats_booked = serializers.IntegerField(read_only=True)
    seats_taken = serializers.IntegerField(read_only=True)

    class Meta:
        model = WorkshopSession
        fields = ('id', 'date', 'start_time', 'end_time', 'capacity', 'is_active', 'note',
                  'seats_booked', 'seats_taken')

    def validate(self, attrs):
        if attrs.get('end_time') and attrs.get('start_time') and attrs['end_time'] <= attrs['start_time']:
            raise serializers.ValidationError("Session end time must be after the start time.")
        return attrs


VERIFIED_SELLERS = User.objects.filter(is_verified_seller=True, is_deleted=False)
FULL_SHARE = Decimal('100')


class AdminOrganiserSerializer(serializers.ModelSerializer):
    seller = serializers.PrimaryKeyRelatedField(queryset=VERIFIED_SELLERS)
    seller_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkshopOrganiser
        fields = ('seller', 'seller_name', 'share_percent', 'commission_percent')

    def get_seller_name(self, obj):
        return _store_name(obj.seller)


def _terms_signature(terms):
    return [(str(t.seller_id), Decimal(t.share_percent), Decimal(t.commission_percent)) for t in terms]


class AdminWorkshopSerializer(serializers.ModelSerializer):
    # Legacy single-organiser input: used only when `organisers` isn't sent.
    seller = serializers.PrimaryKeyRelatedField(queryset=VERIFIED_SELLERS, required=False)
    organisers = AdminOrganiserSerializer(many=True, required=False)
    organiser_name = serializers.SerializerMethodField()
    options = AdminOptionSerializer(many=True, required=False)
    sessions = serializers.SerializerMethodField()
    session_inputs = AdminSessionSerializer(many=True, required=False, write_only=True, source='sessions')
    # Asked on save when co-host terms change and paid bookings exist.
    recalculate_unsettled = serializers.BooleanField(required=False, default=False, write_only=True)
    paid_booking_count = serializers.SerializerMethodField()
    recalculable_booking_count = serializers.SerializerMethodField()

    class Meta:
        model = Workshop
        fields = (
            'id', 'slug', 'title', 'tagline', 'category', 'description', 'highlights', 'what_to_bring',
            'images', 'seller', 'organiser_name', 'status', 'is_featured', 'registration_opens_at',
            'registration_closes_at', 'venue_name', 'address', 'city', 'map_url', 'duration_minutes',
            'skill_level', 'min_age', 'max_seats_per_booking', 'cancellation_policy', 'commission_percent',
            'contact_name', 'contact_phone', 'organisers', 'options', 'sessions', 'session_inputs', 'recalculate_unsettled',
            'paid_booking_count', 'recalculable_booking_count', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'slug', 'created_at', 'updated_at')

    def get_organiser_name(self, obj):
        return _organiser_names(obj)

    def get_paid_booking_count(self, obj):
        return paid_booking_count(obj)

    def get_recalculable_booking_count(self, obj):
        return unsettled_bookings(obj).count()

    def validate_contact_phone(self, value):
        value = (value or '').strip()
        if value and not PHONE_PATTERN.match(value):
            raise serializers.ValidationError("Enter a valid 10-digit mobile number.")
        return value

    def validate_organisers(self, rows):
        if not rows:
            raise serializers.ValidationError("Add at least one organising seller.")
        sellers = [r['seller'].pk for r in rows]
        if len(set(sellers)) != len(sellers):
            raise serializers.ValidationError("Each seller can be added only once.")
        total = sum((Decimal(r['share_percent']) for r in rows), Decimal('0'))
        if total != FULL_SHARE:
            raise serializers.ValidationError(f"Revenue shares must add up to 100% (currently {total}%).")
        return rows

    def get_sessions(self, obj):
        sessions = getattr(obj, 'annotated_sessions', None)
        if sessions is None:
            sessions = obj.sessions.with_seats_taken().order_by('date', 'start_time')
        return AdminSessionSerializer(sessions, many=True).data

    def validate_highlights(self, value):
        return _clean_text_list(value, 'Highlights')

    def validate_what_to_bring(self, value):
        return _clean_text_list(value, 'What to bring')

    def validate_images(self, value):
        images = _clean_text_list(value, 'Images', max_items=MAX_IMAGES)
        url_field = serializers.URLField(max_length=1000)
        for url in images:
            url_field.run_validation(url)
        return images

    def validate(self, attrs):
        opens = attrs.get('registration_opens_at', getattr(self.instance, 'registration_opens_at', None))
        closes = attrs.get('registration_closes_at', getattr(self.instance, 'registration_closes_at', None))
        if opens and closes and closes <= opens:
            raise serializers.ValidationError({'registration_closes_at': "Registration must close after it opens."})
        if self.instance is None and 'organisers' not in attrs and 'seller' not in attrs:
            raise serializers.ValidationError({'organisers': "Add at least one organising seller."})
        status = attrs.get('status', getattr(self.instance, 'status', None))
        if status == 'published':
            options = attrs.get('options')
            has_options = (any(o.get('is_active', True) for o in options) if options is not None
                           else bool(self.instance and self.instance.options.filter(is_active=True).exists()))
            sessions = attrs.get('sessions')
            has_sessions = (any(s.get('is_active', True) for s in sessions) if sessions is not None
                            else bool(self.instance and self.instance.sessions.filter(is_active=True).exists()))
            if not has_options or not has_sessions:
                raise serializers.ValidationError(
                    "Add at least one active option and one session before publishing.")
        return attrs

    def _organiser_rows(self, validated_data, workshop=None):
        rows = validated_data.pop('organisers', None)
        seller = validated_data.pop('seller', None)
        if rows is None and seller is not None:
            commission = validated_data.get('commission_percent', getattr(workshop, 'commission_percent', None))
            rows = [{'seller': seller, 'share_percent': FULL_SHARE, 'commission_percent': commission}]
        if rows:
            validated_data['seller'] = rows[0]['seller']  # lead organiser
        return rows

    @staticmethod
    def _sync_organisers(workshop, rows):
        workshop.organisers.all().delete()
        WorkshopOrganiser.objects.bulk_create([
            WorkshopOrganiser(workshop=workshop, seller=r['seller'], share_percent=r['share_percent'],
                              commission_percent=r['commission_percent'], sort_order=i)
            for i, r in enumerate(rows)
        ])

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop('recalculate_unsettled', None)
        options = validated_data.pop('options', [])
        sessions = validated_data.pop('sessions', [])
        organisers = self._organiser_rows(validated_data)
        if 'commission_percent' not in validated_data and organisers:
            validated_data['commission_percent'] = organisers[0]['commission_percent']
        workshop = Workshop.objects.create(**validated_data)
        self._sync_organisers(workshop, organisers)
        self._sync_children(workshop.options, WorkshopOption, options, workshop)
        self._sync_sessions(workshop, sessions)
        return workshop

    @transaction.atomic
    def update(self, instance, validated_data):
        recalculate = validated_data.pop('recalculate_unsettled', False)
        options = validated_data.pop('options', None)
        sessions = validated_data.pop('sessions', None)
        organisers = self._organiser_rows(validated_data, instance)
        before = _terms_signature(organiser_terms(instance))
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if organisers is not None:
            self._sync_organisers(instance, organisers)
        if options is not None:
            self._sync_children(instance.options, WorkshopOption, options, instance)
        if sessions is not None:
            self._sync_sessions(instance, sessions)
        self.recalculated_count = 0
        if recalculate and _terms_signature(organiser_terms(instance)) != before:
            self.recalculated_count = recalculate_unsettled(instance)
        return instance

    def _sync_sessions(self, workshop, rows):
        """Like _sync_children, plus: capacity can't drop below seats already
        sold, and a moved session updates its bookings and tells attendees."""
        existing = {s.id: s for s in workshop.sessions.with_seats_taken()}
        for row in rows:
            session = existing.get(row.get('id'))
            # Only block *lowering* below seats sold; an already-overbooked
            # session (late payment) must not lock the rest of the form.
            if (session and row.get('capacity') is not None and row['capacity'] < session.seats_booked
                    and row['capacity'] < session.capacity):
                raise serializers.ValidationError({'session_inputs': (
                    f"{session.date:%d %b} {session.start_time:%H:%M} already has {session.seats_booked} "
                    f"seats sold, so its capacity can't be lower than that."
                )})
        moved = []
        for row in rows:
            session = existing.get(row.get('id'))
            if session is None:
                continue
            new_timing = (row.get('date', session.date), row.get('start_time', session.start_time),
                          row.get('end_time', session.end_time))
            if new_timing != (session.date, session.start_time, session.end_time):
                moved.append((session.id, new_timing, f"{session.date:%d %b %Y}, {session.start_time:%I:%M %p}"))
        self._sync_children(workshop.sessions, WorkshopSession, rows, workshop)
        for session_id, (day, start, end), previous_label in moved:
            WorkshopBooking.objects.filter(session_id=session_id).update(
                session_date=day, session_start=start, session_end=end)
            if WorkshopBooking.objects.filter(session_id=session_id, status__in=PAID_BOOKING_STATUSES).exists():
                transaction.on_commit(lambda sid=session_id, label=previous_label: _notify_rescheduled(sid, label))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if hasattr(self, 'recalculated_count'):
            data['recalculated_count'] = self.recalculated_count
        return data

    @staticmethod
    def _sync_children(related_manager, model, rows, workshop):
        """Upsert by id. Rows missing from the payload are deleted, or just
        deactivated if bookings already reference them."""
        existing = {obj.id: obj for obj in related_manager.all()}
        kept_ids = set()
        for row in rows:
            row = dict(row)
            row_id = row.pop('id', None)
            obj = existing.get(row_id)
            if obj is None:
                obj = model.objects.create(workshop=workshop, **row)
            else:
                for field, value in row.items():
                    setattr(obj, field, value)
                obj.save()
            kept_ids.add(obj.id)
        removed_ids = set(existing) - kept_ids
        if not removed_ids:
            return
        removed = model.objects.filter(id__in=removed_ids)
        booked_ids = set(removed.filter(bookings__isnull=False).values_list('id', flat=True))
        removed.filter(id__in=booked_ids).update(is_active=False)
        removed.exclude(id__in=booked_ids).delete()


def _notify_rescheduled(session_id, previous_label):
    import logging
    from .tasks import send_session_rescheduled_task
    try:
        send_session_rescheduled_task.delay(session_id, previous_label)
    except Exception:
        logging.getLogger(__name__).exception("Could not queue reschedule emails for session %s", session_id)


class AdminWorkshopListSerializer(serializers.ModelSerializer):
    organiser_name = serializers.SerializerMethodField()
    cover_image = serializers.CharField(read_only=True)
    next_session_date = serializers.DateField(read_only=True)
    session_count = serializers.IntegerField(read_only=True)
    seats_booked = serializers.IntegerField(read_only=True)
    capacity = serializers.IntegerField(read_only=True)
    has_overbooked_session = serializers.BooleanField(read_only=True)

    class Meta:
        model = Workshop
        fields = (
            'id', 'slug', 'title', 'category', 'status', 'is_featured', 'cover_image', 'city', 'has_overbooked_session',
            'organiser_name', 'commission_percent', 'registration_opens_at', 'registration_closes_at',
            'next_session_date', 'session_count', 'seats_booked', 'capacity', 'updated_at',
        )

    def get_organiser_name(self, obj):
        return _organiser_names(obj)


class PayoutSerializer(serializers.ModelSerializer):
    seller = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_deleted=False), required=False)

    class Meta:
        model = WorkshopPayout
        fields = ('id', 'workshop', 'seller', 'amount', 'paid_on', 'reference', 'notes', 'created_at')
        read_only_fields = ('id', 'created_at')

    def validate(self, attrs):
        workshop = attrs['workshop']
        seller = attrs.get('seller') or workshop.seller
        is_cohost = workshop.organisers.filter(seller=seller).exists() or workshop.seller_id == seller.pk
        has_bookings = workshop.bookings.filter(splits__seller=seller).exists()
        if not (is_cohost or has_bookings):
            raise serializers.ValidationError({'seller': "This seller isn't an organiser of this workshop."})
        attrs['seller'] = seller
        return attrs
