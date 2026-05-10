import requests
from django.conf import settings

def get_cashfree_headers():
    return {
        "x-client-id": settings.CASHFREE_APP_ID,
        "x-client-secret": settings.CASHFREE_SECRET_KEY,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def get_cashfree_url():
    if settings.CASHFREE_ENVIRONMENT == 'PRODUCTION':
        return "https://api.cashfree.com/pg/orders"
    return "https://sandbox.cashfree.com/pg/orders"

def create_cashfree_order(order_id, order_amount, customer_details, order_currency="INR"):
    url = get_cashfree_url()
    headers = get_cashfree_headers()
    
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:5173')
    
    payload = {
        "order_id": order_id,
        "order_amount": order_amount,
        "order_currency": order_currency,
        "customer_details": {
            "customer_id": customer_details.get("customer_id", "guest"),
            "customer_email": customer_details.get("customer_email", "guest@example.com"),
            "customer_phone": customer_details.get("customer_phone", "9999999999"),
            "customer_name": customer_details.get("customer_name", "Guest User")
        },
        "order_meta": {
            "return_url": f"{frontend_url}/payment-status?order_id={order_id}",
            "payment_methods_filters": {
                "methods": {
                    "action": "ALLOW",
                    "values": ["upi"]
                }
            }
        }
    }
    
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Cashfree order creation failed: {response.text}")

def verify_cashfree_payment(order_id):
    url = f"{get_cashfree_url()}/{order_id}/payments"
    headers = get_cashfree_headers()
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        payments = response.json()
        for payment in payments:
            if payment.get("payment_status") == "SUCCESS":
                return True, payment.get("cf_payment_id")
    return False, None

def verify_webhook_signature(payload_body, signature):
    """
    Verify Cashfree webhook HMAC-SHA256 signature.

    Note: No timestamp-staleness check here because Cashfree retries
    failed webhooks for up to 24 hours using the SAME timestamp + signature.
    A 5-minute window would block legitimate retries.

    Replay attacks are harmless — handle_order_paid is idempotent
    (checks payment.status == 'captured' and returns early).
    The HMAC signature itself prevents forgery, which is the real threat.
    """
    import hmac
    import hashlib
    import base64

    timestamp = signature.get('x-webhook-timestamp', '')
    actual_signature = signature.get('x-webhook-signature', '')

    if not timestamp or not actual_signature:
        return False

    data = timestamp + payload_body
    secret = settings.CASHFREE_SECRET_KEY.encode('utf-8')

    expected_signature = base64.b64encode(
        hmac.new(secret, data.encode('utf-8'), hashlib.sha256).digest()
    ).decode('utf-8')

    return hmac.compare_digest(expected_signature, actual_signature)
