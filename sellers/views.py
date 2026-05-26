import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.db.models import Sum, Count, Q

logger = logging.getLogger(__name__)
from core.models import ProductVariant, Product
from orders.models import OrderItem
from .models import (
    SellerProfile, AllowedSeller, SellerShippingConfig,
    ShippingDefaultConfig, SellerBlackoutDate,
)
from .serializers import SellerProfileSerializer, SellerBlackoutDateSerializer
from .encryption import encrypt_field, decrypt_field, mask_account
from django.utils.text import slugify

class GrowerDashboardView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        seller = request.user
        if seller.role != 'grower' and seller.role != 'admin':
            return Response({"error": "Access denied"}, status=403)
            
        profile, _ = SellerProfile.objects.get_or_get_default(user=seller)
        order_items = OrderItem.objects.filter(seller=seller)
        
        # Calculate Advanced Metrics
        total_revenue = order_items.aggregate(total=Sum('unit_price'))['total'] or 0
        total_items_sold = order_items.aggregate(total=Sum('quantity'))['total'] or 0
        
        # Sales Chart Data (Last 14 days)
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models.functions import TruncDay
        
        fourteen_days_ago = timezone.now() - timedelta(days=14)
        sales_by_day = order_items.filter(order__created_at__gte=fourteen_days_ago)\
            .annotate(day=TruncDay('order__created_at'))\
            .values('day')\
            .annotate(revenue=Sum('unit_price'))\
            .order_by('day')
            
        sales_chart = []
        # Fill in gaps with zeros
        for i in range(15):
            day = (fourteen_days_ago + timedelta(days=i)).date()
            revenue = 0
            for entry in sales_by_day:
                if entry['day'].date() == day:
                    revenue = entry['revenue']
                    break
            sales_chart.append({"date": day.strftime('%b %d'), "revenue": revenue})

        # Top Products
        from django.db.models import Count
        top_products = order_items.values('product__name')\
            .annotate(total_qty=Sum('quantity'), total_rev=Sum('unit_price'))\
            .order_by('-total_qty')[:5]

        # Inventory Distribution — single query with conditional aggregation
        inv = ProductVariant.objects.filter(product__seller=seller).aggregate(
            out_of_stock=Count('id', filter=Q(stock=0)),
            low_stock=Count('id', filter=Q(stock__gt=0, stock__lt=5)),
            healthy_stock=Count('id', filter=Q(stock__gte=5)),
        )
        out_of_stock = inv['out_of_stock']
        low_stock = inv['low_stock']
        healthy_stock = inv['healthy_stock']

        metrics = {
            "total_revenue": total_revenue,
            "total_orders": order_items.values('order').distinct().count(),
            "total_items_sold": total_items_sold,
            "pending_orders": order_items.filter(order__status='pending').values('order').distinct().count(),
            "low_stock_variants": low_stock,
            "sales_chart": sales_chart,
            "top_products": top_products,
            "inventory_distribution": {
                "out_of_stock": out_of_stock,
                "low_stock": low_stock,
                "healthy": healthy_stock
            },
            "recent_activity": order_items.order_by('-order__created_at')[:8].values(
                'order__order_number', 'order__status', 'order__created_at', 'product__name', 'quantity'
            )
        }
        
        data = {
            "metrics": metrics,
            "profile": {
                "id": profile.id,
                "store_name": profile.store_name,
                "slug": profile.slug,
                "logo_url": profile.logo_url,
                "banner_url": profile.banner_url,
                "icon_url": profile.icon_url,
                "brand_color": profile.brand_color,
                "bio": profile.bio,
                "location_city": profile.location_city,
                "location_state": profile.location_state,
                "location_pincode": profile.location_pincode,
                "pickup_address": profile.pickup_address,
                "shiprocket_pickup_location": profile.shiprocket_pickup_location,
                "rating": str(profile.rating),
                "total_sales": str(profile.total_sales),
                "shipping_days": profile.shipping_days or [],
                "daily_cutoff_time": profile.daily_cutoff_time.strftime('%H:%M') if profile.daily_cutoff_time else '12:00',
                "next_shipping_date": (profile.get_next_shipping_date().isoformat()
                                       if profile.get_next_shipping_date() else None),
                "blackout_dates": [
                    {
                        "id": b.id,
                        "start_date": b.start_date.isoformat(),
                        "end_date": b.end_date.isoformat(),
                        "reason": b.reason or '',
                    }
                    for b in profile.blackout_dates.all()
                ],
            }
        }
        
        return Response(data)

    def post(self, request):
        seller = request.user
        
        # Check if the user is allowed to become a seller
        is_allowed = AllowedSeller.objects.filter(email=seller.email, is_active=True).exists() or \
                     (hasattr(seller, 'mobile') and AllowedSeller.objects.filter(phone=seller.mobile, is_active=True).exists())
        
        if not is_allowed and seller.role != 'admin':
            return Response({
                "error": "Access denied. Your credentials are not in our approved sellers list. Please contact admin for an invitation."
            }, status=403)

        if seller.role not in ['grower', 'admin', 'collector']:
            return Response({"error": "Access denied"}, status=403)
            
        profile, created = SellerProfile.objects.get_or_create(user=seller)
        
        data = request.data
        store_name = data.get('store_name')
        
        if store_name:
            # Check if store name is already taken by another user
            if SellerProfile.objects.filter(store_name=store_name).exclude(user=seller).exists():
                return Response({"error": "This store name is already taken. Please choose another."}, status=400)
            profile.store_name = store_name
            profile.slug = slugify(store_name)

        # Pickup-address fields (pickup_address / location_* / phone) are owned by
        # the dedicated /api/sellers/pickup-address/ endpoint. Treat them as an
        # optional partial update here: validate only what the caller explicitly
        # provided as a non-empty string, and never overwrite stored values with
        # null/empty payloads from the Studio Identity form.
        def _provided(key):
            val = data.get(key)
            return isinstance(val, str) and val.strip() != ""

        if _provided('pickup_address'):
            profile.pickup_address = data['pickup_address'].strip()

        if _provided('location_city'):
            profile.location_city = data['location_city'].strip()

        if _provided('location_state'):
            profile.location_state = data['location_state'].strip()

        if _provided('location_pincode'):
            pincode_clean = data['location_pincode'].strip()
            if not pincode_clean.isdigit() or len(pincode_clean) != 6:
                return Response({"error": "Pincode must be exactly 6 digits."}, status=400)
            profile.location_pincode = pincode_clean

        if _provided('phone'):
            new_phone = data['phone'].strip().replace('+91', '').replace(' ', '').replace('-', '')
            if not new_phone.isdigit() or len(new_phone) != 10:
                return Response({"error": "Phone number must be a valid 10-digit Indian mobile number."}, status=400)
            current_phone = (seller.phone or '').replace('+91', '').replace(' ', '').replace('-', '')
            if current_phone != new_phone:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                if User.objects.filter(phone=new_phone).exclude(pk=seller.pk).exists():
                    return Response({"error": "This phone number is already linked to another account."}, status=400)
                seller.phone = new_phone
                seller.save(update_fields=['phone'])

        profile.logo_url = data.get('logo_url', profile.logo_url)
        profile.icon_url = data.get('icon_url', profile.icon_url)
        profile.banner_url = data.get('banner_url', profile.banner_url)
        profile.brand_color = data.get('brand_color', profile.brand_color)
        profile.bio = data.get('bio', profile.bio)
        profile.tagline = data.get('expertise', profile.tagline)
        profile.gst_number = data.get('tax_id', profile.gst_number)
        profile.payout_type = data.get('payout_type', profile.payout_type)
        profile.payout_account = data.get('payout_account', profile.payout_account)
        profile.ifsc_code = data.get('ifsc_code', profile.ifsc_code)
        if 'shipping_days' in data:
            days = data.get('shipping_days')
            if isinstance(days, list):
                profile.shipping_days = [int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6]

        if 'daily_cutoff_time' in data:
            cutoff_raw = (data.get('daily_cutoff_time') or '').strip()
            from datetime import time as _time
            parsed = None
            for fmt in ('%H:%M', '%H:%M:%S'):
                try:
                    from datetime import datetime as _dt
                    parsed = _dt.strptime(cutoff_raw, fmt).time()
                    break
                except (ValueError, TypeError):
                    continue
            if parsed is None:
                return Response({"error": "daily_cutoff_time must be HH:MM (24-hour)."}, status=400)
            profile.daily_cutoff_time = parsed

        profile.save()

        # Upgrade user role and staff status
        if seller.role != 'admin':
            seller.role = 'grower'
            seller.is_staff = True
            seller.save(update_fields=['role', 'is_staff'])

        return Response({
            "message": "Store profile updated successfully",
            "user": {
                "id": str(seller.id),
                "role": seller.role,
                "is_staff": seller.is_staff,
                "username": seller.username
            }
        }, status=status.HTTP_200_OK)

