import requests
import logging
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

NIMBUSPOST_BASE = "https://ship.nimbuspost.com/api"
TOKEN_CACHE_KEY = "nimbuspost_token"
TOKEN_TTL = 3600 * 23  # 23 hours (token valid 24 h)


class NimbuspostService:
    """
    NimbusPost B2B Logistics API client.
    All requests use a JWT obtained from /users/login, cached in Redis.
    """

    # ── Authentication ────────────────────────────────────────────────────────

    @classmethod
    def get_token(cls) -> str | None:
        token = cache.get(TOKEN_CACHE_KEY)
        if token:
            return token

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/users/login",
            json={
                "email": settings.NIMBUSPOST_EMAIL,
                "password": settings.NIMBUSPOST_PASSWORD,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status"):
                token = body["data"]
                cache.set(TOKEN_CACHE_KEY, token, TOKEN_TTL)
                return token
        logger.error("NimbusPost login failed: %s %s", resp.status_code, resp.text[:200])
        return None

    @classmethod
    def _headers(cls, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Rate & Serviceability ─────────────────────────────────────────────────

    @classmethod
    def check_serviceability(
        cls,
        origin_pincode: str,
        destination_pincode: str,
        weight_kg: float,
        order_value: float = 0,
        length: float = 10,
        breadth: float = 10,
        height: float = 10,
    ) -> dict | None:
        """
        POST /courier/b2b_serviceability
        Returns list of available couriers with rates.
        """
        token = cls.get_token()
        if not token:
            return None

        payload = {
            "origin": str(origin_pincode),
            "destination": str(destination_pincode),
            "payment_type": "prepaid",
            "details": [
                {
                    "qty": 1,
                    "weight": float(weight_kg),
                    "length": float(length),
                    "breadth": float(breadth),
                    "height": float(height),
                }
            ],
            "order_value": str(order_value),
        }

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/courier/b2b_serviceability",
            json=payload,
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Serviceability check failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Create Shipment ───────────────────────────────────────────────────────

    @classmethod
    def create_shipment(cls, payload: dict) -> dict | None:
        """
        POST /shipmentcargo/create
        Payload must be the full B2B shipment dict.
        Returns NimbusPost response (status, data with awb_number, label, manifest, …).
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/shipmentcargo/create",
            json=payload,
            headers=cls._headers(token),
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Create shipment failed: %s %s", resp.status_code, resp.text[:500])
        return None

    # ── Track Shipment ────────────────────────────────────────────────────────

    @classmethod
    def track_shipment(cls, awb_number: str) -> dict | None:
        """
        GET /shipmentcargo/track/{awb_number}
        Returns tracking history and current status.
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.get(
            f"{NIMBUSPOST_BASE}/shipmentcargo/track/{awb_number}",
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Track shipment failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Generate Manifest ─────────────────────────────────────────────────────

    @classmethod
    def generate_manifest(cls, awb_numbers: list[str]) -> dict | None:
        """
        POST /shipmentcargo/pickup
        Accepts list of AWBs, returns PDF manifest URL.
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/shipmentcargo/pickup",
            json={"awbs": awb_numbers},
            headers=cls._headers(token),
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Generate manifest failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Cancel Shipment ───────────────────────────────────────────────────────

    @classmethod
    def cancel_shipment(cls, awb_number: str) -> dict | None:
        """
        POST /shipmentcargo/Cancel
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/shipmentcargo/Cancel",
            json={"awb": awb_number},
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Cancel shipment failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Generate MPS Label ────────────────────────────────────────────────────

    @classmethod
    def generate_label(cls, awb_numbers: list[str]) -> dict | None:
        """
        POST /shipmentcargo/generate_mps_label
        Returns a PDF label URL for up to 500 AWBs.
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.post(
            f"{NIMBUSPOST_BASE}/shipmentcargo/generate_mps_label",
            json={"master_awbs": awb_numbers},
            headers=cls._headers(token),
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Generate label failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Wallet Balance ────────────────────────────────────────────────────────

    @classmethod
    def get_wallet_balance(cls) -> dict | None:
        """
        GET /shipmentcargo/wallet_balance
        Note: 'available_limit' is the actual usable balance.
        """
        token = cls.get_token()
        if not token:
            return None

        resp = requests.get(
            f"{NIMBUSPOST_BASE}/shipmentcargo/wallet_balance",
            headers=cls._headers(token),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error("Wallet balance failed: %s %s", resp.status_code, resp.text[:200])
        return None
