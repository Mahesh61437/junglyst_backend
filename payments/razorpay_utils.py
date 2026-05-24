import hmac
import hashlib
import base64
import requests
from django.conf import settings



def _auth():
    key_id = getattr(settings, "RAZORPAY_KEY_ID", None)
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", None)
    if not key_id or not key_secret:
        # Names only, never values — this exception may be logged or surfaced.
        raise Exception("Razorpay keys are not configured (RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET).")
    return key_id, key_secret


def create_razorpay_order(receipt: str, amount_inr: float, currency: str = "INR"):
    """
    Create a Razorpay Order via REST API.
    amount is in INR on input; Razorpay expects smallest unit (paise).
    Note: payment method restriction (UPI/QR only) is enforced on the
    frontend via config.display in the Razorpay checkout options.
    """
    key_id, key_secret = _auth()
    amount_paise = int(round(float(amount_inr) * 100))
    if amount_paise < 100:
        raise ValueError("Razorpay order amount must be at least 100 paise (₹1).")

    url = "https://api.razorpay.com/v1/orders"
    payload = {
        "amount": amount_paise,
        "currency": currency,
        "receipt": receipt,
        "payment_capture": 1,
    }
    r = requests.post(url, json=payload, auth=(key_id, key_secret), timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    # Status code only — `r.text` can contain Razorpay error bodies that we
    # don't want surfaced to the client if this exception bubbles up.
    # Full response body is logged by the caller via logger.exception().
    raise Exception(f"Razorpay order creation failed: HTTP {r.status_code}")


def verify_razorpay_signature(razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
    """
    Razorpay signature = HMAC_SHA256(order_id|payment_id, key_secret) (hex digest)
    """
    _, key_secret = _auth()
    message = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
    expected = hmac.new(key_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature or "")