class SellerStoreView(generics.RetrieveAPIView):
    permission_classes = (permissions.AllowAny,)
    
    def get(self, request, slug):
        try:
            profile = SellerProfile.objects.get(slug=slug)
            return Response({
                "store_name": profile.store_name,
                "logo_url": profile.logo_url,
                "banner_url": profile.banner_url,
                "icon_url": profile.icon_url,
                "brand_color": profile.brand_color,
                "bio": profile.bio,
                "location_city": profile.location_city,
                "rating": str(profile.rating),
                "total_sales": str(profile.total_sales),
                "created_at": profile.created_at,
                "expertise_tags": profile.expertise_tags,
                "infrastructure_details": profile.infrastructure_details,
                "experience_years": profile.experience_years,
                "identity_verified": profile.identity_verified,
                "tagline": profile.tagline
            })
        except SellerProfile.DoesNotExist:
            return Response({"error": "Store not found"}, status=404)

class SellerProfileListView(generics.ListAPIView):
    serializer_class = SellerProfileSerializer
    permission_classes = (permissions.AllowAny,)
    pagination_class = None 

    def get_queryset(self):
        queryset = SellerProfile.objects.select_related('user').filter(is_active=True).order_by('sort_order', '-rating')
        featured = self.request.query_params.get('featured')
        if featured == 'true':
            queryset = queryset.filter(is_featured=True)
        return queryset

