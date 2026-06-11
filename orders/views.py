import logging
from rest_framework import generics, status, permissions

logger = logging.getLogger(__name__)
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from django.db import transaction
from django.db import models
from django.db.models import Prefetch
from django.utils import timezone
from datetime import timedelta
from types import SimpleNamespace
import uuid
from decimal import Decimal
from cart.models import Cart, CartItem
from core.models import ProductVariant
from shipping.models import ShippingAddress
from shipping.serializers import ShippingAddressSerializer
from shipping.pincode_zones import classify_pincode, transit_days_for_zone
from shipping.services import check_pincode_deliverable
from notifications.models import AppNotification
from payments.models import Payment
from payments.cashfree_utils import create_cashfree_order, verify_cashfree_payment
from payments.models import PaymentGatewaySettings, PaymentGateway
from payments.razorpay_utils import create_razorpay_order, verify_razorpay_signature
from .models import Order, OrderItem, SubOrder
from .serializers import (
    OrderSerializer, OrderListSerializer, OrderDetailSerializer,
    SellerOrderSerializer, SellerSubOrderSerializer,
    OrderSuccessSerializer, OrderTrackingSerializer)
from .email_utils import send_order_confirmation_emails
from .tasks import send_order_confirmation_emails_task, create_order_notifications_task, clear_buyer_cart_task

# ── Helpers ─────────────────────────────────────────────────────────────────

_LETTER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def _generate_order_number():
    """JNG-YYYY-NNNNN  (year + 5-digit random hex)"""
    import datetime
    year = datetime.datetime.now().year
    suffix = uuid.uuid4().hex[:5].upper()
    return f"JNG-{year}-{suffix}"

def _sub_order_number(master: str, index: int) -> str:
    """JNG-2026-ABCDE-A, -B, -C"""
    return f"{master}-{_LETTER[index]}"

def _build_shipping_config_map(seller_ids):
    """Fetch SellerShippingConfig rows for the given seller IDs in one query.
    Returns dict keyed (seller_id_str, item_category) → config instance."""
    from sellers.models import SellerShippingConfig
    configs = SellerShippingConfig.objects.filter(seller_id__in=seller_ids)
    return {(str(c.seller_id), c.item_category): c for c in configs}


def _shipping_fee_for_seller(subtotal: float, config) -> int:
    """Return shipping fee using DB config. Returns 0 (free) when no config set."""
    if config is None:
        return 0
    return config.fee_for(subtotal)


def _finalize_order(order, payment=None, payment_data=None):
    if payment is not None:
        if payment_data:
            payment.razorpay_payment_id = payment_data.get('razorpay_payment_id')
            payment.razorpay_signature = payment_data.get('razorpay_signature')
        payment.status = 'captured'
        payment.save()

    order.is_paid = True
    order.status = 'confirmed'
    order.payment_status = 'captured'
    order.save()

    if order.user:
        AppNotification.objects.create(
            user=order.user,
            title="Order Placed!",
            message=f"Your order {order.order_number} has been successfully placed and is being prepared."
        )

        seller_notifs = []
        for sub in order.sub_orders.select_related('seller').all():
            item_count = sub.items.count()
            seller_notifs.append(AppNotification(
                user=sub.seller,
                title="New Order Received!",
                message=(
                    f"Sub-order {sub.sub_order_number} has been placed with "
                    f"{item_count} item{'s' if item_count != 1 else ''}. "
                    f"Please confirm within 48 hours."
                ),
            ))
        if seller_notifs:
            AppNotification.objects.bulk_create(seller_notifs)

        Cart.objects.filter(user=order.user).update(updated_at=timezone.now())
        from cart.models import CartItem
        CartItem.objects.filter(cart__user=order.user).delete()

    for item in order.items.all():
        if item.variant:
            item.variant.stock -= item.quantity
            item.variant.save()


