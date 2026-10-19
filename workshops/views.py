from datetime import datetime

from django.db.models import Count, Exists, F, Min, OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.models import User
from core.permissions import IsAdminUser
from orders.bookings import BookingStatus, PAID_BOOKING_STATUSES
from orders.serializers import OrderSuccessSerializer
from payments.checkout import GatewayUnavailable, PaymentCustomer, start_gateway_payment

from .models import (
    OptionPricing, Workshop, WorkshopBooking, WorkshopBookingSplit, WorkshopOption, WorkshopOrganiser,
    WorkshopPayout, WorkshopSession, WorkshopStatus,
)
from .serializers import (
    AdminBookingSerializer, AdminBookingUpdateSerializer, AdminWorkshopListSerializer,
    AdminWorkshopSerializer, BookingRequestSerializer, BookingSerializer, PayoutSerializer,
    SellerBookingSerializer, WorkshopCardSerializer, WorkshopDetailSerializer,
)
from .services import (
    BookingError, BookingRequest, create_pending_booking, financials_overview, release_abandoned_hold,
    seller_overview, workshop_financials,
)

PUBLIC_STATUSES = (WorkshopStatus.PUBLISHED, WorkshopStatus.COMPLETED)


def _upcoming_sessions_qs():
    return WorkshopSession.objects.upcoming().with_seats_taken().order_by('date', 'start_time')


def _seller_qs():
    return User.objects.select_related('seller_profile')


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else None
    except ValueError:
        return None


# ── Public ───────────────────────────────────────────────────────────────────

class WorkshopListView(generics.ListAPIView):
    """GET /api/workshops/?category=terrarium&city=Kochi"""
    permission_classes = (permissions.AllowAny,)
    serializer_class = WorkshopCardSerializer
    filter_backends = ()

    def get_queryset(self):
        next_session = (WorkshopSession.objects.upcoming().filter(workshop=OuterRef('pk'))
                        .order_by('date', 'start_time').values('date')[:1])
        active_options = WorkshopOption.objects.filter(workshop=OuterRef('pk'), is_active=True)
        cheapest_option = active_options.order_by('price')
        qs = (
            Workshop.objects.filter(status__in=PUBLIC_STATUSES)
            .select_related('seller__seller_profile')
            .prefetch_related(
                Prefetch('sessions', queryset=_upcoming_sessions_qs(), to_attr='upcoming_sessions'),
                Prefetch('organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')),
            )
            .annotate(
                price_from=Min('options__price', filter=Q(options__is_active=True)),
                # Is the "from" price per person or per shared terrarium?
                price_from_pricing=Subquery(cheapest_option.values('pricing')[:1]),
                has_group_option=Exists(active_options.filter(pricing=OptionPricing.PER_BUILD)),
                next_session_date=Subquery(next_session),
            )
        )
        params = self.request.query_params
        if params.get('category'):
            qs = qs.filter(category=params['category'])
        if params.get('city'):
            qs = qs.filter(city__iexact=params['city'].strip())
        # Upcoming first (soonest session), past/completed ones last.
        return qs.order_by('-is_featured', 'next_session_date', '-created_at')


class WorkshopDetailView(generics.RetrieveAPIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = WorkshopDetailSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return (
            Workshop.objects.filter(status__in=PUBLIC_STATUSES)
            .select_related('seller__seller_profile')
            .prefetch_related(
                'options',
                Prefetch('sessions', queryset=_upcoming_sessions_qs(), to_attr='upcoming_sessions'),
                Prefetch('organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')),
            )
        )