class AllowedSellerListCreateView(generics.ListCreateAPIView):
    permission_classes = (permissions.IsAdminUser,)
    queryset = AllowedSeller.objects.all().order_by('-created_at')

    def get_serializer_class(self):
        from rest_framework import serializers
        class AllowedSellerSerializer(serializers.ModelSerializer):
            class Meta:
                model = AllowedSeller
                fields = '__all__'
        return AllowedSellerSerializer

    def perform_create(self, serializer):
        instance = serializer.save()
        # If a user with this email already exists and is not yet a grower,
        # upgrade them immediately so they don't have to re-login or re-apply.
        email = instance.email
        if email:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                user = User.objects.get(email__iexact=email)
                if user.role not in ['grower', 'admin']:
                    user.role = 'grower'
                    user.is_staff = True
                    user.save(update_fields=['role', 'is_staff'])
                    SellerProfile.objects.get_or_create(user=user)
            except User.DoesNotExist:
                pass

class AllowedSellerDestroyView(generics.DestroyAPIView):
    permission_classes = (permissions.IsAdminUser,)
    queryset = AllowedSeller.objects.all()

class CheckSellerApprovalView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        seller = request.user
        email = seller.email.strip() if seller.email else ""
        is_allowed = AllowedSeller.objects.filter(email__iexact=email, is_active=True).exists() or \
                     (hasattr(seller, 'mobile') and AllowedSeller.objects.filter(phone=seller.mobile, is_active=True).exists())
        return Response({
            "is_approved": is_allowed or seller.role in ['grower', 'admin'],
            "email_checked": email
        })

class CheckEmailAllowedView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip()
        if not email:
            return Response({'is_allowed': False})
        is_allowed = AllowedSeller.objects.filter(email__iexact=email, is_active=True).exists()
        return Response({'is_allowed': is_allowed})

class PlatformStatsView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # Single aggregation across all three counts
        stats = {
            'total_sellers': SellerProfile.objects.filter(is_active=True).count(),
            'total_products': Product.objects.filter(is_active=True).count(),
            'total_users': User.objects.filter(is_active=True).count(),
        }
        return Response(stats)

