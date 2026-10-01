from types import SimpleNamespace

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from cart.models import Cart, CartItem
from core.models import ProductVariant
from .engine import evaluate_coupon, CouponError
from .models import Coupon, FundedBy
from .serializers import CouponAdminSerializer


def resolve_items(request):
    """
    Same cart contract as CheckoutView: `cart_id` (+ optional `item_ids`) for a
    server cart, or an inline `items` list of {variant_id, quantity} for guests.
    Returns (items, error_response). Items carry `.product`, `.variant`,
    `.quantity` and — for server rows — `.id`.
    """
    cart_id = request.data.get('cart_id')
    item_ids = request.data.get('item_ids')
    raw_items = request.data.get('items')

    if cart_id:
        try:
            cart = Cart.objects.get(id=cart_id)
        except (Cart.DoesNotExist, DjangoValidationError, ValueError):
            return None, Response({"error": "Cart not found"}, status=404)
        qs = cart.items.select_related('product', 'product__seller', 'variant') \
                       .prefetch_related('product__categories')
        if item_ids:
            pk_field = CartItem._meta.pk
            try:
                qs = qs.filter(id__in=[pk_field.to_python(i) for i in item_ids])
            except (DjangoValidationError, ValueError, TypeError):
                return None, Response({"error": "item_ids must be valid item IDs"}, status=400)
        items = [it for it in qs if it.quantity >= 1]
        return items, None

    if raw_items:
        if not isinstance(raw_items, list):
            return None, Response({"error": "items must be a list"}, status=400)
        items = []
        for entry in raw_items:
            vid = entry.get('variant_id')
            try:
                qty = int(entry.get('quantity', 1))
            except (TypeError, ValueError):
                qty = 0
            if not vid or qty < 1:
                continue
            try:
                variant = ProductVariant.objects.select_related('product', 'product__seller') \
                                                .prefetch_related('product__categories').get(id=vid)
            except (ProductVariant.DoesNotExist, DjangoValidationError, ValueError):
                return None, Response({"error": f"Product variant not found: {vid}"}, status=400)
            items.append(SimpleNamespace(id=None, product=variant.product, variant=variant, quantity=qty))
        return items, None

    return None, Response({"error": "cart_id or items is required"}, status=400)


class ApplyCouponView(APIView):
    """
    POST /api/coupons/apply/
    Body: { code, cart_id?, item_ids?, items?, guest_email? }
    Stateless preview — nothing is stored. The UI keeps the code and re-sends
    it as `coupon_code` at checkout, where it is re-validated server-side.
    """
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        items, err = resolve_items(request)
        if err:
            return err
        user = request.user if request.user.is_authenticated else None
        try:
            result = evaluate_coupon(
                request.data.get('code'), items,
                user=user, guest_email=request.data.get('guest_email'),
            )
        except CouponError as e:
            return Response({"error": e.message}, status=400)

        c = result.coupon
        return Response({
            "code": c.code,
            "name": c.name,
            "description": c.description,
            "discount_label": c.describe_discount(),
            "discount_amount": str(result.discount_amount),
            "eligible_subtotal": str(result.eligible_subtotal),
            "cart_subtotal": str(result.cart_subtotal),
            "seller_funded": c.funded_by == FundedBy.SELLER,
            # keyed by cart item id (server carts) or variant id (guest lists)
            "item_discounts": {
                str(getattr(it, 'id', None) or it.variant.id): str(d)
                for it, d in zip(items, result.item_discounts) if d > 0
            },
        })


# ── Super-admin CRUD ─────────────────────────────────────────────────────────

class CouponAdminListCreateView(generics.ListCreateAPIView):
    """GET /api/coupons/admin/  |  POST /api/coupons/admin/"""
    permission_classes = (permissions.IsAdminUser,)
    serializer_class = CouponAdminSerializer
    pagination_class = None

    def get_queryset(self):
        return (Coupon.objects.select_related('seller', 'seller__seller_profile')
                .prefetch_related('categories', 'redemptions'))

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class CouponAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/coupons/admin/<uuid:pk>/  (delete is a soft-delete)"""
    permission_classes = (permissions.IsAdminUser,)
    serializer_class = CouponAdminSerializer
    queryset = Coupon.objects.select_related('seller', 'seller__seller_profile').prefetch_related('categories')
