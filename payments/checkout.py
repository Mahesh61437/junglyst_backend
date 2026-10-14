"""
Gateway initiation shared by every order type.

Creates the gateway-side order for an already-saved Order, records the
Payment row, schedules the reconcile checks, and returns the payload the
frontend needs to open the Cashfree / Razorpay modal. Capture then runs
through the existing verify / status / webhook / reconcile paths.
"""
import logging
from dataclasses import dataclass

from django.conf import settings

from .cashfree_utils import create_cashfree_order
from .models import Payment, PaymentGateway, PaymentGatewaySettings
from .razorpay_utils import create_razorpay_order

logger = logging.getLogger(__name__)

GATEWAY_UNAVAILABLE_MESSAGE = "Payment gateway is temporarily unavailable. Please try again in a moment."
FALLBACK_CUSTOMER_EMAIL = "guest@example.com"
FALLBACK_CUSTOMER_PHONE = "9999999999"


class GatewayUnavailable(Exception):
    """The gateway order could not be created. Message is safe for clients."""


@dataclass
class PaymentCustomer:
    customer_id: str
    email: str
    phone: str
    name: str

    @property
    def gateway_phone(self):
        digits = ''.join(filter(str.isdigit, str(self.phone or '')))
        return digits[-10:] if digits else FALLBACK_CUSTOMER_PHONE


def _schedule_reconcile(payment):
    try:
        from payments.tasks import schedule_payment_checks
        schedule_payment_checks(payment.id)
    except Exception:
        pass  # Don't block checkout if Celery is down


def start_gateway_payment(order, customer):
    """Return the gateway payload dict (without the serialized order).

    Raises GatewayUnavailable when the active gateway rejects the request.
    """
    amount = order.total_amount
    active_gateway = PaymentGatewaySettings.get_solo().active_gateway

    if active_gateway == PaymentGateway.CASHFREE:
        try:
            cashfree_order = create_cashfree_order(
                order_id=order.order_number,
                order_amount=float(amount),
                customer_details={
                    "customer_id": customer.customer_id,
                    "customer_email": customer.email or FALLBACK_CUSTOMER_EMAIL,
                    "customer_phone": customer.gateway_phone,
                    "customer_name": customer.name,
                },
            )
            payment = Payment.objects.create(
                order=order,
                gateway=PaymentGateway.CASHFREE,
                cashfree_order_id=cashfree_order['order_id'],
                cashfree_session_id=cashfree_order['payment_session_id'],
                amount=amount,
            )
        except Exception as exc:
            # Full traceback stays server-side; never echo gateway errors to clients.
            logger.exception("Cashfree order initialization failed for order %s", order.order_number)
            raise GatewayUnavailable(GATEWAY_UNAVAILABLE_MESSAGE) from exc
        _schedule_reconcile(payment)
        return {
            "gateway": PaymentGateway.CASHFREE,
            "payment_session_id": cashfree_order['payment_session_id'],
            "cashfree_order_id": cashfree_order['order_id'],
            "amount": amount,
            "currency": "INR",
        }

    try:
        rzp_order = create_razorpay_order(
            receipt=order.order_number,
            amount_inr=float(amount),
            currency="INR",
        )
        payment = Payment.objects.create(
            order=order,
            gateway=PaymentGateway.RAZORPAY,
            razorpay_order_id=rzp_order["id"],
            amount=amount,
        )
    except Exception as exc:
        logger.exception("Razorpay order initialization failed for order %s", order.order_number)
        raise GatewayUnavailable(GATEWAY_UNAVAILABLE_MESSAGE) from exc
    _schedule_reconcile(payment)
    return {
        "gateway": PaymentGateway.RAZORPAY,
        "razorpay_order_id": rzp_order["id"],
        "razorpay_key_id": getattr(settings, "RAZORPAY_KEY_ID", ""),
        "amount": amount,
        "currency": "INR",
    }