class CheckoutView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    @transaction.atomic
    def post(self, request):
        cart_id = request.data.get('cart_id')
        address_id = request.data.get('address_id')
        guest_info = request.data.get('guest_info')
        raw_items = request.data.get('items')   # guest itemized checkout (no cart)
        pincode = request.data.get('pincode', '')

        # ── Resolve cart items ──────────────────────────────────────────────
        if cart_id:
            try:
                cart_obj = Cart.objects.get(id=cart_id)
            except Cart.DoesNotExist:
                return Response({"error": "Cart not found"}, status=404)
            if not cart_obj.items.exists():
                return Response({"error": "Cart is empty"}, status=400)
            cart_items = list(cart_obj.items.select_related(
                'product', 'product__seller', 'variant'
            ).prefetch_related('product__categories').all())
        elif raw_items:
            if not isinstance(raw_items, list) or len(raw_items) == 0:
                return Response({"error": "Items list is empty"}, status=400)
            cart_items = []
            for entry in raw_items:
                vid = entry.get('variant_id')
                qty = int(entry.get('quantity', 1))
                if not vid or qty < 1:
                    return Response({"error": "Each item requires a valid variant_id and quantity >= 1"}, status=400)
                try:
                    variant = ProductVariant.objects.select_related(
                        'product', 'product__seller'
                    ).prefetch_related('product__categories').get(id=vid)
                except ProductVariant.DoesNotExist:
                    return Response({"error": f"Product variant not found: {vid}"}, status=400)
                cart_items.append(SimpleNamespace(
                    product=variant.product,
                    variant=variant,
                    quantity=qty,
                ))
        else:
            return Response({"error": "cart_id or items is required"}, status=400)

        # Address
        if request.user.is_authenticated and address_id:
            # Saved address flow
            try:
                address_obj = ShippingAddress.objects.get(id=address_id, user=request.user)
                # Convert to plain dict with only address fields — strip user FK (UUID)
                # so the JSONField can serialize cleanly
                _raw = ShippingAddressSerializer(address_obj).data
                shipping_address = {
                    k: str(v) if hasattr(v, 'hex') else v   # UUID → str safety net
                    for k, v in _raw.items()
                    if k not in ('id', 'user', 'created_at', 'updated_at', 'is_default')
                }
                email = request.user.email
                phone = request.user.phone
            except (ShippingAddress.DoesNotExist, ValueError):
                return Response({"error": "Shipping address not found or invalid"}, status=400)
        else:
            # Guest checkout OR logged-in user with a one-time address
            if not guest_info:
                return Response({"error": "Shipping address or guest info is required"}, status=400)
            shipping_address = guest_info.get('address')
            email = guest_info.get('email') or (request.user.email if request.user.is_authenticated else None)
            phone = guest_info.get('phone') or (request.user.phone if request.user.is_authenticated else None)

        # Pincode deliverability check — verified against the active logistics provider API
        effective_pincode = str(pincode or (shipping_address or {}).get('pincode', '') or '')
        if effective_pincode:
            is_deliverable, delivery_msg = check_pincode_deliverable(effective_pincode)
            if not is_deliverable:
                return Response({"error": delivery_msg or "Sorry, we don't deliver to your pincode yet."}, status=400)

        # Stock check + group items by seller
        # When using a saved backend cart (cart_id), stale items (e.g. removed in UI
        # but not synced) may linger. Auto-remove them here so the user can still
        # check out with the items they actually want.
        # For inline item lists (guest / fallback), fail fast as normal.
        if cart_id:
            stale = [item for item in cart_items if item.quantity > item.variant.stock]
            for item in stale:
                CartItem.objects.filter(id=item.id).delete()
            cart_items = [item for item in cart_items if item not in stale]
            if not cart_items:
                return Response({"error": "All items in your cart are out of stock. Please add available products."}, status=400)

        seller_buckets = {}  # seller_id → {seller, items, subtotal, has_heavy, has_light}
        for item in cart_items:
            if item.quantity > item.variant.stock:
                return Response({
                    "error": f"Inventory mismatch: {item.product.name} has only {item.variant.stock} units available."
                }, status=400)

            sid = str(item.product.seller_id)
            if sid not in seller_buckets:
                seller_buckets[sid] = {
                    'seller': item.product.seller,
                    'items': [],
                    'subtotal': 0,
                    'has_heavy': False,
                    'has_light': False,
                }
            price = float(item.variant.price) * item.quantity
            seller_buckets[sid]['items'].append(item)
            seller_buckets[sid]['subtotal'] += price
            # Track both light and heavy items to detect hybrid carts
            if item.variant.item_category == 'heavy':
                seller_buckets[sid]['has_heavy'] = True
            elif item.variant.item_category == 'light':
                seller_buckets[sid]['has_light'] = True

        # SHIP-003: max 3 sellers
        if len(seller_buckets) > 3:
            return Response({"error": "Cart supports up to 3 sellers. Please remove items."}, status=400)

        # Shipping availability: snapshot each seller's next dispatch date.
        # Empty shipping_days isn't blocking — model defaults to Mon/Wed/Fri at
        # creation and existing rows are backfilled. We still guard the rare
        # case where every upcoming day is blacked out, since that means the
        # seller genuinely can't ship anything for the next 90 days.
        from sellers.models import _default_shipping_days
        for sid, bucket in seller_buckets.items():
            seller_user = bucket['seller']
            try:
                seller_profile = seller_user.seller_profile
            except Exception:
                seller_profile = None
            store = (seller_profile.store_name if seller_profile else 'A seller in your cart')
            if seller_profile and not (seller_profile.shipping_days or []):
                # Belt-and-braces — if somehow still empty in the DB, fall back to defaults
                seller_profile.shipping_days = _default_shipping_days()
                seller_profile.save(update_fields=['shipping_days'])
            next_ship = seller_profile.get_next_shipping_date() if seller_profile else None
            if not next_ship:
                return Response({
                    "error": f"{store} is on a break and not accepting orders right now. Please remove their items or try again later."
                }, status=400)
            bucket['next_shipping_date'] = next_ship

        # Compute promised delivery range from the buyer's pincode zone
        zone_for_eta = (classify_pincode(effective_pincode).get('zone') if effective_pincode else None)
        min_transit, max_transit = transit_days_for_zone(zone_for_eta)

        # SHIP-003: min order check removed; accept all cart values

        # Resolve per-seller shipping configs in one query
        shipping_config_map = _build_shipping_config_map(seller_buckets.keys())
        for sid, bucket in seller_buckets.items():
            # Determine shipping category: hybrid if both light and heavy, else heavy or light
            if bucket['has_light'] and bucket['has_heavy']:
                cat = 'hybrid'
            elif bucket['has_heavy']:
                cat = 'heavy'
            else:
                cat = 'light'
            bucket['shipping_category'] = cat
            bucket['shipping_config'] = shipping_config_map.get((sid, cat))

        # Totals
        subtotal = sum(float(item.variant.price) * item.quantity for item in cart_items)
        gst_total = sum(
            float(item.variant.price) * item.quantity *
            (float(item.product.categories.first().gst_percentage) / (100 + float(item.product.categories.first().gst_percentage)))
            if item.product.categories.exists() and getattr(item.product.categories.first(), 'gst_percentage', None) else 0
            for item in cart_items
        )
        # Master shipping = sum of per-seller fees (each seller has independent config)
        total_shipping = sum(
            _shipping_fee_for_seller(b['subtotal'], b['shipping_config'])
            for b in seller_buckets.values()
        )
        total_amount = subtotal + total_shipping

        # Create master Order with new number format: JNG-YYYY-XXXXX
        order_number = _generate_order_number()
        while Order.objects.filter(order_number=order_number).exists():
            order_number = _generate_order_number()

        order = Order.objects.create(
            order_number=order_number,
            user=request.user if request.user.is_authenticated else None,
            guest_email=email if not request.user.is_authenticated else None,
            guest_phone=phone if not request.user.is_authenticated else None,
            shipping_address=shipping_address,
            subtotal=subtotal,
            shipping_fee=total_shipping,
            gst_total=gst_total,
            total_amount=total_amount,
            status='pending',
        )

        # Create SubOrders + OrderItems (SHIP-003)
        now = timezone.now()
        dispatch_deadline = now + timedelta(hours=48)

        for idx, (sid, bucket) in enumerate(seller_buckets.items()):
            seller_shipping = _shipping_fee_for_seller(bucket['subtotal'], bucket['shipping_config'])
            promised_ship = bucket.get('next_shipping_date')
            promised_min = (promised_ship + timedelta(days=min_transit)) if promised_ship else None
            promised_max = (promised_ship + timedelta(days=max_transit)) if promised_ship else None
            sub_order = SubOrder.objects.create(
                order=order,
                sub_order_number=_sub_order_number(order_number, idx),
                seller=bucket['seller'],
                subtotal=bucket['subtotal'],
                shipping_fee=seller_shipping,
                seller_total=bucket['subtotal'] + seller_shipping,
                status='pending',
                dispatch_deadline=dispatch_deadline,
                promised_ship_date=promised_ship,
                promised_delivery_min=promised_min,
                promised_delivery_max=promised_max,
            )

            for item in bucket['items']:
                gst_pct = float(item.product.categories.first().gst_percentage) if item.product.categories.exists() else 0
                OrderItem.objects.create(
                    order=order,
                    sub_order=sub_order,
                    product=item.product,
                    variant=item.variant,
                    product_name=item.product.name,
                    variant_name=item.variant.name,
                    unit_price=item.variant.price,
                    gst_percentage=gst_pct,
                    quantity=item.quantity,
                    seller=bucket['seller'],
                )

        active_gateway = PaymentGatewaySettings.get_solo().active_gateway

        # Cashfree
        if active_gateway == PaymentGateway.CASHFREE:
            try:
                cashfree_order = create_cashfree_order(
                    order_id=order.order_number,
                    order_amount=float(total_amount),
                    customer_details={
                        "customer_id": str(request.user.id) if request.user.is_authenticated else "guest",
                        "customer_email": email or "guest@example.com",
                        "customer_phone": ''.join(filter(str.isdigit, str(phone)))[-10:] if phone else "9999999999",
                        "customer_name": request.user.get_full_name().strip() if (request.user.is_authenticated and request.user.get_full_name().strip()) else "Guest User"
                    }
                )
                payment = Payment.objects.create(
                    order=order,
                    gateway=PaymentGateway.CASHFREE,
                    cashfree_order_id=cashfree_order['order_id'],
                    cashfree_session_id=cashfree_order['payment_session_id'],
                    amount=total_amount,
                )
                # Schedule delayed reconciliation checks at 15min, 30min, 1hr, 24hr
                try:
                    from payments.tasks import schedule_payment_checks
                    schedule_payment_checks(payment.id)
                except Exception:
                    pass  # Don't block checkout if Celery is down
                return Response({
                    "gateway": PaymentGateway.CASHFREE,
                    "order": OrderSuccessSerializer(order).data,
                    "payment_session_id": cashfree_order['payment_session_id'],
                    "cashfree_order_id": cashfree_order['order_id'],
                    "amount": total_amount,
                    "currency": "INR",
                }, status=201)
            except Exception:
                # Log full traceback server-side; return a generic message to the
                # client so we never echo exception text (which may contain URLs,
                # request headers, or upstream API error bodies) into the browser.
                logger.exception("Cashfree order initialization failed for order %s", order.order_number)
                return Response({
                    "error": "Payment gateway is temporarily unavailable. Please try again in a moment."
                }, status=400)

        # Razorpay
        try:
            rzp_order = create_razorpay_order(
                receipt=order.order_number,
                amount_inr=float(total_amount),
                currency="INR",
            )
            payment = Payment.objects.create(
                order=order,
                gateway=PaymentGateway.RAZORPAY,
                razorpay_order_id=rzp_order["id"],
                amount=total_amount,
            )
            # Schedule delayed reconciliation checks at 15min, 30min, 1hr, 24hr
            try:
                from payments.tasks import schedule_payment_checks
                schedule_payment_checks(payment.id)
            except Exception:
                pass  # Don't block checkout if Celery is down
            return Response({
                "gateway": PaymentGateway.RAZORPAY,
                "order": OrderSuccessSerializer(order).data,
                "razorpay_order_id": rzp_order["id"],
                "razorpay_key_id": getattr(settings, "RAZORPAY_KEY_ID", ""),
                "amount": total_amount,
                "currency": "INR",
            }, status=201)
        except Exception:
            logger.exception("Razorpay order initialization failed for order %s", order.order_number)
            return Response({
                "error": "Payment gateway is temporarily unavailable. Please try again in a moment."
            }, status=400)