class VerifiedCuratorDirectoryView(generics.ListAPIView):
    """
    Dedicated endpoint for identity-verified curators.
    """
    serializer_class = SellerProfileSerializer
    permission_classes = (permissions.AllowAny,)
    pagination_class = None

    def get_queryset(self):
        # We can broaden this to all active curators or keep it strictly for identity_verified
        return SellerProfile.objects.filter(is_active=True).order_by('-rating', '-created_at')
class FeaturedCuratorView(generics.GenericAPIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = SellerProfileSerializer

    def get(self, request):
        featured = SellerProfile.objects.filter(is_active=True, is_featured=True).order_by('sort_order', '?').first()
        if not featured:
            return Response({"message": "No featured curators found"}, status=404)
        return Response(SellerProfileSerializer(featured).data)


class AdminSellerProfileEditView(generics.RetrieveUpdateAPIView):
    """
    Admin-only: view and edit any seller's profile fields.
    GET/PATCH /api/sellers/profiles/<id>/admin-edit/
    """
    permission_classes = (permissions.IsAdminUser,)
    serializer_class = SellerProfileSerializer
    queryset = SellerProfile.objects.all()

    def patch(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


class SellerPromotionView(generics.GenericAPIView):
    """
    Admin-only endpoint to toggle is_featured and set sort_order for a seller profile.
    PATCH /api/sellers/profiles/<id>/promote/
    Body: { "is_featured": true/false, "sort_order": 0 }
    """
    permission_classes = (permissions.IsAdminUser,)

    def patch(self, request, pk):
        try:
            profile = SellerProfile.objects.get(pk=pk)
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        if 'is_featured' in request.data:
            profile.is_featured = bool(request.data['is_featured'])
        if 'sort_order' in request.data:
            try:
                profile.sort_order = int(request.data['sort_order'])
            except (ValueError, TypeError):
                return Response({"error": "sort_order must be an integer"}, status=400)

        profile.save()
        return Response(SellerProfileSerializer(profile).data)


def _shiprocket_status_for_profile(seller, profile) -> dict:
    """
    Returns the seller's current Shiprocket pickup status dict.
    Calls ShiprocketService only if Shiprocket is the active provider.
    Falls back gracefully if Shiprocket is not configured.
    """
    try:
        from shipping.services import get_logistics_service
        svc = get_logistics_service()
        if hasattr(svc, 'check_pickup_status'):
            return svc.check_pickup_status(seller, profile)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("_shiprocket_status_for_profile failed: %s", exc)
    return {"status": "unknown", "location_name": None, "message": ""}


class SellerPickupAddressView(generics.GenericAPIView):
    """
    GET   /api/sellers/pickup-address/          — returns address + Shiprocket verification status
    PATCH /api/sellers/pickup-address/          — saves address, immediately registers with
                                                  Shiprocket, returns verification status
    POST  /api/sellers/pickup-address/register/ — re-attempts Shiprocket registration / status refresh
    """
    permission_classes = (permissions.IsAuthenticated,)

    def _require_grower(self, user):
        if user.role not in ('grower', 'admin'):
            return Response({"error": "Access denied"}, status=403)
        return None

    def _serialize(self, profile, user=None, shiprocket_status: dict | None = None):
        if shiprocket_status is None:
            shiprocket_status = _shiprocket_status_for_profile(user or profile, profile)
        return {
            "phone": (user.phone if user else "") or "",
            "pickup_address": profile.pickup_address or "",
            "location_city": profile.location_city or "",
            "location_state": profile.location_state or "",
            "location_pincode": profile.location_pincode or "",
            "shiprocket_pickup_location": profile.shiprocket_pickup_location or "",
            # Verification status surface to the frontend
            "shiprocket_status": shiprocket_status.get("status", "unknown"),
            "shiprocket_status_message": shiprocket_status.get("message", ""),
        }

    def get(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)
        return Response(self._serialize(profile, request.user))

    def patch(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        data = request.data

        # ── Validate required fields ──────────────────────────────────────────
        if not data.get('reset_shiprocket_location'):
            required = {
                'phone': 'Phone number',
                'pickup_address': 'Street address',
                'location_city': 'City',
                'location_state': 'State',
                'location_pincode': 'Pincode',
            }
            errors = {k: f"{label} is required." for k, label in required.items() if not (data.get(k) or "").strip()}
            if errors:
                return Response({"errors": errors}, status=400)

            pincode = data['location_pincode'].strip()
            if not pincode.isdigit() or len(pincode) != 6:
                return Response({"errors": {"location_pincode": "Pincode must be exactly 6 digits."}}, status=400)

            phone = data['phone'].strip().replace('+91', '').replace(' ', '').replace('-', '')
            if not phone.isdigit() or len(phone) != 10:
                return Response({"errors": {"phone": "Enter a valid 10-digit Indian mobile number."}}, status=400)

        profile_update_fields = []
        reset_cache = False

        # ── Phone → saved on User model ───────────────────────────────────────
        if 'phone' in data and not data.get('reset_shiprocket_location'):
            new_phone = data['phone'].strip().replace('+91', '').replace(' ', '').replace('-', '')
            current_phone = (request.user.phone or '').replace('+91', '').replace(' ', '').replace('-', '')
            if current_phone != new_phone:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                if User.objects.filter(phone=new_phone).exclude(pk=request.user.pk).exists():
                    return Response({"errors": {"phone": "This phone number is already linked to another account."}}, status=400)
                request.user.phone = new_phone
                request.user.save(update_fields=['phone'])
                reset_cache = True

        # ── Address fields → saved on SellerProfile ───────────────────────────
        address_field_map = {
            'pickup_address': 'pickup_address',
            'location_city': 'location_city',
            'location_state': 'location_state',
            'location_pincode': 'location_pincode',
        }
        for key, model_field in address_field_map.items():
            if key in data:
                new_val = (data[key] or "").strip()
                if (getattr(profile, model_field) or "") != new_val:
                    setattr(profile, model_field, new_val or None)
                    profile_update_fields.append(model_field)
                    reset_cache = True

        # ── Reset Shiprocket cache when address or phone changes ─────────────
        if reset_cache or data.get('reset_shiprocket_location'):
            profile.shiprocket_pickup_location = None
            profile_update_fields.append('shiprocket_pickup_location')

        if profile_update_fields:
            profile.save(update_fields=list(set(profile_update_fields)))

        # ── Immediately attempt Shiprocket registration after save ────────────
        # This registers the address in Shiprocket and sends the OTP to the seller's phone.
        # The response tells the seller whether to verify an OTP or if they're already active.
        sr_status = {"status": "unknown", "location_name": None, "message": ""}
        if not data.get('reset_shiprocket_location'):
            try:
                from shipping.services import get_logistics_service
                svc = get_logistics_service()
                if hasattr(svc, 'ensure_seller_pickup_location'):
                    location_name = svc.ensure_seller_pickup_location(request.user, profile)
                    # Re-fetch status so the response reflects the actual current state
                    if hasattr(svc, 'check_pickup_status'):
                        sr_status = svc.check_pickup_status(request.user, profile)
                    else:
                        sr_status = {
                            "status": "active" if location_name else "pending",
                            "location_name": location_name,
                            "message": "Pickup location ready." if location_name else (
                                "OTP sent to your phone. Verify in Shiprocket dashboard to activate."
                            ),
                        }
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Post-save Shiprocket registration failed: %s", exc)
                sr_status = {"status": "unknown", "location_name": None, "message": str(exc)}

        return Response({
            "message": "Pickup address saved." + (
                " Check your phone to verify the OTP in Shiprocket." if sr_status.get("status") == "pending" else
                " Your pickup location is active and ready." if sr_status.get("status") == "active" else ""
            ),
            **self._serialize(profile, request.user, shiprocket_status=sr_status),
        })

    def post(self, request):
        """
        POST /api/sellers/pickup-address/register/
        Re-attempts Shiprocket registration and returns the current verification status.
        Call this after the seller has verified the OTP to refresh the cached status.
        """
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        if not all([profile.pickup_address, profile.location_city, profile.location_state, profile.location_pincode]):
            return Response({"error": "Complete your pickup address before registering."}, status=400)

        sr_status = {"status": "unknown", "location_name": None, "message": ""}
        try:
            from shipping.services import get_logistics_service
            svc = get_logistics_service()
            if hasattr(svc, 'ensure_seller_pickup_location'):
                # Clears stale cache so we re-check Shiprocket fresh
                profile.shiprocket_pickup_location = None
                profile.save(update_fields=['shiprocket_pickup_location'])
                location_name = svc.ensure_seller_pickup_location(request.user, profile)
                if hasattr(svc, 'check_pickup_status'):
                    sr_status = svc.check_pickup_status(request.user, profile)
                else:
                    sr_status = {
                        "status": "active" if location_name else "pending",
                        "location_name": location_name,
                        "message": "Active." if location_name else "Still pending OTP verification.",
                    }
            else:
                sr_status = {"status": "not_applicable", "location_name": None,
                             "message": "Shiprocket is not the active logistics provider."}
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("Shiprocket registration refresh failed for seller %s: %s", request.user.id, exc)
            sr_status = {"status": "unknown", "location_name": None, "message": str(exc)}

        profile.refresh_from_db(fields=['shiprocket_pickup_location'])
        return Response({**self._serialize(profile, request.user, shiprocket_status=sr_status)})


class SellerPickupOTPView(generics.GenericAPIView):
    """
    POST  /api/sellers/pickup-address/otp/  — register address in Shiprocket (sends OTP SMS)
    PATCH /api/sellers/pickup-address/otp/  — verify the 6-digit OTP entered by the seller
    """
    permission_classes = (permissions.IsAuthenticated,)

    def _require_grower(self, user):
        if user.role not in ('grower', 'admin'):
            return Response({"error": "Access denied"}, status=403)
        return None

    def post(self, request):
        """Send (or resend) the Shiprocket pickup verification OTP."""
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        try:
            from shipping.services import get_logistics_service
            svc = get_logistics_service()
            if not hasattr(svc, 'send_pickup_otp'):
                return Response({"status": "not_applicable", "message": "OTP not required for your logistics provider.", "phone_hint": ""})
            result = svc.send_pickup_otp(request.user, profile)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("send_pickup_otp error for seller %s: %s", request.user.id, exc)
            return Response({"status": "failed", "message": str(exc), "phone_hint": ""}, status=500)

        http_status = 200 if result.get("status") in ("sent", "already_active") else 400
        return Response(result, status=http_status)

    def patch(self, request):
        """Verify the OTP the seller received via SMS."""
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        otp = (request.data.get('otp') or '').strip()
        if not otp:
            return Response({"error": "OTP is required."}, status=400)

        try:
            from shipping.services import get_logistics_service
            svc = get_logistics_service()
            if not hasattr(svc, 'verify_pickup_otp'):
                return Response({"status": "not_applicable", "message": "OTP verification not required for your logistics provider.", "location_name": None})
            result = svc.verify_pickup_otp(request.user, profile, otp)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("verify_pickup_otp error for seller %s: %s", request.user.id, exc)
            return Response({"status": "failed", "message": str(exc), "location_name": None}, status=500)

        # Re-read cached location after potential update
        profile.refresh_from_db(fields=['shiprocket_pickup_location'])

        if result.get("status") == "active":
            sr_status = {"status": "active", "location_name": result.get("location_name"), "message": result.get("message", "")}
        else:
            sr_status = _shiprocket_status_for_profile(request.user, profile)

        # Return merged response: OTP result + serialized pickup address
        view = SellerPickupAddressView()
        serialized = view._serialize(profile, request.user, shiprocket_status=sr_status)
        return Response({**result, **serialized}, status=200 if result.get("status") == "active" else 400)


class BankDetailsView(generics.GenericAPIView):
    """
    GET  /api/sellers/bank-details/  — returns masked payout info (no raw account numbers)
    POST /api/sellers/bank-details/  — encrypts and saves payout details
    """
    permission_classes = (permissions.IsAuthenticated,)

    def _require_grower(self, user):
        if user.role not in ('grower', 'admin'):
            return Response({"error": "Access denied"}, status=403)
        return None

    def get(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        payout_type = profile.payout_type or 'upi'
        raw_account = decrypt_field(profile.payout_account or '')
        raw_ifsc = decrypt_field(profile.ifsc_code or '')

        return Response({
            "payout_type": payout_type,
            "account_holder_name": profile.account_holder_name or '',
            "payout_account_masked": mask_account(raw_account) if raw_account else '',
            "ifsc_code_masked": raw_ifsc[:4] + '****' if len(raw_ifsc) > 4 else ('****' if raw_ifsc else ''),
            "has_bank_details": bool(raw_account),
        })

    def post(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        try:
            profile = request.user.seller_profile
        except SellerProfile.DoesNotExist:
            return Response({"error": "Seller profile not found"}, status=404)

        data = request.data
        payout_type = data.get('payout_type', profile.payout_type or 'upi')

        if payout_type not in ('upi', 'bank'):
            return Response({"error": "payout_type must be 'upi' or 'bank'"}, status=400)

        payout_account = data.get('payout_account', '').strip()
        ifsc_code = data.get('ifsc_code', '').strip()
        account_holder_name = data.get('account_holder_name', '').strip()

        if not payout_account:
            return Response({"error": "payout_account is required"}, status=400)

        if payout_type == 'bank':
            if not ifsc_code:
                return Response({"error": "ifsc_code is required for bank transfers"}, status=400)
            if len(ifsc_code) != 11:
                return Response({"error": "IFSC code must be exactly 11 characters"}, status=400)
            if not account_holder_name:
                return Response({"error": "account_holder_name is required for bank transfers"}, status=400)

        profile.payout_type = payout_type
        profile.payout_account = encrypt_field(payout_account)
        profile.ifsc_code = encrypt_field(ifsc_code) if ifsc_code else ''
        profile.account_holder_name = account_holder_name
        profile.save(update_fields=['payout_type', 'payout_account', 'ifsc_code', 'account_holder_name'])

        raw_account = payout_account
        raw_ifsc = ifsc_code
        return Response({
            "message": "Payout details saved securely",
            "payout_type": payout_type,
            "account_holder_name": account_holder_name,
            "payout_account_masked": mask_account(raw_account),
            "ifsc_code_masked": raw_ifsc[:4] + '****' if len(raw_ifsc) > 4 else ('****' if raw_ifsc else ''),
        }, status=status.HTTP_200_OK)


def _config_to_dict(cfg):
    try:
        store = cfg.seller.seller_profile.store_name
    except Exception:
        store = ''
    return {
        'id': cfg.id,
        'seller_id': str(cfg.seller_id),
        'store_name': store,
        'item_category': cfg.item_category,
        'tier1_max': float(cfg.tier1_max),
        'tier1_fee': float(cfg.tier1_fee),
        'tier2_max': float(cfg.tier2_max),
        'tier2_fee': float(cfg.tier2_fee),
        'show_nudge_products': cfg.show_nudge_products,
    }


class SellerShippingConfigListCreateView(generics.GenericAPIView):
    """Superadmin: list all shipping configs or create/upsert one."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        configs = SellerShippingConfig.objects.select_related(
            'seller__seller_profile'
        ).order_by('seller__seller_profile__store_name', 'item_category')
        return Response([_config_to_dict(c) for c in configs])

    def post(self, request):
        d = request.data
        required = ['seller_id', 'item_category', 'tier1_max', 'tier1_fee', 'tier2_max', 'tier2_fee']
        missing = [f for f in required if f not in d]
        if missing:
            return Response({'error': f'Missing fields: {missing}'}, status=400)
        try:
            cfg, created = SellerShippingConfig.objects.update_or_create(
                seller_id=d['seller_id'],
                item_category=d['item_category'],
                defaults={
                    'tier1_max': d['tier1_max'],
                    'tier1_fee': d['tier1_fee'],
                    'tier2_max': d['tier2_max'],
                    'tier2_fee': d['tier2_fee'],
                    'show_nudge_products': d.get('show_nudge_products', False),
                }
            )
            cfg.refresh_from_db()
            cfg.seller  # trigger select_related manually
            return Response(_config_to_dict(cfg), status=201 if created else 200)
        except Exception:
            logger.exception("Failed to upsert shipping config: data=%s", request.data)
            return Response({'error': 'Failed to save shipping config. Check server logs.'}, status=400)


class ShippingDefaultConfigView(generics.GenericAPIView):
    """
    Superadmin: read or update platform-wide default shipping tier values.
    GET  /sellers/shipping-configs/defaults/  → { light: {...}, heavy: {...} }
    PATCH /sellers/shipping-configs/defaults/ → body: { item_category, tier1_max, tier1_fee, tier2_max, tier2_fee }
    """
    permission_classes = [permissions.IsAdminUser]

    def _all_as_dict(self):
        return {
            d.item_category: {
                'tier1_max': float(d.tier1_max),
                'tier1_fee': float(d.tier1_fee),
                'tier2_max': float(d.tier2_max),
                'tier2_fee': float(d.tier2_fee),
            }
            for d in ShippingDefaultConfig.objects.all()
        }

    def get(self, request):
        return Response(self._all_as_dict())

    def patch(self, request):
        category = request.data.get('item_category')
        if category not in ('light', 'heavy'):
            return Response({'error': "item_category must be 'light' or 'heavy'"}, status=400)
        cfg, _ = ShippingDefaultConfig.objects.get_or_create(
            item_category=category,
            defaults={'tier1_max': 0, 'tier1_fee': 0, 'tier2_max': 0, 'tier2_fee': 0},
        )
        for field in ['tier1_max', 'tier1_fee', 'tier2_max', 'tier2_fee']:
            if field in request.data:
                setattr(cfg, field, request.data[field])
        try:
            cfg.save()
        except Exception:
            logger.exception("Failed to save ShippingDefaultConfig: data=%s", request.data)
            return Response({'error': 'Failed to save default shipping config. Check server logs.'}, status=400)
        return Response(self._all_as_dict())


class SellerShippingConfigDetailView(generics.GenericAPIView):
    """Superadmin: update or delete a single shipping config."""
    permission_classes = [permissions.IsAdminUser]

    def _get(self, pk):
        try:
            return SellerShippingConfig.objects.select_related(
                'seller__seller_profile'
            ).get(id=pk)
        except SellerShippingConfig.DoesNotExist:
            return None

    def patch(self, request, pk):
        cfg = self._get(pk)
        if not cfg:
            return Response({'error': 'Not found'}, status=404)
        d = request.data
        for field in ['tier1_max', 'tier1_fee', 'tier2_max', 'tier2_fee', 'show_nudge_products']:
            if field in d:
                setattr(cfg, field, d[field])
        cfg.save()
        return Response(_config_to_dict(cfg))

    def delete(self, request, pk):
        cfg = self._get(pk)
        if not cfg:
            return Response({'error': 'Not found'}, status=404)
        cfg.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Blackout dates (vacation / OOO) — seller self-serve ──────────────────────

class SellerBlackoutListCreateView(generics.GenericAPIView):
    """
    GET  /api/sellers/blackouts/  — list current seller's upcoming blackouts
    POST /api/sellers/blackouts/  — create a blackout range
        body: { start_date: 'YYYY-MM-DD', end_date: 'YYYY-MM-DD', reason?: str }
    """
    permission_classes = (permissions.IsAuthenticated,)

    def _require_grower(self, user):
        if user.role not in ('grower', 'admin'):
            return Response({"error": "Access denied"}, status=403)
        return None

    def _profile(self, user):
        try:
            return user.seller_profile
        except SellerProfile.DoesNotExist:
            return None

    def get(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        profile = self._profile(request.user)
        if not profile:
            return Response({"error": "Seller profile not found"}, status=404)
        # Hide past blackouts to keep the list focused
        from django.utils import timezone as _tz
        today = _tz.localdate()
        blackouts = profile.blackout_dates.filter(end_date__gte=today).order_by('start_date')
        return Response(SellerBlackoutDateSerializer(blackouts, many=True).data)

    def post(self, request):
        denied = self._require_grower(request.user)
        if denied:
            return denied
        profile = self._profile(request.user)
        if not profile:
            return Response({"error": "Seller profile not found"}, status=404)
        serializer = SellerBlackoutDateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(seller=profile)
        return Response(SellerBlackoutDateSerializer(instance).data, status=201)


class SellerBlackoutDestroyView(generics.GenericAPIView):
    """DELETE /api/sellers/blackouts/<pk>/ — remove a blackout range."""
    permission_classes = (permissions.IsAuthenticated,)

    def delete(self, request, pk):
        if request.user.role not in ('grower', 'admin'):
            return Response({"error": "Access denied"}, status=403)
        try:
            profile = request.user.seller_profile
            blackout = SellerBlackoutDate.objects.get(pk=pk, seller=profile)
        except (SellerProfile.DoesNotExist, SellerBlackoutDate.DoesNotExist):
            return Response({"error": "Blackout not found"}, status=404)
        blackout.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
