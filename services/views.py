import logging

from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.permissions import IsAdminUser
from orders.serializers import OrderSuccessSerializer
from payments.checkout import GatewayUnavailable, PaymentCustomer, start_gateway_payment

from .permissions import IsSellerAccount, assignable_sellers
from .validators import validate_reference_upload
from .models import QUOTE_SECTIONS, AdditionalCharge, QuoteRequest, QuoteStatus, ServiceGroup, ServiceOffering
from .serializers import (
    AdditionalChargeCreateSerializer, AdminGroupSerializer, CancelSerializer, AdminOfferingSerializer, AdminQuoteDetailsSerializer,
    AssignSellerSerializer, DeclineSerializer, GroupSerializer, OfferingDetailSerializer, PayAddressSerializer,
    QuoteDetailsSerializer, QuoteListSerializer, QuoteRequestCreateSerializer, QuoteRequestSerializer,
    QuoteStaffDetailSerializer, SellerBriefSerializer, SellerQuoteDetailSerializer, SellerQuoteListSerializer,
)
from .workflow import (
    QuoteError, QuoteExpired, assign_seller, cancel_additional_charge, cancel_order, customer_cancel,
    decline_request, dismiss_cancel_request, expire_overdue_pay_requests, mark_delivered, notify_new_request,
    record_extra_payout, renew_pay_request,
    prepare_charge_order, prepare_order_for_payment, request_additional_charge, send_pay_request,
    update_quote_details,
)


logger = logging.getLogger(__name__)


def active_groups_queryset():
    """Active groups with their active offerings attached as `active_offerings`."""
    return ServiceGroup.objects.filter(is_active=True).prefetch_related(
        Prefetch('offerings', queryset=ServiceOffering.objects.filter(is_active=True).select_related('group'),
                 to_attr='active_offerings'),
    )


def _error(message, code=status.HTTP_400_BAD_REQUEST):
    return Response({'error': message}, status=code)


# ── Public catalogue ─────────────────────────────────────────────────────────

class ServiceGroupListView(generics.ListAPIView):
    """GET /api/services/groups/ — `?home=1` returns only the groups shown on the home page."""
    permission_classes = (permissions.AllowAny,)
    serializer_class = GroupSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = active_groups_queryset()
        if self.request.query_params.get('home') == '1':
            queryset = queryset.filter(show_on_home=True)
        return queryset.filter(offerings__is_active=True).distinct()