class VerifyPaymentView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        gateway = request.data.get("gateway") or PaymentGateway.CASHFREE

        if gateway == PaymentGateway.RAZORPAY:
            razorpay_order_id = request.data.get("razorpay_order_id")
            razorpay_payment_id = request.data.get("razorpay_payment_id")
            razorpay_signature = request.data.get("razorpay_signature")

            if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
                return Response({"error": "razorpay_order_id, razorpay_payment_id, razorpay_signature are required"}, status=400)

            if not verify_razorpay_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
                return Response({"error": "Invalid payment signature"}, status=400)

            try:
                payment = Payment.objects.get(razorpay_order_id=razorpay_order_id)
                order = payment.order

                # Idempotency guard: already captured → return success, skip double processing
                if payment.status == 'captured':
                    return Response({"message": "Payment already verified"}, status=200)
                insufficient_items = []
                for item in order.items.all():
                    if item.variant and item.variant.stock < item.quantity:
                        insufficient_items.append(item.product_name)
                
                if insufficient_items:
                    # In a real scenario, we would trigger a refund here
                    order.status = 'failed'
                    order.payment_status = 'failed'
                    order.save()
                    return Response({
                        "error": f"Fulfillment integrity compromised. The following specimens went out of stock during your transaction: {', '.join(insufficient_items)}. Please contact support for a refund."
                    }, status=400)

                payment.gateway = PaymentGateway.RAZORPAY
                payment.razorpay_payment_id = razorpay_payment_id
                payment.razorpay_signature = razorpay_signature
                payment.status = 'captured'
                payment.gateway_status = 'captured'
                payment.paid_at = timezone.now()
                # Fetch payment details from Razorpay for method & bank reference
                try:
                    import requests as req
                    from payments.razorpay_utils import _auth
                    key_id, key_secret = _auth()
                    rzp_resp = req.get(
                        f'https://api.razorpay.com/v1/payments/{razorpay_payment_id}',
                        auth=(key_id, key_secret), timeout=10,
                    ).json()
                    payment.method = rzp_resp.get('method', '')
                    payment.bank_reference = (
                        rzp_resp.get('acquirer_data', {}).get('upi_transaction_id', '')
                        or rzp_resp.get('acquirer_data', {}).get('bank_transaction_id', '')
                    )
                    payment.gateway_response = rzp_resp
                except Exception:
                    pass  # Don't block capture if detail fetch fails
                payment.save()
                
                order.is_paid = True
                order.payment_status = 'completed'
                order.status = 'confirmed'
                order.payment_gateway = PaymentGateway.RAZORPAY
                order.save()

                # Update sub-order statuses + reset dispatch clock from payment time
                placed_at = timezone.now()
                for sub in order.sub_orders.all():
                    sub.status = 'placed'
                    sub.dispatch_deadline = placed_at + timedelta(hours=48)
                    sub.save(update_fields=['status', 'dispatch_deadline', 'updated_at'])

                # Deduct stock (sync — must be atomic)
                for item in order.items.select_related('variant').all():
                    if item.variant:
                        item.variant.__class__.objects.filter(pk=item.variant.pk).update(
                            stock=models.F('stock') - item.quantity
                        )

                # Fire async tasks — do not block the API response
                create_order_notifications_task.delay(str(order.id))
                send_order_confirmation_emails_task.delay(str(order.id))
                if order.user_id:
                    from cart.models import CartItem
                    CartItem.objects.filter(cart__user_id=order.user_id).delete()

                return Response({
                    "message": "Payment verified and order placed",
                    "order": OrderSuccessSerializer(order).data
                }, status=200)
            except Payment.DoesNotExist:
                return Response({"error": "Payment record not found"}, status=404)

        # Default: Cashfree
        cashfree_order_id = request.data.get('cashfree_order_id')
        if not cashfree_order_id:
            return Response({"error": "cashfree_order_id is required"}, status=400)
        try:
            is_verified, cf_payment_id, cf_payment_data = verify_cashfree_payment(cashfree_order_id)
        except Exception as e:
            logger.error(f"[VERIFY] Cashfree API error for {cashfree_order_id}: {e}")
            return Response({"error": "Could not reach payment gateway. Please check My Orders or contact support."}, status=502)
        if is_verified:
            try:
                payment = Payment.objects.get(cashfree_order_id=cashfree_order_id)
                order = payment.order

                # Idempotency guard: already captured → return order data so frontend can show success page
                if payment.status == 'captured':
                    return Response({
                        "message": "Payment already verified",
                        "order": OrderSuccessSerializer(order).data
                    }, status=200)
                insufficient_items = []
                for item in order.items.all():
                    if item.variant and item.variant.stock < item.quantity:
                        insufficient_items.append(item.product_name)
                
                if insufficient_items:
                    order.status = 'failed'
                    order.payment_status = 'failed'
                    order.save()
                    return Response({
                        "error": f"Fulfillment integrity compromised. The following specimens went out of stock during your transaction: {', '.join(insufficient_items)}. Please contact support for a refund."
                    }, status=400)

                payment.gateway = PaymentGateway.CASHFREE
                payment.cashfree_payment_id = cf_payment_id
                payment.status = 'captured'
                payment.gateway_status = 'SUCCESS'
                payment.paid_at = timezone.now()
                # Capture method & bank reference from gateway response
                if cf_payment_data:
                    payment.method = cf_payment_data.get('payment_group', '')
                    payment.bank_reference = (
                        cf_payment_data.get('bank_reference', '')
                        or cf_payment_data.get('utr', '')
                    )
                    payment.gateway_response = cf_payment_data
                payment.save()
                
                order.is_paid = True
                order.payment_status = 'completed'
                order.status = 'confirmed'
                order.payment_gateway = PaymentGateway.CASHFREE
                order.save()

                # Update sub-order statuses + reset dispatch clock from payment time
                placed_at = timezone.now()
                for sub in order.sub_orders.all():
                    sub.status = 'placed'
                    sub.dispatch_deadline = placed_at + timedelta(hours=48)
                    sub.save(update_fields=['status', 'dispatch_deadline', 'updated_at'])

                # Deduct stock (sync — must be atomic)
                for item in order.items.select_related('variant').all():
                    if item.variant:
                        item.variant.__class__.objects.filter(pk=item.variant.pk).update(
                            stock=models.F('stock') - item.quantity
                        )

                # Fire async tasks — do not block the API response
                create_order_notifications_task.delay(str(order.id))
                send_order_confirmation_emails_task.delay(str(order.id))
                if order.user_id:
                    from cart.models import CartItem
                    CartItem.objects.filter(cart__user_id=order.user_id).delete()

                return Response({
                    "message": "Payment verified and order placed",
                    "order": OrderSuccessSerializer(order).data
                }, status=200)
            except Payment.DoesNotExist:
                return Response({"error": "Payment record not found"}, status=404)
            except Exception as e:
                logger.error(f"[VERIFY] Unexpected error processing cashfree order {cashfree_order_id}: {e}")
                return Response({"error": "Order processing failed. Your payment was received — please contact support."}, status=500)

        # Payment not yet confirmed — could be still processing or failed
        # Check the actual payment status at Cashfree to give a useful response
        try:
            import requests as req
            from payments.cashfree_utils import get_cashfree_url, get_cashfree_headers
            cf_url = f"{get_cashfree_url()}/{cashfree_order_id}/payments"
            cf_resp = req.get(cf_url, headers=get_cashfree_headers(), timeout=10)
            if cf_resp.status_code == 200:
                for p in cf_resp.json():
                    pstatus = (p.get("payment_status") or "").upper()
                    if pstatus in ("PENDING", "PROCESSING"):
                        return Response({
                            "error": "Payment is still being processed by your bank. Please check My Orders in a few minutes.",
                            "status": "processing",
                        }, status=202)
        except Exception:
            pass

        return Response({"error": "Payment could not be verified. If money was debited, please check My Orders or contact support."}, status=400)


