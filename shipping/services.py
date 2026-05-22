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


# ── Shiprocket ────────────────────────────────────────────────────────────────

SHIPROCKET_BASE = "https://apiv2.shiprocket.in/v1/external"
SHIPROCKET_TOKEN_CACHE_KEY = "shiprocket_token"
SHIPROCKET_TOKEN_TTL = 3600 * 230  # 230 hours (tokens valid for 240 h per docs; refresh before expiry)


class ShiprocketService:
    """
    Shiprocket Logistics API client.
    Implements the same interface as NimbuspostService so views/tasks can
    swap providers via get_logistics_service() without any other changes.
    """

    # ── Authentication ────────────────────────────────────────────────────────

    @classmethod
    def get_token(cls) -> str | None:
        token = cache.get(SHIPROCKET_TOKEN_CACHE_KEY)
        if token:
            return token

        resp = requests.post(
            f"{SHIPROCKET_BASE}/auth/login",
            json={
                "email": settings.SHIPROCKET_EMAIL,
                "password": settings.SHIPROCKET_PASSWORD,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            token = body.get("token")
            if token:
                cache.set(SHIPROCKET_TOKEN_CACHE_KEY, token, SHIPROCKET_TOKEN_TTL)
                return token
        logger.error("Shiprocket login failed: %s %s", resp.status_code, resp.text[:200])
        return None

    @classmethod
    def _headers(cls, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Pickup Location Management ────────────────────────────────────────────

    @classmethod
    def ensure_seller_pickup_location(cls, seller, profile) -> str:
        """
        Returns the Shiprocket pickup_location name for this seller.

        Only locations with status=1 (verified/active) are used — Shiprocket rejects
        status=2 (pending phone verification) locations at order creation time.

        Flow:
          1. Return cached value if present (was verified at cache time).
          2. Scan existing account locations for an active (status=1) match by name.
          3. Register the seller's address via addpickup.
          4. After registration, re-fetch and confirm status=1 before caching.
             If still status=2 (pending OTP), do NOT cache — fall back to any
             other active location so this shipment can proceed.
          5. Ultimate fallback: SHIPROCKET_PICKUP_LOCATION setting.
        """
        import re
        from django.conf import settings as _s
        fallback = getattr(_s, 'SHIPROCKET_PICKUP_LOCATION', 'Primary')

        cached = getattr(profile, 'shiprocket_pickup_location', None)
        if cached:
            return cached

        token = cls.get_token()
        if not token:
            logger.error("Shiprocket auth failed — cannot register pickup for seller %s", seller.id)
            return fallback

        # Build a clean location name (Shiprocket only allows alphanumeric + spaces, max 50)
        raw = profile.store_name or seller.get_full_name() or seller.username or 'Seller'
        location_name = re.sub(r'[^A-Za-z0-9 ]', '', raw).strip()[:50] or f"Seller{str(seller.id)[:8]}"

        # ── Step 1: check whether an active matching location already exists ──
        pre_check = cls._fetch_active_pickup(profile, location_name, token, None)
        if pre_check:
            logger.info("Shiprocket: reusing existing active pickup '%s' for seller %s", pre_check, seller.id)
            return pre_check

        # ── Step 2: register the seller's address ──
        # Phone is mandatory for Shiprocket pickup registration (OTP verification).
        # Use seller.phone (User model) — no fake fallback; missing phone = skip registration.
        phone = getattr(seller, 'phone', None) or getattr(profile, 'phone', None)
        if not phone:
            logger.warning(
                "Shiprocket: seller %s has no phone number — cannot register pickup location. "
                "Seller must add a phone number in the Pickup Address settings.",
                seller.id,
            )
            return cls._fetch_active_pickup(profile, None, token, fallback) or fallback

        phone_clean = str(phone).replace('+91', '').replace(' ', '').replace('-', '').strip()[-10:]

        resp = requests.post(
            f"{SHIPROCKET_BASE}/settings/company/addpickup",
            json={
                "pickup_location": location_name,
                "name": seller.get_full_name() or seller.username,
                "email": seller.email or '',
                "phone": phone_clean,
                "address": getattr(profile, 'pickup_address', None) or getattr(profile, 'location_city', '') or '',
                "address_2": "",
                "city": getattr(profile, 'location_city', '') or '',
                "state": getattr(profile, 'location_state', '') or '',
                "country": "India",
                "pin_code": str(getattr(profile, 'location_pincode', '') or ''),
            },
            headers=cls._headers(token),
            timeout=20,
        )

        if resp.status_code in (200, 201):
            body = resp.json()
            registered = (body.get('data') or {}).get('pickup_location') or location_name
            logger.info("Shiprocket: addpickup accepted '%s' for seller %s — verifying status", registered, seller.id)

            # ── Step 3: confirm the newly registered location is active (status=1) ──
            # Shiprocket requires phone/OTP verification before a location becomes usable.
            # If status=2 (pending), do NOT cache it — fall through to any active location.
            active = cls._fetch_active_pickup(profile, registered, token, None)
            if active:
                logger.info("Shiprocket: pickup '%s' is active and ready for seller %s", active, seller.id)
                return active

            # Registered but still pending verification — warn and use any active location
            logger.warning(
                "Shiprocket: pickup '%s' registered for seller %s but status≠1 (pending verification). "
                "Seller must verify the phone number in Shiprocket dashboard. "
                "Using first available active location for this shipment.",
                registered, seller.id,
            )
            # Don't cache — retry registration/verification check on next shipment
            return cls._fetch_active_pickup(profile, None, token, fallback) or fallback

        # addpickup failed (422 = already exists, or other) — scan for active match
        logger.info(
            "addpickup returned %s for seller %s — scanning existing active locations",
            resp.status_code, seller.id,
        )
        return cls._fetch_active_pickup(profile, location_name, token, fallback) or fallback

    @classmethod
    def _cache_pickup_location(cls, profile, name: str):
        try:
            from sellers.models import SellerProfile
            SellerProfile.objects.filter(pk=profile.pk).update(shiprocket_pickup_location=name)
        except Exception as exc:
            logger.warning("Could not cache shiprocket_pickup_location: %s", exc)

    @classmethod
    def _fetch_active_pickup(cls, profile, desired_name, token: str, fallback) -> str:
        """
        Fetch all Shiprocket pickup locations and return the best *active* (status=1) one.

        Priority:
          1. Exact name match with status=1  → cache and return.
          2. Any location with status=1       → cache and return (no exact match found).
          3. None active                      → return fallback without caching.

        Pass desired_name=None to skip the exact-match step (i.e. just get any active).
        """
        try:
            resp = requests.get(
                f"{SHIPROCKET_BASE}/settings/company/pickup",
                headers=cls._headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                locations = resp.json().get('data', {}).get('shipping_address', [])
                # Only consider verified/active locations
                active = [loc for loc in locations if loc.get('status') == 1]

                if desired_name:
                    for loc in active:
                        if loc.get('pickup_location', '').lower() == desired_name.lower():
                            name = loc['pickup_location']
                            if profile is not None:
                                cls._cache_pickup_location(profile, name)
                            return name

                if active:
                    name = active[0].get('pickup_location', fallback)
                    if profile is not None:
                        cls._cache_pickup_location(profile, name)
                    logger.warning(
                        "Shiprocket: no exact active pickup match for '%s' — using first active '%s'",
                        desired_name, name,
                    )
                    return name

                logger.error("Shiprocket: no active (status=1) pickup locations found in account")
        except Exception as exc:
            logger.error("Could not fetch Shiprocket pickup list: %s", exc)
        return fallback

    # Keep old name as alias so any external callers don't break
    @classmethod
    def _fetch_existing_pickup(cls, profile, desired_name: str, token: str, fallback: str) -> str:
        return cls._fetch_active_pickup(profile, desired_name, token, fallback)

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
        token = cls.get_token()
        if not token:
            return None

        resp = requests.get(
            f"{SHIPROCKET_BASE}/courier/serviceability/",
            params={
                "pickup_postcode": str(origin_pincode),
                "delivery_postcode": str(destination_pincode),
                "weight": weight_kg,
                "cod": 0,
                "declared_value": order_value,
                "length": length,
                "breadth": breadth,
                "height": height,
            },
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            # Normalise to NimbusPost-compatible shape:
            # { status: True, data: [ { courier_id, courier_name, courier_charges, ... } ] }
            couriers = (
                body.get("data", {}).get("available_courier_companies") or []
            )
            normalised = [
                {
                    "courier_id": str(c.get("courier_company_id")),
                    "courier_name": c.get("courier_name"),
                    "courier_charges": c.get("rate") or c.get("freight_charge") or 0,
                    "etd": c.get("estimated_delivery_days"),
                }
                for c in couriers
            ]
            return {"status": True, "data": normalised}
        logger.error("Shiprocket serviceability failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Create Shipment ───────────────────────────────────────────────────────

    @classmethod
    def create_shipment(cls, payload: dict) -> dict | None:
        """
        Payload arrives in NimbusPost format from _build_shipment_payload().
        We translate it to Shiprocket's /orders/create/adhoc and then
        auto-assign a courier, returning a NimbusPost-compatible response.
        """
        token = cls.get_token()
        if not token:
            return None

        pickup = payload.get("pickup", {})
        products = payload.get("products", [])
        invoice = (payload.get("invoice") or [{}])[0]

        # Convert invoice_date from DD-MM-YYYY (NimbusPost format) to YYYY-MM-DD (Shiprocket format)
        raw_date = invoice.get("invoice_date", "")
        try:
            from datetime import datetime as _dt
            order_date = _dt.strptime(raw_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            from datetime import date as _date
            order_date = _date.today().strftime("%Y-%m-%d")

        # pickup_location is resolved by create_shipment_task via ensure_seller_pickup_location()
        # and stored in payload["pickup"]["warehouse_name"] before this method is called.
        pickup_location_name = pickup.get("warehouse_name") or "Primary"

        # Build Shiprocket order payload
        sr_payload = {
            "order_id": payload.get("order_id"),
            "order_date": order_date,
            "pickup_location": pickup_location_name,
            "billing_customer_name": payload.get("consignee_name", ""),
            "billing_last_name": "",
            "billing_address": payload.get("consignee_address", ""),
            "billing_city": payload.get("consignee_city", ""),
            "billing_pincode": payload.get("consignee_pincode", ""),
            "billing_state": payload.get("consignee_state", ""),
            "billing_country": "India",
            "billing_email": payload.get("consignee_email", ""),
            "billing_phone": payload.get("consignee_phone", ""),
            "shipping_is_billing": True,
            "order_items": [
                {
                    "name": p.get("product_name", "Plant"),
                    "sku": p.get("product_name", "SKU")[:40],
                    "units": int(p.get("no_of_box", 1)),
                    "selling_price": float(p.get("product_price", 0)) / max(int(p.get("no_of_box", 1)), 1),
                    "hsn": p.get("product_hsn_code", "0602"),
                }
                for p in products
            ],
            "payment_method": "Prepaid",
            "sub_total": float(invoice.get("invoice_value", 0)),
            "length": float((products[0] if products else {}).get("product_length", 10)),
            "breadth": float((products[0] if products else {}).get("product_breadth", 10)),
            "height": float((products[0] if products else {}).get("product_height", 10)),
            "weight": sum(
                float(p.get("product_weight", 500)) * int(p.get("no_of_box", 1))
                for p in products
            ) / 1000 or 0.5,
        }

        resp = requests.post(
            f"{SHIPROCKET_BASE}/orders/create/adhoc",
            json=sr_payload,
            headers=cls._headers(token),
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.error("Shiprocket create order failed: %s %s", resp.status_code, resp.text[:500])
            return None

        body = resp.json()
        order_id_sr = body.get("order_id")
        shipment_id_sr = body.get("shipment_id")

        if not shipment_id_sr:
            logger.error("Shiprocket: no shipment_id in response: %s", body)
            return None

        # Auto-assign cheapest courier
        courier_id = payload.get("courier_id")
        if not courier_id:
            svc = cls.check_serviceability(
                origin_pincode=pickup.get("pincode", "560001"),
                destination_pincode=payload.get("consignee_pincode", ""),
                weight_kg=sr_payload["weight"],
                order_value=sr_payload["sub_total"],
            )
            if svc and svc.get("data"):
                sorted_c = sorted(svc["data"], key=lambda c: float(c.get("courier_charges", 9999)))
                if sorted_c:
                    courier_id = sorted_c[0]["courier_id"]

        # Assign courier to generate AWB
        # shipment_id must be a plain string (not a list) per Shiprocket API docs
        awb = None
        courier_name = None
        if courier_id:
            assign_resp = requests.post(
                f"{SHIPROCKET_BASE}/courier/assign/awb",
                json={"shipment_id": str(shipment_id_sr), "courier_id": str(courier_id)},
                headers=cls._headers(token),
                timeout=20,
            )
            if assign_resp.status_code == 200:
                adata = assign_resp.json().get("response", {}).get("data", {})
                awb = adata.get("awb_code") or adata.get("awb")
                courier_name = adata.get("courier_name")

        # Request pickup — shipment_id must be a list of integers
        if awb:
            requests.post(
                f"{SHIPROCKET_BASE}/courier/generate/pickup",
                json={"shipment_id": [int(shipment_id_sr)]},
                headers=cls._headers(token),
                timeout=15,
            )

        # Generate label immediately after AWB assignment
        label_url = ""
        if awb and shipment_id_sr:
            label_resp = requests.post(
                f"{SHIPROCKET_BASE}/courier/generate/label",
                json={"shipment_id": [str(shipment_id_sr)]},
                headers=cls._headers(token),
                timeout=20,
            )
            if label_resp.status_code == 200:
                lbody = label_resp.json()
                label_url = (
                    lbody.get("label_url")
                    or (lbody.get("response") or {}).get("label_url")
                    or ""
                )
                if not label_url:
                    logger.warning("Shiprocket label not ready yet for shipment %s", shipment_id_sr)
            else:
                logger.warning(
                    "Shiprocket generate label returned %s for shipment %s: %s",
                    label_resp.status_code, shipment_id_sr, label_resp.text[:200],
                )

        return {
            "status": True,
            "data": {
                "shipment_id": str(shipment_id_sr),
                "order_id": str(order_id_sr),
                "awb_number": awb or "",
                "courier_name": courier_name or "",
                "status": "booked",
                "label": label_url,
                "manifest": "",
            },
        }

    # ── Track Shipment ────────────────────────────────────────────────────────

    @classmethod
    def track_shipment(cls, awb_number: str) -> dict | None:
        token = cls.get_token()
        if not token:
            return None

        resp = requests.get(
            f"{SHIPROCKET_BASE}/courier/track/awb/{awb_number}",
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            tracking = body.get("tracking_data") or {}
            return {"status": True, "data": tracking}
        logger.error("Shiprocket track failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Generate Label ────────────────────────────────────────────────────────

    @classmethod
    def generate_label(cls, awb_numbers: list[str]) -> dict | None:
        """Shiprocket labels are fetched by shipment_id, not AWB."""
        token = cls.get_token()
        if not token:
            return None

        from .models import Shipment
        shipment_ids = [
            int(sid) for sid in
            Shipment.objects.filter(awb_number__in=awb_numbers)
            .exclude(nimbuspost_id=None)
            .values_list("nimbuspost_id", flat=True)
            if sid
        ]
        if not shipment_ids:
            return {"status": False, "message": "No matching shipment IDs found"}

        resp = requests.post(
            f"{SHIPROCKET_BASE}/courier/generate/label",
            json={"shipment_id": shipment_ids},
            headers=cls._headers(token),
            timeout=20,
        )
        if resp.status_code == 200:
            body = resp.json()
            # API returns: {"label_created": 1, "label_url": "...", "response": "<string>"}
            label_url = body.get("label_url")
            return {"status": bool(label_url), "data": label_url}
        logger.error("Shiprocket generate label failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Generate Manifest ─────────────────────────────────────────────────────

    @classmethod
    def generate_manifest(cls, awb_numbers: list[str]) -> dict | None:
        token = cls.get_token()
        if not token:
            return None

        from .models import Shipment
        shipment_ids = [
            int(sid) for sid in
            Shipment.objects.filter(awb_number__in=awb_numbers)
            .exclude(nimbuspost_id=None)
            .values_list("nimbuspost_id", flat=True)
            if sid
        ]
        if not shipment_ids:
            return {"status": False, "message": "No matching shipment IDs found"}

        resp = requests.post(
            f"{SHIPROCKET_BASE}/manifests/generate",
            json={"shipment_id": shipment_ids},
            headers=cls._headers(token),
            timeout=20,
        )
        if resp.status_code == 200:
            body = resp.json()
            manifest_url = body.get("manifest_url")
            return {"status": bool(manifest_url), "data": manifest_url}
        logger.error("Shiprocket manifest failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Cancel Shipment ───────────────────────────────────────────────────────

    @classmethod
    def cancel_shipment(cls, awb_number: str) -> dict | None:
        token = cls.get_token()
        if not token:
            return None

        from .models import Shipment
        try:
            shipment = Shipment.objects.get(awb_number=awb_number)
            order_id_sr = shipment.nimbuspost_order_id  # reused field stores SR order ID
        except Shipment.DoesNotExist:
            return {"status": False, "message": "Shipment not found"}

        resp = requests.post(
            f"{SHIPROCKET_BASE}/orders/cancel",
            json={"ids": [order_id_sr]},
            headers=cls._headers(token),
            timeout=15,
        )
        if resp.status_code == 200:
            return {"status": True, "message": "Shipment cancelled"}
        logger.error("Shiprocket cancel failed: %s %s", resp.status_code, resp.text[:200])
        return None

    # ── Wallet Balance ────────────────────────────────────────────────────────

    @classmethod
    def get_wallet_balance(cls) -> dict | None:
        token = cls.get_token()
        if not token:
            return None

        resp = requests.get(
            f"{SHIPROCKET_BASE}/account/details/wallet-balance",
            headers=cls._headers(token),
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            return {"status": True, "data": body.get("data", body)}
        logger.error("Shiprocket wallet balance failed: %s %s", resp.status_code, resp.text[:200])
        return None


# ── Factory ───────────────────────────────────────────────────────────────────

def get_logistics_service():
    """
    Returns the active logistics service class (NimbuspostService or ShiprocketService)
    based on LogisticsProviderSettings. Falls back to NimbusPost if DB is unavailable.
    """
    from .models import LogisticsProviderSettings, LogisticsProvider
    try:
        provider = LogisticsProviderSettings.get_solo().active_provider
    except Exception:
        provider = LogisticsProvider.NIMBUSPOST

    if provider == LogisticsProvider.SHIPROCKET:
        return ShiprocketService
    return NimbuspostService
