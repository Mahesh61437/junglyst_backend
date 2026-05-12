import razorpay
from django.conf import settings


def get_razorpay_client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise ValueError('Razorpay gateway is not configured. Please set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.')
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_razorpay_order(amount_in_paise, currency="INR"):
    client = get_razorpay_client()
    data = {
        "amount": amount_in_paise,
        "currency": currency,
        "payment_capture": 1 # Auto capture
    }
    try:
        return client.order.create(data=data)
    except Exception as exc:
        raise RuntimeError(f"Razorpay order creation failed: {exc}") from exc

def verify_payment_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
    client = get_razorpay_client()
    params_dict = {
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature
    }
    try:
        client.utility.verify_payment_signature(params_dict)
        return True
    except:
        return False