class PaymentStatusView(APIView):
    """
    GET /api/orders/payment-status/?cashfree_order_id=xxx
    GET /api/orders/payment-status/?razorpay_order_id=xxx

    Real-time payment status check — called by the frontend when the
    payment modal closes in an ambiguous state (user entered PIN but
    modal closed before the bank response arrived).

    Returns:
      { status: 'success' | 'processing' | 'failed', order_number? }

    If the gateway confirms success and our DB hasn't captured yet,
    this view captures the payment immediately (same as VerifyPaymentView),
    so the reconcile task doesn't need to wait an hour.
    """
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        cashfree_order_id = request.query_params.get('cashfree_order_id')
        razorpay_order_id = request.query_params.get('razorpay_order_id')

        if not cashfree_order_id and not razorpay_order_id:
            return Response({"error": "cashfree_order_id or razorpay_order_id required"}, status=400)

        # ── Cashfree ────────────────────────────────────────────────────────
        if cashfree_order_id:
            try:
                payment = Payment.objects.select_related('order').get(
                    cashfree_order_id=cashfree_order_id
                )
            except Payment.DoesNotExist:
                return Response({"status": "failed"}, status=404)

            # Already captured in our DB → success
            if payment.status == 'captured':
                return Response({
                    "status": "success",
                    "order_number": payment.order.order_number,
                    "order": OrderSerializer(payment.order).data,
                })

            # Query Cashfree API for real-time status
            try:
                is_paid, cf_payment_id, cf_payment_data = verify_cashfree_payment(cashfree_order_id)
            except Exception:
                # Gateway unreachable → tell frontend to keep polling
                return Response({"status": "processing"})

            if is_paid:
                # Capture immediately (don't wait for reconcile task)
                try:
                    from payments.tasks import _capture_payment
                    _capture_payment(payment, cf_payment_id, cf_payment_data)
                except Exception:
                    pass  # reconcile task will catch it
                return Response({
                    "status": "success",
                    "order_number": payment.order.order_number,
                    "order": OrderSerializer(payment.order).data,
                })

            # Check if Cashfree confirms a failure
            # (verify_cashfree_payment returns False for both PENDING and FAILED)
            # We need to distinguish — call the payments list endpoint directly
            try:
                import requests as req
                from payments.cashfree_utils import get_cashfree_url, get_cashfree_headers
                url = f"{get_cashfree_url()}/{cashfree_order_id}/payments"
                r = req.get(url, headers=get_cashfree_headers(), timeout=10)
                if r.status_code == 200:
                    for p in r.json():
                        pstatus = p.get("payment_status", "").upper()
                        if pstatus in ("FAILED", "CANCELLED", "VOID", "NOT_ATTEMPTED"):
                            return Response({"status": "failed"})
                        if pstatus == "SUCCESS":
                            return Response({
                                "status": "success",
                                "order_number": payment.order.order_number,
                                "order": OrderSerializer(payment.order).data,
                            })
            except Exception:
                pass

            return Response({"status": "processing"})

        # ── Razorpay ─────────────────────────────────────────────────────────
        if razorpay_order_id:
            try:
                payment = Payment.objects.select_related('order').get(
                    razorpay_order_id=razorpay_order_id
                )
            except Payment.DoesNotExist:
                return Response({"status": "failed"}, status=404)

            if payment.status == 'captured':
                return Response({
                    "status": "success",
                    "order_number": payment.order.order_number,
                    "order": OrderSerializer(payment.order).data,
                })

            # Query Razorpay API
            try:
                import requests as req
                from payments.razorpay_utils import _auth
                key_id, key_secret = _auth()
                url = f"https://api.razorpay.com/v1/orders/{razorpay_order_id}/payments"
                r = req.get(url, auth=(key_id, key_secret), timeout=10)
                if r.status_code == 200:
                    items = r.json().get('items', [])
                    # Zero payment attempts at Razorpay = user closed the modal
                    # without entering payment details. Mark cancelled so the
                    # frontend can stop polling immediately.
                    if not items:
                        if payment.status not in ('captured', 'failed', 'cancelled'):
                            payment.status = 'cancelled'
                            payment.gateway_status = 'cancelled'
                            payment.save(update_fields=['status', 'gateway_status', 'updated_at'])
                            order = payment.order
                            if order.status == 'pending':
                                order.status = 'failed'
                                order.payment_status = 'failed'
                                order.save(update_fields=['status', 'payment_status', 'updated_at'])
                            try:
                                from payments.tasks import cancel_payment_checks
                                cancel_payment_checks(payment.id)
                            except Exception:
                                pass
                        return Response({"status": "cancelled"})
                    for p in items:
                        pstatus = p.get('status', '')
                        if pstatus == 'captured':
                            try:
                                from payments.tasks import _capture_payment
                                payment.razorpay_payment_id = p.get('id')
                                _capture_payment(payment, p.get('id'))
                            except Exception:
                                pass
                            return Response({
                                "status": "success",
                                "order_number": payment.order.order_number,
                                "order": OrderSerializer(payment.order).data,
                            })
                        if pstatus == 'failed':
                            return Response({"status": "failed"})
            except Exception:
                pass

            return Response({"status": "processing"})