class ServiceGroupDetailView(generics.RetrieveAPIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = GroupSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return active_groups_queryset()


class ServiceOfferingDetailView(generics.RetrieveAPIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = OfferingDetailSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return ServiceOffering.objects.filter(is_active=True, group__is_active=True).select_related('group').prefetch_related('form_fields')


class ReferenceUploadView(APIView):
    """POST /api/services/uploads/ — one reference photo for a quote request (guests allowed)."""
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'service_upload'

    def post(self, request):
        from core.storage import upload_to_firebase

        try:
            image = validate_reference_upload(request.FILES.get('image'))
        except serializers.ValidationError as exc:
            return _error(exc.detail[0])
        owner = request.user.id if request.user.is_authenticated else 'guest'
        try:
            url = upload_to_firebase(image, owner, 'service-reference')
        except Exception:
            logger.exception('Reference photo upload failed')
            return _error('Photo upload failed. Please try again.', status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'url': url}, status=status.HTTP_201_CREATED)


class QuoteRequestCreateView(APIView):
    """POST /api/services/offerings/<slug>/request/ — a customer asks for a quote."""
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'service_quote'

    def post(self, request, slug):
        offering = get_object_or_404(
            ServiceOffering.objects.select_related('group').prefetch_related('form_fields'),
            slug=slug, is_active=True, group__is_active=True,
        )
        user = request.user if request.user.is_authenticated else None
        serializer = QuoteRequestCreateSerializer(data=request.data, context={'offering': offering, 'user': user})
        serializer.is_valid(raise_exception=True)
        quote = serializer.save()
        notify_new_request(quote)
        return Response(
            {'reference': quote.reference, 'message': 'Request received. We will contact you with a quote soon.'},
            status=status.HTTP_201_CREATED,
        )


# ── Customer ─────────────────────────────────────────────────────────────────

def customer_quotes(user):
    """The user's requests, including ones made as a guest with the same email."""
    return QuoteRequest.objects.filter(
        Q(user=user) | Q(user__isnull=True, contact_email__iexact=user.email),
    ).select_related('offering__group', 'order').prefetch_related('additional_charges')


class MyQuoteListView(generics.ListAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = QuoteRequestSerializer

    def get_queryset(self):
        return customer_quotes(self.request.user)


class MyQuoteDetailView(generics.RetrieveAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = QuoteRequestSerializer
    lookup_field = 'reference'

    def get_queryset(self):
        return customer_quotes(self.request.user)


class QuotePayView(APIView):
    """POST /api/services/my-requests/<reference>/pay/ — delivery address in, gateway payload out."""
    permission_classes = (permissions.IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'service_pay'

    def post(self, request, reference):
        quote = get_object_or_404(customer_quotes(request.user).select_related('assigned_seller__seller_profile'), reference=reference)
        serializer = PayAddressSerializer(data=request.data, context={'quote': quote})
        serializer.is_valid(raise_exception=True)
        try:
            quote, order = prepare_order_for_payment(quote.id, request.user, serializer.shipping_address())
        except QuoteExpired as exc:
            expire_overdue_pay_requests()  # record it now instead of waiting for the hourly task
            return _error(str(exc))
        except QuoteError as exc:
            return _error(str(exc))
        customer = PaymentCustomer(
            customer_id=str(request.user.id), email=quote.contact_email, phone=quote.contact_phone,
            name=serializer.validated_data['full_name'],
        )
        try:
            gateway_payload = start_gateway_payment(order, customer)
        except GatewayUnavailable as exc:
            return _error(str(exc))
        return Response({**gateway_payload, 'order': OrderSuccessSerializer(order).data}, status=status.HTTP_201_CREATED)


class MyQuoteCancelView(APIView):
    """POST /api/services/my-requests/<reference>/cancel/ — cancel before paying, or ask to after."""
    permission_classes = (permissions.IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'service_cancel'

    def post(self, request, reference):
        quote = get_object_or_404(customer_quotes(request.user), reference=reference)
        payload = CancelSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            outcome = customer_cancel(quote, request.user, payload.validated_data['reason'])
        except QuoteError as exc:
            return _error(str(exc))
        fresh = customer_quotes(request.user).get(pk=quote.pk)
        return Response({'outcome': outcome, 'request': QuoteRequestSerializer(fresh).data})


class QuoteChargePayView(APIView):
    """POST /api/services/my-requests/<reference>/charges/<id>/pay/ — pay an additional request."""
    permission_classes = (permissions.IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'service_pay'

    def post(self, request, reference, charge_id):
        quote = get_object_or_404(customer_quotes(request.user), reference=reference)
        charge = get_object_or_404(AdditionalCharge, pk=charge_id, quote_request=quote)
        try:
            charge, order = prepare_charge_order(charge.id, request.user)
        except QuoteError as exc:
            return _error(str(exc))
        customer = PaymentCustomer(
            customer_id=str(request.user.id), email=quote.contact_email, phone=quote.contact_phone, name=quote.contact_name,
        )
        try:
            gateway_payload = start_gateway_payment(order, customer)
        except GatewayUnavailable as exc:
            return _error(str(exc))
        return Response({**gateway_payload, 'order': OrderSuccessSerializer(order).data}, status=status.HTTP_201_CREATED)


# ── Seller ───────────────────────────────────────────────────────────────────

def seller_quotes(user):
    return (QuoteRequest.objects.filter(assigned_seller=user).select_related('offering__group', 'order')
            .prefetch_related('additional_charges'))


class SellerQuoteListView(generics.ListAPIView):
    permission_classes = (IsSellerAccount,)
    serializer_class = SellerQuoteListSerializer

    def get_queryset(self):
        # Sellers never need declined / cancelled requests; ?section=delivered shows finished ones.
        section = self.request.query_params.get('section')
        return filter_section(
            seller_quotes(self.request.user).select_related('assigned_seller__seller_profile'),
            section if section in ('active', 'delivered') else None, default='active',
        )


class SellerQuoteDetailView(generics.RetrieveUpdateAPIView):
    """GET the request; PATCH the quote figures while it is still open."""
    permission_classes = (IsSellerAccount,)
    http_method_names = ('get', 'patch', 'head', 'options')

    def get_queryset(self):
        return seller_quotes(self.request.user).select_related('assigned_seller__seller_profile')

    def get_serializer_class(self):
        return SellerQuoteDetailSerializer

    def patch(self, request, *args, **kwargs):
        quote = self.get_object()
        details = QuoteDetailsSerializer(data=request.data, partial=True)
        details.is_valid(raise_exception=True)
        try:
            update_quote_details(quote, request.user, details.validated_data, actor_is_admin=False)
        except QuoteError as exc:
            return _error(str(exc))
        return Response(SellerQuoteDetailSerializer(self.get_queryset().get(pk=quote.pk)).data)


class _ChargeViewMixin:
    """Request or cancel an additional payment; subclasses say whose requests they may touch."""
    detail_serializer = None

    def quotes(self, request):
        raise NotImplementedError

    def refreshed(self, request, pk):
        return Response(self.detail_serializer(self.quotes(request).get(pk=pk)).data)


class SellerChargeCreateView(_ChargeViewMixin, APIView):
    permission_classes = (IsSellerAccount,)
    detail_serializer = SellerQuoteDetailSerializer

    def quotes(self, request):
        return seller_quotes(request.user).select_related('assigned_seller__seller_profile')

    def post(self, request, pk):
        quote = get_object_or_404(self.quotes(request), pk=pk)
        payload = AdditionalChargeCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            request_additional_charge(quote, request.user, **payload.validated_data)
        except QuoteError as exc:
            return _error(str(exc))
        return self.refreshed(request, pk)


class SellerChargeCancelView(_ChargeViewMixin, APIView):
    permission_classes = (IsSellerAccount,)
    detail_serializer = SellerQuoteDetailSerializer

    def quotes(self, request):
        return seller_quotes(request.user).select_related('assigned_seller__seller_profile')

    def post(self, request, pk, charge_id):
        quote = get_object_or_404(self.quotes(request), pk=pk)
        charge = get_object_or_404(AdditionalCharge, pk=charge_id, quote_request=quote)
        try:
            cancel_additional_charge(charge, request.user)
        except QuoteError as exc:
            return _error(str(exc))
        return self.refreshed(request, pk)


# ── Super admin ──────────────────────────────────────────────────────────────

class AdminGroupListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = AdminGroupSerializer
    pagination_class = None

    def get_queryset(self):
        return ServiceGroup.objects.annotate(offering_count=Count('offerings'))


class AdminGroupDetailView(generics.RetrieveUpdateAPIView):
    """Groups are switched off with `is_active`, never deleted (requests reference them)."""
    permission_classes = (IsAdminUser,)
    serializer_class = AdminGroupSerializer

    def get_queryset(self):
        return ServiceGroup.objects.annotate(offering_count=Count('offerings'))


class AdminOfferingListCreateView(generics.ListCreateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = AdminOfferingSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = ServiceOffering.objects.select_related('group').prefetch_related('form_fields')
        group = self.request.query_params.get('group')
        return queryset.filter(group_id=group) if group and group.isdigit() else queryset


class AdminOfferingDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = AdminOfferingSerializer

    def get_queryset(self):
        return ServiceOffering.objects.select_related('group').prefetch_related('form_fields')


class AdminSellerListView(generics.ListAPIView):
    """Sellers the admin can assign a request to."""
    permission_classes = (IsAdminUser,)
    serializer_class = SellerBriefSerializer
    pagination_class = None

    def get_queryset(self):
        return assignable_sellers().order_by('seller_profile__store_name')


class AdminSummaryView(APIView):
    """GET /api/services/admin/summary/ — what needs the admin's attention, for the dashboard highlight."""
    permission_classes = (IsAdminUser,)

    def get(self, request):
        counts = QuoteRequest.objects.aggregate(
            new_requests=Count('id', filter=Q(status=QuoteStatus.SUBMITTED)),
            in_review=Count('id', filter=Q(status=QuoteStatus.IN_REVIEW)),
            awaiting_payment=Count('id', filter=Q(status=QuoteStatus.AWAITING_PAYMENT)),
            cancel_requests=Count('id', filter=Q(status=QuoteStatus.PAID, cancel_requested_at__isnull=False)),
            **{f'section_{name}': Count('id', filter=Q(status__in=statuses)) for name, statuses in QUOTE_SECTIONS.items()},
        )
        return Response(counts)


def filter_section(queryset, section, default=None):
    """Limit a request list to one section (active / delivered / closed); unknown values fall back."""
    statuses = QUOTE_SECTIONS.get(section) or QUOTE_SECTIONS.get(default)
    return queryset.filter(status__in=statuses) if statuses else queryset


QUOTE_ORDERINGS = {'created': 'created_at', '-created': '-created_at', 'budget': 'budget_max', '-budget': '-budget_max'}


class AdminQuoteListCreateView(generics.ListCreateAPIView):
    """List the queue; POST records a request taken over the phone (needs `offering` slug)."""
    permission_classes = (IsAdminUser,)

    def get_serializer_class(self):
        return QuoteRequestCreateSerializer if self.request.method == 'POST' else QuoteListSerializer

    def get_queryset(self):
        params = self.request.query_params
        queryset = filter_section(
            QuoteRequest.objects.select_related('offering__group', 'assigned_seller__seller_profile'),
            params.get('section'), default='active',
        )
        if params.get('status'):
            queryset = queryset.filter(status=params['status'])
        if params.get('group'):
            queryset = queryset.filter(offering__group__slug=params['group'])
        if params.get('seller'):
            queryset = queryset.filter(assigned_seller_id=params['seller'])
        if params.get('q'):
            term = params['q'].strip()
            queryset = queryset.filter(
                Q(reference__icontains=term) | Q(contact_name__icontains=term) | Q(contact_phone__icontains=term),
            )
        return queryset.order_by(QUOTE_ORDERINGS.get(params.get('ordering'), '-created_at'))

    def create(self, request, *args, **kwargs):
        offering = get_object_or_404(
            ServiceOffering.objects.prefetch_related('form_fields'), slug=request.data.get('offering'),
        )
        serializer = QuoteRequestCreateSerializer(data=request.data, context={'offering': offering, 'user': None})
        serializer.is_valid(raise_exception=True)
        quote = serializer.save()
        notify_new_request(quote)
        return Response({'reference': quote.reference, 'id': str(quote.id)}, status=status.HTTP_201_CREATED)


class AdminQuoteDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = (IsAdminUser,)
    serializer_class = QuoteStaffDetailSerializer
    http_method_names = ('get', 'patch', 'head', 'options')

    def get_queryset(self):
        return QuoteRequest.objects.select_related(
            'offering__group', 'assigned_seller__seller_profile', 'order',
        ).prefetch_related('events__actor', 'additional_charges')

    def patch(self, request, *args, **kwargs):
        quote = self.get_object()
        details = AdminQuoteDetailsSerializer(data=request.data, partial=True)
        details.is_valid(raise_exception=True)
        try:
            update_quote_details(quote, request.user, details.validated_data, actor_is_admin=True)
        except QuoteError as exc:
            return _error(str(exc))
        return Response(QuoteStaffDetailSerializer(self.get_queryset().get(pk=quote.pk)).data)


class _AdminQuoteActionView(APIView):
    """Shared shape for the admin's one-click actions on a request."""
    permission_classes = (IsAdminUser,)

    def act(self, request, quote):
        raise NotImplementedError

    def post(self, request, pk):
        quote = get_object_or_404(
            QuoteRequest.objects.select_related('offering', 'assigned_seller__seller_profile'), pk=pk,
        )
        try:
            self.act(request, quote)
        except QuoteError as exc:
            return _error(str(exc))
        fresh = AdminQuoteDetailView().get_queryset().get(pk=pk)
        return Response(QuoteStaffDetailSerializer(fresh).data)


class AdminAssignSellerView(_AdminQuoteActionView):
    def act(self, request, quote):
        payload = AssignSellerSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assign_seller(quote, request.user, payload.seller)


class AdminSendPayRequestView(_AdminQuoteActionView):
    def act(self, request, quote):
        send_pay_request(quote, request.user)


class AdminChargeCreateView(_AdminQuoteActionView):
    def act(self, request, quote):
        payload = AdditionalChargeCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        request_additional_charge(quote, request.user, **payload.validated_data)


class AdminChargeCancelView(APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request, pk, charge_id):
        charge = get_object_or_404(AdditionalCharge, pk=charge_id, quote_request_id=pk)
        try:
            cancel_additional_charge(charge, request.user)
        except QuoteError as exc:
            return _error(str(exc))
        fresh = AdminQuoteDetailView().get_queryset().get(pk=pk)
        return Response(QuoteStaffDetailSerializer(fresh).data)


class AdminMarkDeliveredView(_AdminQuoteActionView):
    def act(self, request, quote):
        mark_delivered(quote, request.user)


class AdminRenewView(_AdminQuoteActionView):
    def act(self, request, quote):
        renew_pay_request(quote, request.user)


class AdminCancelView(_AdminQuoteActionView):
    def act(self, request, quote):
        payload = DeclineSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        cancel_order(quote, request.user, payload.validated_data['reason'])


class AdminDismissCancelView(_AdminQuoteActionView):
    def act(self, request, quote):
        dismiss_cancel_request(quote, request.user)


class AdminExtraPayoutView(APIView):
    """POST .../charges/<id>/payout-done/ — the admin paid the seller an extra amount by hand."""
    permission_classes = (IsAdminUser,)

    def post(self, request, pk, charge_id):
        charge = get_object_or_404(AdditionalCharge, pk=charge_id, quote_request_id=pk)
        try:
            record_extra_payout(charge, request.user)
        except QuoteError as exc:
            return _error(str(exc))
        fresh = AdminQuoteDetailView().get_queryset().get(pk=pk)
        return Response(QuoteStaffDetailSerializer(fresh).data)


class AdminDeclineView(_AdminQuoteActionView):
    def act(self, request, quote):
        payload = DeclineSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        decline_request(quote, request.user, payload.validated_data['reason'])