class WorkshopBookView(APIView):
    """POST /api/workshops/<slug>/book/ — creates the pending booking + gateway order."""
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'workshop_booking'

    def post(self, request, slug):
        workshop = get_object_or_404(
            Workshop.objects.select_related('seller'), slug=slug, status=WorkshopStatus.PUBLISHED,
        )
        payload = BookingRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        user = request.user if request.user.is_authenticated else None
        try:
            booking = create_pending_booking(BookingRequest(workshop=workshop, user=user, **data))
        except BookingError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        customer = PaymentCustomer(
            customer_id=str(user.id) if user else 'guest',
            email=data['attendee_email'], phone=data['attendee_phone'], name=data['attendee_name'],
        )
        try:
            gateway_payload = start_gateway_payment(booking.order, customer)
        except GatewayUnavailable as exc:
            booking.order.status = 'failed'
            booking.order.save(update_fields=['status', 'updated_at'])
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            **gateway_payload,
            'order': OrderSuccessSerializer(booking.order).data,
            'booking': BookingSerializer(booking).data,
        }, status=status.HTTP_201_CREATED)


def _requesters_booking(request, order_number, email):
    """The booking behind an order number, if the requester owns it: the
    signed-in user, or a guest who also knows the attendee email."""
    qs = WorkshopBooking.objects.select_related('workshop', 'order')
    if request.user.is_authenticated:
        return qs.filter(order__order_number=order_number, user=request.user).first()
    email = (email or '').strip()
    if not email:
        return None
    return qs.filter(order__order_number=order_number, attendee_email__iexact=email).first()


class BookingByOrderView(APIView):
    """GET /api/workshops/bookings/by-order/<order_number>/

    Success page lookup. A guest has no session, so the order number plus the
    attendee email (both only known to the buyer) are required together.
    """
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'workshop_lookup'

    def get(self, request, order_number):
        booking = _requesters_booking(request, order_number, request.query_params.get('email'))
        if booking is None:
            return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(BookingSerializer(booking).data)


class ReleaseBookingHoldView(APIView):
    """POST /api/workshops/bookings/by-order/<order_number>/release/  {email?}

    Called when the buyer closes or fails the payment window, so the held
    seats return to the pool at once. Paid bookings are never released.
    """
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'workshop_lookup'

    def post(self, request, order_number):
        booking = _requesters_booking(request, order_number, request.data.get('email'))
        if booking is None:
            return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': release_abandoned_hold(booking)})


class MyBookingsView(generics.ListAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = BookingSerializer

    def get_queryset(self):
        return (WorkshopBooking.objects.filter(user=self.request.user)
                .exclude(status=BookingStatus.PAYMENT_FAILED)
                .select_related('workshop__seller__seller_profile', 'order')
                .prefetch_related(Prefetch('workshop__organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')))
                .order_by('-session_date', '-created_at'))


# ── Seller (organiser) ───────────────────────────────────────────────────────

class SellerBookingListView(generics.ListAPIView):
    """GET /api/workshops/seller/bookings/ — paid bookings for the seller's workshops."""
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = SellerBookingSerializer

    def get_queryset(self):
        own_split = WorkshopBookingSplit.objects.filter(seller=self.request.user)
        qs = (WorkshopBooking.objects
              .filter(status__in=PAID_BOOKING_STATUSES, id__in=own_split.values('booking'))
              .select_related('workshop__seller__seller_profile', 'order')
              .prefetch_related(Prefetch('splits', queryset=own_split, to_attr='my_splits'), Prefetch('workshop__organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile'))))
        params = self.request.query_params
        if params.get('workshop'):
            qs = qs.filter(workshop_id=params['workshop'])
        when = params.get('when')
        today = timezone.localdate()
        if when == 'upcoming':
            return qs.filter(session_date__gte=today).order_by('session_date', 'session_start')
        if when == 'past':
            qs = qs.filter(session_date__lt=today)
        return qs.order_by('-session_date', '-session_start')


class SellerWorkshopSummaryView(APIView):
    """GET /api/workshops/seller/summary/ — per-workshop totals + payouts received."""
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        return Response(seller_overview(request.user))


# ── Super admin ──────────────────────────────────────────────────────────────

class AdminWorkshopListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAdminUser,)
    pagination_class = None  # a curated, small set; the console shows them all

    def get_serializer_class(self):
        return AdminWorkshopSerializer if self.request.method == 'POST' else AdminWorkshopListSerializer

    def get_queryset(self):
        today = timezone.localdate()
        next_session = (WorkshopSession.objects.filter(workshop=OuterRef('pk'), is_active=True, date__gte=today)
                        .order_by('date', 'start_time').values('date')[:1])
        booked = (WorkshopBooking.objects.filter(workshop=OuterRef('pk'), status__in=PAID_BOOKING_STATUSES)
                  .values('workshop').annotate(total=Sum('builds')).values('total'))
        return (
            Workshop.objects.select_related('seller__seller_profile')
            .prefetch_related(Prefetch('organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')))
            .annotate(
                session_count=Count('sessions', filter=Q(sessions__is_active=True), distinct=True),
                capacity=Coalesce(Sum('sessions__capacity', filter=Q(sessions__is_active=True)), 0),
                next_session_date=Subquery(next_session),
                seats_booked=Coalesce(Subquery(booked), 0),
                has_overbooked_session=Exists(
                    WorkshopSession.objects.filter(workshop=OuterRef('pk'))
                    .annotate(booked=Coalesce(Sum('bookings__builds',
                                                  filter=Q(bookings__status__in=PAID_BOOKING_STATUSES)), 0))
                    .filter(booked__gt=F('capacity'))
                ),
            )
            .order_by('-created_at')
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AdminWorkshopDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Delete is a soft delete, refused while paid bookings exist."""
    permission_classes = (IsAdminUser,)
    serializer_class = AdminWorkshopSerializer

    def get_queryset(self):
        return Workshop.objects.select_related('seller__seller_profile').prefetch_related(
            'options', Prefetch('organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')),
            Prefetch('sessions', queryset=WorkshopSession.objects.with_seats_taken().order_by('date', 'start_time'),
                     to_attr='annotated_sessions'),
        )

    def destroy(self, request, *args, **kwargs):
        workshop = self.get_object()
        if workshop.bookings.filter(status__in=PAID_BOOKING_STATUSES).exists():
            return Response(
                {'error': 'This workshop has paid bookings. Cancel it instead of deleting.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        workshop.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminOrganiserListView(APIView):
    """Verified sellers that can be mapped as workshop organisers."""
    permission_classes = (IsAdminUser,)

    def get(self, request):
        sellers = (User.objects.filter(is_verified_seller=True, is_deleted=False)
                   .select_related('seller_profile').order_by('seller_profile__store_name'))
        return Response([
            {
                'id': s.id,
                'store_name': getattr(getattr(s, 'seller_profile', None), 'store_name', None) or s.get_full_name() or s.email,
                'email': s.email,
                'phone': s.phone,
            }
            for s in sellers
        ])


class AdminBookingListView(generics.ListAPIView):
    """GET /api/workshops/admin/bookings/?workshop=&session=&status=&search=&from=&to="""
    permission_classes = (IsAdminUser,)
    serializer_class = AdminBookingSerializer
    filter_backends = ()

    def get_queryset(self):
        qs = (WorkshopBooking.objects.select_related('workshop__seller__seller_profile', 'order', 'seller__seller_profile')
              .prefetch_related(Prefetch('splits', queryset=WorkshopBookingSplit.objects.select_related('seller__seller_profile')),
                                Prefetch('workshop__organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile'))))
        params = self.request.query_params
        status_filter = params.get('status')
        if status_filter == 'paid':
            qs = qs.filter(status__in=PAID_BOOKING_STATUSES)
        elif status_filter in BookingStatus.values:
            qs = qs.filter(status=status_filter)
        if params.get('workshop'):
            qs = qs.filter(workshop_id=params['workshop'])
        if params.get('session'):
            qs = qs.filter(session_id=params['session'])
        if params.get('seller'):
            qs = qs.filter(id__in=WorkshopBookingSplit.objects.filter(seller_id=params['seller']).values('booking'))
        start, end = _parse_date(params.get('from')), _parse_date(params.get('to'))
        if start:
            qs = qs.filter(session_date__gte=start)
        if end:
            qs = qs.filter(session_date__lte=end)
        search = (params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                Q(attendee_name__icontains=search) | Q(attendee_phone__icontains=search)
                | Q(attendee_email__icontains=search) | Q(order__order_number__icontains=search)
            )
        return qs.order_by('-created_at')


class AdminBookingDetailView(generics.UpdateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = AdminBookingUpdateSerializer
    queryset = WorkshopBooking.objects.select_related('order')
    http_method_names = ['patch']

    def perform_update(self, serializer):
        cancelling = serializer.validated_data.get('status') == BookingStatus.CANCELLED
        extra = {'cancelled_at': timezone.now()} if cancelling and not serializer.instance.cancelled_at else {}
        serializer.save(**extra)

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)
        booking = (WorkshopBooking.objects.select_related('workshop', 'order', 'seller__seller_profile')
                   .prefetch_related(Prefetch('splits', queryset=WorkshopBookingSplit.objects.select_related('seller__seller_profile'))).get(pk=kwargs['pk']))
        return Response(AdminBookingSerializer(booking).data)


class AdminFinancialsOverviewView(APIView):
    """GET /api/workshops/admin/financials/ — revenue + settlement per workshop."""
    permission_classes = (IsAdminUser,)

    def get(self, request):
        rows, paid_out = financials_overview(
            Workshop.all_objects.select_related('seller__seller_profile').prefetch_related(Prefetch('organisers', queryset=WorkshopOrganiser.objects.select_related('seller__seller_profile')))
        )
        due_by_workshop = _balance_due_by_workshop()
        data = []
        for w in rows.order_by('-created_at'):
            if w.is_deleted and not w.paid_bookings:
                continue
            settled = paid_out.get(w.id, 0)
            data.append({
                'id': w.id, 'title': w.title, 'status': w.status,
                'organiser_name': AdminWorkshopListSerializer().get_organiser_name(w),
                'commission_percent': w.commission_percent,
                'paid_bookings': w.paid_bookings, 'seats_sold': w.seats_sold,
                'gross': w.gross, 'commission': w.commission, 'seller_payout': w.seller_payout,
                'earned_payout': w.earned_payout, 'paid_out': settled,
                'balance_due': due_by_workshop.get(w.id, 0),
                'pending_settlement': w.seller_payout - settled,
            })
        return Response(data)


def _balance_due_by_workshop():
    """Sum over each workshop's sellers of max(earned - settled, 0). Two queries."""
    today = timezone.localdate()
    earned = (WorkshopBookingSplit.objects
              .filter(booking__status__in=PAID_BOOKING_STATUSES, booking__session_date__lt=today)
              .values_list('booking__workshop', 'seller').annotate(total=Sum('seller_payout')))
    settled = {(w, sel): total for w, sel, total in
               WorkshopPayout.objects.values_list('workshop', 'seller').annotate(total=Sum('amount'))}
    due = {}
    for workshop_id, seller_id, total in earned:
        due[workshop_id] = due.get(workshop_id, 0) + max(total - settled.get((workshop_id, seller_id), 0), 0)
    return due


class AdminWorkshopFinancialsView(APIView):
    permission_classes = (IsAdminUser,)

    def get(self, request, pk):
        workshop = get_object_or_404(Workshop.all_objects, pk=pk)
        return Response(workshop_financials(workshop))


class AdminPayoutCreateView(generics.CreateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = PayoutSerializer

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)


class AdminPayoutDeleteView(generics.DestroyAPIView):
    permission_classes = (IsAdminUser,)
    queryset = WorkshopPayout.objects.all()