class CancelOrderView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            order = Order.objects.get(id=pk, user=request.user)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=404)

        if order.status != 'pending':
            return Response({'error': f'Cannot cancel order with status {order.status}'}, status=400)

        order.status = 'cancelled'
        order.payment_status = 'failed'
        order.save()

        # Cancel sub-orders
        for sub in order.sub_orders.all():
            sub.status = 'cancelled'
            sub.save()

        return Response({'message': 'Order cancelled successfully'})


class OrderListView(generics.ListAPIView):
    serializer_class = OrderListSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        user = self.request.user
        qs = Order.objects.filter(user=user)

        # Single query for items + their product images — eliminates all N+1
        items_qs = OrderItem.objects.select_related('product').prefetch_related('product__images')
        sub_orders_qs = SubOrder.objects.select_related(
            'seller', 'seller__seller_profile'
        ).prefetch_related(
            Prefetch('items', queryset=items_qs),
        )
        return qs.prefetch_related(
            Prefetch('items', queryset=items_qs),
            Prefetch('sub_orders', queryset=sub_orders_qs),
            Prefetch('shipments', to_attr='shipments_prefetched'),
        ).order_by('-created_at')


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderDetailSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.role == 'admin':
            qs = Order.objects.all()
        else:
            qs = Order.objects.filter(user=user)

        items_qs = OrderItem.objects.select_related('product', 'variant').prefetch_related('product__images')
        sub_orders_qs = SubOrder.objects.select_related(
            'seller', 'seller__seller_profile'
        ).prefetch_related(
            Prefetch('items', queryset=items_qs),
        )
        return qs.prefetch_related(
            Prefetch('items', queryset=items_qs),
            Prefetch('sub_orders', queryset=sub_orders_qs),
            Prefetch('shipments', to_attr='shipments_prefetched'),
        )


