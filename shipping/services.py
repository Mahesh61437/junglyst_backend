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

    # ── Request Pickup ────────────────────────────────────────────────────────

    @classmethod
    def request_pickup(cls, awb_number: str, shipment_id: str | None = None) -> dict | None:
        """
        NimbusPost auto-pickups via the same /shipmentcargo/pickup endpoint when
        request_auto_pickup was 'No' at shipment creation time.
        """
        result = cls.generate_manifest([awb_number])
        if result and result.get("status"):
            return {"status": True, "data": result.get("data")}
        return result

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
    def _build_location_name(cls, seller, profile) -> str:
        """Clean Shiprocket-safe location name for this seller (alphanumeric + spaces, max 36)."""
        import re
        raw = profile.store_name or seller.get_full_name() or seller.username or 'Seller'
        name = re.sub(r'[^A-Za-z0-9 ]', '', raw).strip()[:36]
        return name or f"JL{str(seller.id).replace('-','')[:8].upper()}"

    @classmethod
    def _build_alt_location_name(cls, seller) -> str:
        """
        Short, unique alternate location name for OTP resend when primary name is 422-inactive.
        Format: JL + first 10 hex chars of seller UUID → e.g. 'JL86B09B68BD' (12 chars, well under 36).
        """
        return f"JL{str(seller.id).replace('-', '')[:10].upper()}"

    @classmethod
    def check_pickup_status(cls, seller, profile) -> dict:
        """
        Non-mutating status check — does NOT register, does NOT cache.
        Returns:
          { "status": "active"|"pending"|"no_address"|"auth_failed",
            "location_name": <str or None>,
            "message": <human-readable str> }
        """
        # Missing address fields → can't even attempt registration
        if not all([
            getattr(profile, 'pickup_address', None),
            getattr(profile, 'location_city', None),
            getattr(profile, 'location_state', None),
            getattr(profile, 'location_pincode', None),
            getattr(seller, 'phone', None) or getattr(profile, 'phone', None),
        ]):
            return {
                "status": "no_address",
                "location_name": None,
                "message": "Pickup address is incomplete. Fill in all fields in Settings → Pickup Address.",
            }

        # If the DB already has a verified location cached, trust it — it was written
        # only after Shiprocket confirmed status=1.
        cached_name = getattr(profile, 'shiprocket_pickup_location', None)
        if cached_name:
            return {
                "status": "active",
                "location_name": cached_name,
                "message": "Pickup location is verified and ready.",
            }

        token = cls.get_token()
        if not token:
            return {
                "status": "auth_failed",
                "location_name": None,
                "message": "Shiprocket authentication failed. Please try again later.",
            }

        # Search both the primary name and any alt name used during OTP registration
        primary_name = cls._build_location_name(seller, profile)
        alt_name = cls._build_alt_location_name(seller)
        names_to_check = list(dict.fromkeys([primary_name, alt_name]))  # deduplicated

        try:
            resp = requests.get(
                f"{SHIPROCKET_BASE}/settings/company/pickup",
                headers=cls._headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                locations = resp.json().get('data', {}).get('shipping_address', [])
                pending_loc = None
                for loc in locations:
                    if loc.get('pickup_location', '').lower() in [n.lower() for n in names_to_check]:
                        if loc.get('status') == 1:
                            # Found active — cache it so future calls are instant
                            cls._cache_pickup_location(profile, loc['pickup_location'])
                            return {
                                "status": "active",
                                "location_name": loc['pickup_location'],
                                "message": "Pickup location is verified and ready.",
                            }
                        else:
                            pending_loc = loc  # keep searching in case another name is active
                if pending_loc:
                    return {
                        "status": "pending",
                        "location_name": pending_loc.get('pickup_location'),
                        "message": "Pickup address registered but awaiting OTP verification.",
                    }
                # Not found in Shiprocket at all — needs registration
                return {
                    "status": "pending",
                    "location_name": None,
                    "message": "Pickup location not yet registered in Shiprocket.",
                }
        except Exception as exc:
            logger.error("check_pickup_status failed for seller %s: %s", seller.id, exc)

        return {
            "status": "auth_failed",
            "location_name": None,
            "message": "Could not reach Shiprocket. Please try again.",
        }

    @classmethod
    def ensure_seller_pickup_location(cls, seller, profile) -> str | None:
        """
        Returns the verified Shiprocket pickup_location name for this seller,
        or None if the seller's location is not yet active (pending OTP).

        NEVER falls back to a different seller's or admin's location.
        The caller must treat None as a hard failure — do not proceed with
        another location, as that would print the wrong FROM address on labels.

        Flow:
          1. Return cached value if present (cached only after status=1 confirmed).
          2. Check if seller's location already exists and is active (status=1).
          3. Register the seller's address via addpickup.
          4. Re-confirm status=1 after registration.
          5. If still pending OTP → return None.  Caller must abort the shipment.
        """
        cached = getattr(profile, 'shiprocket_pickup_location', None)
        if cached:
            return cached

        token = cls.get_token()
        if not token:
            logger.error("Shiprocket auth failed — cannot register pickup for seller %s", seller.id)
            return None

        location_name = cls._build_location_name(seller, profile)

        # ── Step 1: check whether the seller's own location is already active ──
        pre_check = cls._fetch_active_pickup(profile, location_name, token, None,
                                             cache_result=True, strict=True)
        if pre_check:
            logger.info("Shiprocket: reusing existing active pickup '%s' for seller %s", pre_check, seller.id)
            return pre_check

        # ── Step 2: register the seller's address ──
        phone = getattr(seller, 'phone', None) or getattr(profile, 'phone', None)
        if not phone:
            logger.warning(
                "Shiprocket: seller %s has no phone number — cannot register pickup location.",
                seller.id,
            )
            return None

        phone_clean = str(phone).replace('+91', '').replace(' ', '').replace('-', '').strip()[-10:]
        seller_display = seller.get_full_name() or seller.username

        resp = requests.post(
            f"{SHIPROCKET_BASE}/settings/company/addpickup",
            json={
                "pickup_location": location_name,
                "name": f"Junglyst | {seller_display}",
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
        else:
            # 422 = already exists — fall through to active check below
            logger.info("addpickup returned %s for seller %s — checking existing active status", resp.status_code, seller.id)
            registered = location_name

        # ── Step 3: confirm active (status=1) — strict, no fallback ──
        confirmed = cls._fetch_active_pickup(profile, registered, token, None,
                                             cache_result=True, strict=True)
        if confirmed:
            logger.info("Shiprocket: pickup '%s' is active and ready for seller %s", confirmed, seller.id)
            return confirmed

        # Pending OTP — return None; caller MUST abort the shipment
        logger.warning(
            "Shiprocket: pickup '%s' for seller %s is pending OTP verification. "
            "Seller must verify in Shiprocket dashboard (Settings → Manage Pickup Addresses). "
            "Shipment blocked until verified.",
            registered, seller.id,
        )
        return None

    # ── OTP Registration & Verification ──────────────────────────────────────

    @classmethod
    def send_pickup_otp(cls, seller, profile) -> dict:
        """
        Register / re-register the seller's pickup address in Shiprocket.
        Shiprocket automatically sends an OTP SMS to the seller's phone.
        Returns {"status": "sent"|"already_active"|"failed", "message": str,
                 "phone_hint": last-4-digits-of-phone}
        """
        cached = getattr(profile, 'shiprocket_pickup_location', None)
        if cached:
            return {
                "status": "already_active",
                "message": "Pickup location is already verified and active.",
                "phone_hint": "",
            }

        phone = getattr(seller, 'phone', None) or getattr(profile, 'phone', None)
        if not phone:
            return {"status": "failed", "message": "Phone number is required.", "phone_hint": ""}

        phone_clean = str(phone).replace('+91', '').replace(' ', '').replace('-', '').strip()[-10:]

        if not all([
            getattr(profile, 'pickup_address', None),
            getattr(profile, 'location_city', None),
            getattr(profile, 'location_state', None),
            getattr(profile, 'location_pincode', None),
        ]):
            return {"status": "failed", "message": "Complete your pickup address before sending OTP.", "phone_hint": ""}

        token = cls.get_token()
        if not token:
            return {"status": "failed", "message": "Shiprocket authentication failed. Try again.", "phone_hint": phone_clean[-4:]}

        location_name = cls._build_location_name(seller, profile)
        seller_display = seller.get_full_name() or seller.username

        resp = requests.post(
            f"{SHIPROCKET_BASE}/settings/company/addpickup",
            json={
                "pickup_location": location_name,
                "name": f"Junglyst | {seller_display}",
                "email": seller.email or '',
                "phone": phone_clean,
                "address": getattr(profile, 'pickup_address', None) or '',
                "address_2": "",
                "city": getattr(profile, 'location_city', '') or '',
                "state": getattr(profile, 'location_state', '') or '',
                "country": "India",
                "pin_code": str(getattr(profile, 'location_pincode', '') or ''),
            },
            headers=cls._headers(token),
            timeout=20,
        )

        phone_hint = phone_clean[-4:]
        logger.info(
            "Shiprocket addpickup → seller=%s location='%s' phone=...%s status=%s body=%s",
            seller.id, location_name, phone_hint, resp.status_code, resp.text[:300],
        )

        if resp.status_code in (200, 201):
            # Location created fresh — OTP sent to phone
            logger.info("Shiprocket: addpickup accepted for seller %s — OTP sent to ...%s", seller.id, phone_hint)
            cache.set(f"shiprocket_otp_name_{seller.id}", location_name, 3600)
            return {
                "status": "sent",
                "message": f"OTP sent to your phone ending in ...{phone_hint}. Enter it below to activate your pickup address.",
                "phone_hint": phone_hint,
            }

        elif resp.status_code == 422:
            # Shiprocket says this name already exists (inactive/pending).
            # Calling addpickup again with the SAME name does NOT trigger a new OTP.
            # Solution: register under a short unique alt name (≤36 chars) to get a fresh OTP.
            alt_name = cls._build_alt_location_name(seller)  # e.g. "JL86B09B68BD" — always ≤36 chars
            logger.info(
                "Shiprocket: primary name '%s' inactive for seller %s — trying alt name '%s'",
                location_name, seller.id, alt_name,
            )
            alt_payload = {
                "pickup_location": alt_name,
                "name": f"Junglyst | {seller_display}",
                "email": seller.email or '',
                "phone": phone_clean,
                "address": getattr(profile, 'pickup_address', None) or '',
                "address_2": "",
                "city": getattr(profile, 'location_city', '') or '',
                "state": getattr(profile, 'location_state', '') or '',
                "country": "India",
                "pin_code": str(getattr(profile, 'location_pincode', '') or ''),
            }
            alt_resp = requests.post(
                f"{SHIPROCKET_BASE}/settings/company/addpickup",
                json=alt_payload,
                headers=cls._headers(token),
                timeout=20,
            )
            logger.info(
                "Shiprocket addpickup alt → seller=%s alt='%s' status=%s body=%s",
                seller.id, alt_name, alt_resp.status_code, alt_resp.text[:300],
            )
            if alt_resp.status_code in (200, 201):
                cache.set(f"shiprocket_otp_name_{seller.id}", alt_name, 3600)
                logger.info("Shiprocket: alt addpickup accepted for seller %s — OTP sent to ...%s", seller.id, phone_hint)
                return {
                    "status": "sent",
                    "message": f"OTP sent to your phone ending in ...{phone_hint}. Enter it below to activate your pickup address.",
                    "phone_hint": phone_hint,
                }
            elif alt_resp.status_code == 422:
                try:
                    alt_body = alt_resp.json()
                    alt_msg = str(alt_body.get("message", ""))
                except Exception:
                    alt_msg = alt_resp.text[:200]
                # Distinguish "already exists" (can use original OTP) vs validation error
                if "already exists" in alt_msg.lower() or "inactive" in alt_msg.lower():
                    cache.set(f"shiprocket_otp_name_{seller.id}", alt_name, 3600)
                    logger.warning("Shiprocket: both primary and alt names exist inactive for seller %s", seller.id)
                    return {
                        "status": "sent",
                        "message": (
                            f"Your pickup address is already registered. Please check your SMS history — "
                            f"Shiprocket sent an OTP to ...{phone_hint} when you first saved your address. "
                            "Enter that OTP below to activate."
                        ),
                        "phone_hint": phone_hint,
                    }
                else:
                    logger.error("Shiprocket: alt addpickup 422 (validation) for seller %s: %s", seller.id, alt_msg)
                    return {"status": "failed", "message": f"Could not register pickup: {alt_msg}", "phone_hint": phone_hint}
            else:
                try:
                    msg = alt_resp.json().get("message", alt_resp.text[:150])
                except Exception:
                    msg = alt_resp.text[:150]
                return {"status": "failed", "message": f"Could not re-register pickup: {msg}", "phone_hint": phone_hint}

        else:
            try:
                msg = resp.json().get("message", resp.text[:150])
            except Exception:
                msg = resp.text[:150]
            logger.error("Shiprocket send_pickup_otp failed %s: %s", resp.status_code, msg)
            return {"status": "failed", "message": f"Could not register pickup: {msg}", "phone_hint": phone_hint}

    @classmethod
    def verify_pickup_otp(cls, seller, profile, otp: str) -> dict:
        """
        Verify the pickup location OTP with Shiprocket.
        Fetches the pickup_id for this seller's location then submits the OTP.
        On success, caches the verified location name.
        Returns {"status": "active"|"failed", "message": str, "location_name": str|None}
        """
        token = cls.get_token()
        if not token:
            return {"status": "failed", "message": "Shiprocket authentication failed. Try again.", "location_name": None}

        primary_name = cls._build_location_name(seller, profile)
        # Use the name that was actually registered for OTP (may be an alternate name if primary was 422)
        otp_reg_name = cache.get(f"shiprocket_otp_name_{seller.id}") or primary_name
        # Search both names — try otp_reg_name first, then primary_name as fallback
        names_to_check = list(dict.fromkeys([otp_reg_name, primary_name]))  # deduplicated, otp_reg first
        pickup_id = None
        pickup_found_name = None

        try:
            resp = requests.get(
                f"{SHIPROCKET_BASE}/settings/company/pickup",
                headers=cls._headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                locations = resp.json().get('data', {}).get('shipping_address', [])
                # First pass: check if ANY of our candidate names is already active
                for loc in locations:
                    loc_name_lower = loc.get('pickup_location', '').lower()
                    if loc_name_lower in [n.lower() for n in names_to_check]:
                        if loc.get('status') == 1:
                            cls._cache_pickup_location(profile, loc['pickup_location'])
                            return {
                                "status": "active",
                                "message": "Pickup location is already verified!",
                                "location_name": loc['pickup_location'],
                            }
                # Second pass: find the pending location to submit OTP against
                for name in names_to_check:
                    for loc in locations:
                        if loc.get('pickup_location', '').lower() == name.lower():
                            pickup_id = loc.get('id') or loc.get('pickup_location_id')
                            pickup_found_name = loc.get('pickup_location')
                            break
                    if pickup_id:
                        break
        except Exception as exc:
            logger.error("verify_pickup_otp: pickup list fetch failed: %s", exc)
            return {"status": "failed", "message": "Could not reach Shiprocket. Please try again.", "location_name": None}

        logger.info(
            "verify_pickup_otp: seller=%s names_checked=%s pickup_id=%s found_name='%s'",
            seller.id, names_to_check, pickup_id, pickup_found_name,
        )

        if not pickup_id:
            return {
                "status": "failed",
                "message": "Pickup location not found in Shiprocket. Please click 'Send OTP to My Phone' to register again.",
                "location_name": None,
            }

        verify_resp = requests.post(
            f"{SHIPROCKET_BASE}/settings/company/pickup/verify",
            json={"pickup_id": int(pickup_id), "otp": str(otp)},
            headers=cls._headers(token),
            timeout=15,
        )

        if verify_resp.status_code == 200:
            verified_name = pickup_found_name or primary_name
            cls._cache_pickup_location(profile, verified_name)
            # Clear the OTP registration cache since verification is complete
            cache.delete(f"shiprocket_otp_name_{seller.id}")
            logger.info("Shiprocket: pickup '%s' verified for seller %s", verified_name, seller.id)
            return {
                "status": "active",
                "message": "✅ Pickup location verified! Your address will now appear on all shipping labels.",
                "location_name": verified_name,
            }
        else:
            try:
                err_body = verify_resp.json()
                err_msg = err_body.get("message") or err_body.get("error") or f"Invalid OTP (code {verify_resp.status_code})."
            except Exception:
                err_msg = f"Invalid OTP (code {verify_resp.status_code}). Please check and try again."
            logger.warning("Shiprocket OTP verify failed %s: %s", verify_resp.status_code, err_msg)
            return {"status": "failed", "message": err_msg, "location_name": None}

    @classmethod
    def _cache_pickup_location(cls, profile, name: str):
        try:
            from sellers.models import SellerProfile
            SellerProfile.objects.filter(pk=profile.pk).update(shiprocket_pickup_location=name)
        except Exception as exc:
            logger.warning("Could not cache shiprocket_pickup_location: %s", exc)

    @classmethod
    def _fetch_active_pickup(cls, profile, desired_name, token: str, fallback,
                             cache_result: bool = True, strict: bool = False) -> str:
        """
        Fetch all Shiprocket pickup locations and return the best *active* (status=1) one.

        strict=True  → only return if desired_name is found active; never fall through
                        to a different location. Returns None (not fallback) on miss.
        strict=False → fall through to first active location if desired_name not found.
        cache_result → whether to persist the resolved name in SellerProfile.
                        Pass False when returning a temporary fallback so the seller's
                        own (pending-OTP) location is retried on the next shipment.
        """
        try:
            resp = requests.get(
                f"{SHIPROCKET_BASE}/settings/company/pickup",
                headers=cls._headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                locations = resp.json().get('data', {}).get('shipping_address', [])
                active = [loc for loc in locations if loc.get('status') == 1]

                if desired_name:
                    for loc in active:
                        if loc.get('pickup_location', '').lower() == desired_name.lower():
                            name = loc['pickup_location']
                            if cache_result and profile is not None:
                                cls._cache_pickup_location(profile, name)
                            return name
                    if strict:
                        # Desired location exists but is not active — don't fall through
                        return None

                if active:
                    name = active[0].get('pickup_location', fallback)
                    if cache_result and profile is not None:
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

        # Request pickup — shipment_id must be a list of integers.
        # Skipped at the Confirm step (request_auto_pickup="No"); the seller
        # triggers it later via the Ship Now action once they have packed
        # the box and uploaded the packaging photo.
        if awb and str(payload.get("request_auto_pickup", "Yes")).lower() == "yes":
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

    # ── Request Pickup ────────────────────────────────────────────────────────

    @classmethod
    def request_pickup(cls, awb_number: str, shipment_id: str | None = None) -> dict | None:
        """Ask Shiprocket to schedule the courier pickup for this shipment."""
        token = cls.get_token()
        if not token:
            return None

        sr_shipment_id = shipment_id
        if not sr_shipment_id:
            from .models import Shipment
            try:
                sr_shipment_id = Shipment.objects.get(awb_number=awb_number).nimbuspost_id
            except Shipment.DoesNotExist:
                return {"status": False, "message": "Shipment not found"}

        if not sr_shipment_id:
            return {"status": False, "message": "Missing shipment id"}

        resp = requests.post(
            f"{SHIPROCKET_BASE}/courier/generate/pickup",
            json={"shipment_id": [int(sr_shipment_id)]},
            headers=cls._headers(token),
            timeout=20,
        )
        if resp.status_code in (200, 201):
            body = resp.json()
            return {
                "status": True,
                "data": body.get("response") or body,
            }
        logger.error("Shiprocket request_pickup failed: %s %s", resp.status_code, resp.text[:300])
        return {"status": False, "message": resp.text[:300]}

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


def check_pincode_deliverable(pincode: str, origin_pincode: str | None = None) -> tuple[bool, str]:
    """
    Verify pincode serviceability against the active logistics provider API.
    Returns (is_deliverable: bool, message: str).

    Caches results for 6 hours to avoid repeated external API calls.
    Falls back to local zone classification if the external API is unavailable.
    """
    from django.core.cache import cache
    from django.conf import settings
    from .pincode_zones import classify_pincode

    if not pincode or not pincode.isdigit() or len(pincode) != 6:
        return False, "Invalid pincode format."

    svc = get_logistics_service()
    provider_name = svc.__name__
    cache_key = f"pincode_deliverable_v1_{provider_name}_{pincode}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached["deliverable"], cached["message"]

    origin = origin_pincode or getattr(settings, "DEFAULT_ORIGIN_PINCODE", "560001")

    try:
        result = svc.check_serviceability(
            origin_pincode=origin,
            destination_pincode=pincode,
            weight_kg=0.5,
            order_value=100,
        )
        if result is not None:
            couriers = result.get("data", [])
            deliverable = len(couriers) > 0
            message = "Delivery available." if deliverable else "Sorry, we don't deliver to your pincode yet."
            cache.set(cache_key, {"deliverable": deliverable, "message": message}, 60 * 60 * 6)
            return deliverable, message
    except Exception as exc:
        logger.warning("Logistics API pincode check failed for %s: %s", pincode, exc)

    # Fall back to local zone classifier
    zone_info = classify_pincode(pincode)
    return zone_info["deliverable"], zone_info.get("message", "")
