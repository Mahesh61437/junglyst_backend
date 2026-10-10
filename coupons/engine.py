"""
Coupon evaluation. Pure logic over cart-like items so the same code serves
the cart preview endpoint and the checkout view (which must never trust a
client-supplied discount).

An "item" is anything with `.product`, `.variant` and `.quantity` —
`cart.models.CartItem` rows or the SimpleNamespace objects checkout builds
for guest carts. `product.categories` should be prefetched by the caller.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import List, Optional

from django.utils import timezone

from .models import Coupon, DiscountType

TWO_PLACES = Decimal('0.01')


class CouponError(Exception):
    """Raised when a code can't be applied. `.message` is safe to show buyers."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass
class CouponResult:
    coupon: Coupon
    cart_subtotal: Decimal
    eligible_subtotal: Decimal
    discount_amount: Decimal
    # Per-line discount, aligned by index with the `items` list passed in.
    # Lines outside the coupon's scope get Decimal('0').
    item_discounts: List[Decimal] = field(default_factory=list)


def _q(value) -> Decimal:
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _line_total(item) -> Decimal:
    return Decimal(str(item.variant.price)) * item.quantity


def item_matches_scope(coupon: Coupon, item, category_ids=None) -> bool:
    """Does this cart line fall inside the coupon's seller/category scope?"""
    if coupon.seller_id and item.product.seller_id != coupon.seller_id:
        return False
    if category_ids is None:
        category_ids = set(coupon.categories.values_list('id', flat=True))
    if category_ids:
        product_cats = {c.id for c in item.product.categories.all()}
        if not (product_cats & category_ids):
            return False
    return True


def _allocate(total: Decimal, weights: List[Decimal]) -> List[Decimal]:
    """Split `total` across lines proportionally to `weights`, rounded to
    paise, using largest-remainder so the parts sum exactly to `total`."""
    weight_sum = sum(weights, Decimal('0'))
    if not weight_sum or total <= 0:
        return [Decimal('0') for _ in weights]
    raw = [total * w / weight_sum for w in weights]
    floored = [r.quantize(TWO_PLACES, rounding=ROUND_DOWN) for r in raw]
    remainder = total - sum(floored, Decimal('0'))
    # hand out leftover paise to the lines with the largest fractional part
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floored[i], reverse=True)
    i = 0
    while remainder >= TWO_PLACES and order:
        floored[order[i % len(order)]] += TWO_PLACES
        remainder -= TWO_PLACES
        i += 1
    return floored


def evaluate_coupon(code: str, items, user=None, guest_email: Optional[str] = None) -> CouponResult:
    """
    Validate `code` against the cart and return the computed discount.
    Raises CouponError with a buyer-facing message when it can't be applied.
    """
    code = (code or '').strip().upper()
    if not code:
        raise CouponError("Please enter a coupon code.")

    coupon = Coupon.objects.prefetch_related('categories').filter(code=code).first()
    if coupon is None or not coupon.is_active:
        raise CouponError("This coupon code is not valid.")

    now = timezone.now()
    if coupon.valid_from > now:
        raise CouponError("This coupon is not active yet.")
    if coupon.valid_until and now >= coupon.valid_until:
        raise CouponError("This coupon has expired.")

    items = list(items)
    if not items:
        raise CouponError("Your cart is empty.")

    # ── Usage limits ────────────────────────────────────────────────────────
    if coupon.usage_limit_total is not None or coupon.usage_limit_per_user is not None:
        counted = coupon.redemptions_counted()
        if coupon.usage_limit_total is not None and counted.count() >= coupon.usage_limit_total:
            raise CouponError("This coupon has reached its usage limit.")
        if coupon.usage_limit_per_user is not None:
            if user is not None and getattr(user, 'is_authenticated', False):
                mine = counted.filter(user=user).count()
            elif guest_email:
                mine = counted.filter(guest_email__iexact=guest_email).count()
            else:
                mine = 0
            if mine >= coupon.usage_limit_per_user:
                raise CouponError("You have already used this coupon the maximum number of times.")

    # ── Cart value + scope ──────────────────────────────────────────────────
    cart_subtotal = sum((_line_total(it) for it in items), Decimal('0'))
    if cart_subtotal < coupon.min_cart_value:
        short = _q(coupon.min_cart_value - cart_subtotal)
        raise CouponError(
            f"Add ₹{short} more to your cart to use this coupon (minimum ₹{_q(coupon.min_cart_value)})."
        )

    category_ids = set(coupon.categories.values_list('id', flat=True))
    weights = [
        _line_total(it) if item_matches_scope(coupon, it, category_ids) else Decimal('0')
        for it in items
    ]
    eligible_subtotal = sum(weights, Decimal('0'))
    if eligible_subtotal <= 0:
        raise CouponError(_scope_message(coupon))

    # ── Discount ────────────────────────────────────────────────────────────
    if coupon.discount_type == DiscountType.PERCENT:
        discount = eligible_subtotal * Decimal(str(coupon.discount_value)) / Decimal('100')
    else:
        discount = Decimal(str(coupon.discount_value))
    if coupon.max_discount_amount is not None:
        discount = min(discount, Decimal(str(coupon.max_discount_amount)))
    discount = min(discount, eligible_subtotal)   # never discount below zero
    discount = _q(discount)

    return CouponResult(
        coupon=coupon,
        cart_subtotal=_q(cart_subtotal),
        eligible_subtotal=_q(eligible_subtotal),
        discount_amount=discount,
        item_discounts=_allocate(discount, weights),
    )


def _scope_message(coupon: Coupon) -> str:
    parts = []
    if coupon.seller_id:
        try:
            parts.append(f"from {coupon.seller.seller_profile.store_name}")
        except Exception:
            parts.append("from the selected seller")
    cats = list(coupon.categories.values_list('name', flat=True))
    if cats:
        parts.append("in " + ", ".join(cats))
    where = " ".join(parts) or "that this coupon covers"
    return f"This coupon applies to products {where}. Add one to your cart to use it."