class OrderTrackView(APIView):
    """
    GET /api/orders/track/?order_id=<uuid>
    Public order tracking endpoint.
    Anyone with a valid order_id can track the order status.
    """
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        order_id = request.query_params.get('order_id')
        order_number = (request.query_params.get('order_number') or '').strip().upper() or None

        if not order_id and not order_number:
            return Response({'error': 'order_id or order_number query parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        order_uuid = None
        if order_id:
            try:
                order_uuid = uuid.UUID(str(order_id))
            except ValueError:
                return Response({'error': 'Invalid order_id format'}, status=status.HTTP_400_BAD_REQUEST)

        # Build optimised queryset with all prefetches the serializer needs
        items_qs = OrderItem.objects.select_related('product', 'variant').prefetch_related('product__images')
        sub_orders_qs = SubOrder.objects.select_related(
            'seller', 'seller__seller_profile'
        ).prefetch_related(
            Prefetch('items', queryset=items_qs),
        )
        qs = Order.objects.select_related('payment').prefetch_related(
            Prefetch('items', queryset=items_qs),
            Prefetch('sub_orders', queryset=sub_orders_qs),
            'shipments',
        )

        if order_uuid:
            order = qs.filter(id=order_uuid).first()
        else:
            order = qs.filter(order_number=order_number).first()

        if not order:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response(OrderTrackingSerializer(order).data)


class SellerOrderListView(generics.ListAPIView):
    """Grower-scoped order list — items and shipment pre-filtered to the seller."""
    serializer_class = SellerOrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return (
            Order.objects
            .filter(items__seller=self.request.user, is_paid=True)
            .distinct()
            .prefetch_related('items', 'shipments')
            .order_by('-created_at')
        )


class SellerSubOrderListView(generics.ListAPIView):
    """List sub-orders belonging to the requesting seller."""
    serializer_class = SellerSubOrderSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        from shipping.models import Shipment
        from core.models import ProductImage
        seller = self.request.user
        items_qs = OrderItem.objects.select_related('product').prefetch_related(
            Prefetch('product__images', queryset=ProductImage.objects.all()),
        )
        qs = SubOrder.objects.filter(
            seller=seller, order__is_paid=True
        ).select_related(
            'order', 'order__user'
        ).prefetch_related(
            Prefetch('items', queryset=items_qs),
            Prefetch(
                'order__shipments',
                queryset=Shipment.objects.filter(seller=seller),
                to_attr='seller_shipments',
            ),
        )
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by('-created_at')

    def paginate_queryset(self, queryset):
        if self.request.query_params.get('no_pagination'):
            return None
        return super().paginate_queryset(queryset)


class SellerSubOrderDetailView(APIView):
    """GET /orders/seller/sub-orders/<pk>/ — fetch a single sub-order for the seller."""
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class ConfirmSubOrderView(APIView):
    """
    Seller confirms a sub-order — collects approximate package weight + dimensions
    and immediately queues courier booking so the label is generated up-front.

    Flow:
        placed → confirmed (this view) → booked (Celery sets after AWB returns)

    Packaging photos are NOT required here — the seller updates the actuals and
    uploads photos later from the Booking Courier tab before pickup is scheduled.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if sub_order.status != 'placed':
            return Response({'error': f'Cannot confirm: current status is {sub_order.status}'}, status=status.HTTP_400_BAD_REQUEST)

        weight = request.data.get('actual_weight_grams') or request.data.get('weight_grams')
        length = request.data.get('actual_length_cm') or request.data.get('length_cm')
        breadth = request.data.get('actual_breadth_cm') or request.data.get('breadth_cm')
        height = request.data.get('actual_height_cm') or request.data.get('height_cm')

        errors = {}
        try:
            w = int(weight)
            if not (1 <= w <= 30000):
                errors['actual_weight_grams'] = 'Must be between 1 and 30,000 grams.'
        except (TypeError, ValueError):
            errors['actual_weight_grams'] = 'Approximate weight in grams is required.'

        parsed_dims = {}
        for field, raw in (
            ('actual_length_cm', length),
            ('actual_breadth_cm', breadth),
            ('actual_height_cm', height),
        ):
            try:
                v = int(raw)
                if v <= 0:
                    errors[field] = 'Must be a positive integer.'
                else:
                    parsed_dims[field] = v
            except (TypeError, ValueError):
                errors[field] = 'Approximate dimension (cm) is required.'

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        sub_order.status = 'confirmed'
        sub_order.confirmed_at = now
        sub_order.dispatch_deadline = now + timedelta(hours=48)
        sub_order.actual_weight_grams = w
        for field, value in parsed_dims.items():
            setattr(sub_order, field, value)
        sub_order.save(update_fields=[
            'status', 'confirmed_at', 'dispatch_deadline',
            'actual_weight_grams', 'actual_length_cm', 'actual_breadth_cm', 'actual_height_cm',
            'updated_at',
        ])

        buyer = sub_order.order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Order Confirmed!',
                message=f'Your order {sub_order.sub_order_number} has been confirmed by the seller and is being prepared.',
            )

        # Queue courier booking right away so the seller can download the label.
        # Pickup is NOT scheduled here — that happens at Ship Now once the seller
        # has uploaded a packaging photo and confirmed actual measurements.
        from shipping.tasks import create_shipment_task
        try:
            create_shipment_task.delay(
                str(sub_order.order_id), str(request.user.id), None, str(sub_order.id),
                schedule_pickup=False,
            )
        except Exception as broker_err:
            logger.warning(
                "Celery broker unavailable (%s); running create_shipment_task synchronously",
                broker_err,
            )
            create_shipment_task(
                str(sub_order.order_id), str(request.user.id), None, str(sub_order.id),
                schedule_pickup=False,
            )

        sub_order.refresh_from_db()
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class UploadPackagingPhotoView(APIView):
    """Seller uploads packaging photo URL(s) for a sub-order."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        photo_url = request.data.get('photo_url')
        if not photo_url:
            return Response({'error': 'photo_url required'}, status=status.HTTP_400_BAD_REQUEST)

        photos = sub_order.packaging_photos or []
        photos.append(photo_url)
        sub_order.packaging_photos = photos
        if sub_order.status == 'confirmed':
            sub_order.status = 'packing'
        sub_order.save(update_fields=['packaging_photos', 'status', 'updated_at'])

        return Response({'packaging_photos': sub_order.packaging_photos, 'status': sub_order.status})


class UpdateShipmentDetailsView(APIView):
    """Seller saves actual package weight + dimensions before shipping."""
    permission_classes = (permissions.IsAuthenticated,)

    def patch(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if sub_order.status in ('shipped', 'in_transit', 'out_for_delivery', 'delivered', 'cancelled'):
            return Response({'error': 'Cannot update shipment details after dispatch.'}, status=status.HTTP_400_BAD_REQUEST)

        weight = request.data.get('actual_weight_grams')
        length = request.data.get('actual_length_cm')
        breadth = request.data.get('actual_breadth_cm')
        height = request.data.get('actual_height_cm')

        errors = {}
        if weight is not None:
            try:
                w = int(weight)
                if not (1 <= w <= 30000):
                    errors['actual_weight_grams'] = 'Must be between 1 and 30,000 grams.'
                else:
                    sub_order.actual_weight_grams = w
            except (ValueError, TypeError):
                errors['actual_weight_grams'] = 'Must be a positive integer.'

        for field, val in [('actual_length_cm', length), ('actual_breadth_cm', breadth), ('actual_height_cm', height)]:
            if val is not None:
                try:
                    v = int(val)
                    if v <= 0:
                        errors[field] = 'Must be a positive integer.'
                    else:
                        setattr(sub_order, field, v)
                except (ValueError, TypeError):
                    errors[field] = 'Must be a positive integer.'

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        sub_order.save(update_fields=['actual_weight_grams', 'actual_length_cm', 'actual_breadth_cm', 'actual_height_cm', 'updated_at'])
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class SubOrderShipView(APIView):
    """
    Seller marks a booked sub-order as ready for pickup.

    Pre-conditions:
        - Status is 'booked' (label already generated at Confirm step) OR
          'booking_failed' (retry path).
        - At least one packaging photo uploaded.
        - Actual weight + L×B×H persisted.

    On success: requests pickup from the logistics provider.
    Manual-AWB sellers skip the pickup call and are simply marked as ready.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        manual_awb = (request.data.get('awb_number') or '').strip()
        manual_courier = (request.data.get('courier_name') or '').strip()
        courier_id = request.data.get('courier_id')

        # Allow Ship Now from booked (most common), booking_failed (retry), or
        # legacy confirmed/packing for backward compatibility with older orders
        # that were confirmed before the new flow shipped.
        allowed = ('booked', 'booking_failed', 'confirmed', 'packing')
        if sub_order.status not in allowed:
            return Response(
                {'error': f'Cannot ship: current status is {sub_order.status}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not sub_order.packaging_photos:
            return Response(
                {'error': 'Upload at least one packaging photo before scheduling pickup.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        missing = []
        if not sub_order.actual_weight_grams:
            missing.append('actual weight (grams)')
        if not (sub_order.actual_length_cm and sub_order.actual_breadth_cm and sub_order.actual_height_cm):
            missing.append('box dimensions (L × B × H)')
        if missing:
            return Response(
                {'error': f'Please fill in {" and ".join(missing)} before shipping.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Manual AWB path — seller booked their own courier outside Shiprocket.
        if manual_awb:
            sub_order.awb_number = manual_awb
            sub_order.courier_name = manual_courier or 'Manual'
            sub_order.status = 'booked'
            sub_order.booking_failure_reason = None
            sub_order.save(update_fields=['awb_number', 'courier_name', 'status', 'booking_failure_reason', 'updated_at'])
            buyer = sub_order.order.user
            if buyer:
                AppNotification.objects.create(
                    user=buyer,
                    title='Your order is being packed!',
                    message=f'Order {sub_order.sub_order_number} is being prepared for dispatch.',
                )
            return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)

        # Retry path after booking failure: clear the stale failure record and
        # re-run the full booking task (so a new AWB + label is generated).
        if sub_order.status == 'booking_failed' or not sub_order.awb_number:
            if sub_order.status == 'booking_failed':
                SubOrder.objects.filter(id=sub_order.id).update(booking_failure_reason=None)
                from shipping.models import Shipment
                Shipment.objects.filter(
                    order=sub_order.order, seller=request.user, status='pending',
                ).delete()
            sub_order.status = 'booked'
            sub_order.save(update_fields=['status', 'updated_at'])
            from shipping.tasks import create_shipment_task
            try:
                create_shipment_task.delay(
                    str(sub_order.order_id), str(request.user.id), courier_id, str(sub_order.id),
                    schedule_pickup=True,
                )
            except Exception as broker_err:
                logger.warning(
                    "Celery broker unavailable (%s); running create_shipment_task synchronously",
                    broker_err,
                )
                create_shipment_task(
                    str(sub_order.order_id), str(request.user.id), courier_id, str(sub_order.id),
                    schedule_pickup=True,
                )
        else:
            # Standard path: AWB already exists from the Confirm step — only the
            # pickup needs to be requested from Shiprocket / NimbusPost.
            from shipping.tasks import schedule_pickup_task
            try:
                schedule_pickup_task.delay(str(sub_order.id))
            except Exception as broker_err:
                logger.warning(
                    "Celery broker unavailable (%s); running schedule_pickup_task synchronously",
                    broker_err,
                )
                schedule_pickup_task(str(sub_order.id))

        buyer = sub_order.order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Your order is being packed!',
                message=f'Order {sub_order.sub_order_number} is being prepared for dispatch.',
            )

        sub_order.refresh_from_db()
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class RefreshShippingLabelView(APIView):
    """
    POST /orders/seller/sub-orders/<pk>/refresh-label/

    Shiprocket sometimes returns an empty label_url at the time the AWB is
    assigned because the PDF is still being generated. This endpoint lets the
    seller re-fetch the label by AWB without re-creating the shipment.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        try:
            sub_order = SubOrder.objects.select_related('order').get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        if not sub_order.awb_number:
            return Response({'error': 'Courier has not been booked yet — no AWB to fetch a label for.'}, status=status.HTTP_400_BAD_REQUEST)

        from shipping.models import Shipment
        from shipping.services import get_logistics_service
        shipment = Shipment.objects.filter(order=sub_order.order, seller=request.user).first()

        try:
            result = get_logistics_service().generate_label([sub_order.awb_number])
        except Exception as exc:
            logger.error("RefreshShippingLabelView: provider raised %s", exc)
            return Response({'error': 'Could not reach the courier API. Try again in a moment.'}, status=status.HTTP_502_BAD_GATEWAY)

        label_url = (result or {}).get('data') if (result or {}).get('status') else None
        if not label_url:
            return Response(
                {'error': 'Label not ready yet — please retry in a minute. Shiprocket usually needs 30–60 seconds after AWB assignment.'},
                status=status.HTTP_202_ACCEPTED,
            )

        if shipment:
            Shipment.objects.filter(pk=shipment.pk).update(label_url=label_url)

        sub_order.refresh_from_db()
        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)


class ShipNowView(APIView):
    """
    POST /api/orders/ship-now/
    Body: { order_id, courier_id (optional) }
    Updates order status, notifies buyer, triggers NimbusPost shipment creation.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        order_id = request.data.get('order_id')
        courier_id = request.data.get('courier_id')

        if not order_id:
            return Response({'error': 'order_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order = Order.objects.get(id=order_id, items__seller=request.user)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

        # Update order status to processing
        if order.status not in ('shipped', 'delivered', 'cancelled'):
            Order.objects.filter(id=order_id).update(status='processing')

        # Notify buyer
        buyer = order.user
        if buyer:
            AppNotification.objects.create(
                user=buyer,
                title='Your order is being packed!',
                message=(
                    f'Great news! Order {order.order_number} is being prepared for shipment '
                    f'by your grower. You will receive tracking details soon.'
                ),
            )

        # Trigger async NimbusPost shipment creation
        from shipping.tasks import create_shipment_task
        create_shipment_task.delay(str(order_id), str(request.user.id), courier_id)

        return Response(
            {'message': 'Shipment initiated', 'order_id': str(order_id)},
            status=status.HTTP_202_ACCEPTED,
        )


# ── Notification message templates ───────────────────────────────────────────
_BUYER_MSGS = {
    'in_transit':       ('Your order is in transit!',         'Sub-order {sub} is on its way. AWB: {awb}.'),
    'out_for_delivery': ('Out for delivery today!',            'Sub-order {sub} will be delivered today. Keep your phone handy!'),
    'delivered':        ('Delivered!',                         'Sub-order {sub} has been delivered. We hope you love your specimens!'),
    'delivery_failed':  ('Delivery attempt failed',            'The delivery of {sub} was unsuccessful. The courier will retry soon.'),
    'doa_raised':       ('DOA complaint raised',               'A Dead-on-Arrival complaint for {sub} has been registered. Our team will be in touch.'),
    'cancelled':        ('Order cancelled',                    'Sub-order {sub} has been cancelled. If you paid, a refund will be processed.'),
}
_SELLER_MSGS = {
    'delivered':        ('Sub-order delivered',                'Sub-order {sub} has been delivered successfully.'),
    'delivery_failed':  ('Delivery failed — action needed',    'Sub-order {sub} delivery failed. Please coordinate with the courier.'),
    'cancelled':        ('Sub-order cancelled',                'Sub-order {sub} has been cancelled.'),
}

# Allowed status transitions (current → allowed next states)
_VALID_TRANSITIONS = {
    'placed':           {'confirmed', 'cancelled'},
    'confirmed':        {'packing', 'cancelled'},
    'packing':          {'booked', 'shipped', 'cancelled'},
    'booked':           {'booking_failed', 'shipped', 'cancelled'},
    'booking_failed':   {'packing', 'booked', 'cancelled'},       # seller retries or manual AWB
    'shipped':          {'in_transit', 'delivered', 'delivery_failed', 'cancelled'},
    'in_transit':       {'out_for_delivery', 'delivered', 'delivery_failed', 'cancelled'},
    'out_for_delivery': {'delivered', 'delivery_failed'},
    'delivery_failed':  {'out_for_delivery', 'delivered', 'cancelled'},
    'delivered':        {'doa_raised'},
    'doa_raised':       set(),
    'cancelled':        set(),
}


class UpdateSubOrderStatusView(APIView):
    """
    PATCH /api/orders/seller/sub-orders/<pk>/status/
    Called by NimbusPost webhook, admin panel, or seller for manual overrides.
    Fires buyer + seller notifications for each status change.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def patch(self, request, pk):
        # Sellers can only update their own sub-orders; staff/admin can update any
        try:
            if request.user.is_staff or getattr(request.user, 'role', '') == 'admin':
                sub_order = SubOrder.objects.select_related('order', 'order__user', 'seller').get(id=pk)
            else:
                sub_order = SubOrder.objects.select_related('order', 'order__user', 'seller').get(id=pk, seller=request.user)
        except SubOrder.DoesNotExist:
            return Response({'error': 'Sub-order not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        if not new_status:
            return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)

        allowed = _VALID_TRANSITIONS.get(sub_order.status, set())
        if new_status not in allowed:
            return Response(
                {'error': f"Cannot transition from '{sub_order.status}' to '{new_status}'. Allowed: {sorted(allowed) or 'none'}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sub_order.status = new_status
        # Auto-set timestamps
        if new_status == 'confirmed' and not sub_order.confirmed_at:
            sub_order.confirmed_at = timezone.now()
            sub_order.dispatch_deadline = sub_order.confirmed_at + timedelta(hours=48)
        sub_order.save()

        awb = sub_order.awb_number or 'pending'
        notifs = []

        # Notify buyer
        buyer = sub_order.order.user
        guest_email = sub_order.order.guest_email
        if new_status in _BUYER_MSGS:
            title, msg_tpl = _BUYER_MSGS[new_status]
            msg = msg_tpl.format(sub=sub_order.sub_order_number, awb=awb)
            if buyer:
                notifs.append(AppNotification(user=buyer, title=title, message=msg))
            # TODO: send email to guest_email if no buyer account

        # Notify seller for key events
        if new_status in _SELLER_MSGS:
            title, msg_tpl = _SELLER_MSGS[new_status]
            msg = msg_tpl.format(sub=sub_order.sub_order_number)
            notifs.append(AppNotification(user=sub_order.seller, title=title, message=msg))

        if notifs:
            AppNotification.objects.bulk_create(notifs)

        return Response(SellerSubOrderSerializer(sub_order, context={'request': request}).data)
