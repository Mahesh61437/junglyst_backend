from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.db.models import Sum, Count, Q
from core.models import ProductVariant, Product
from orders.models import OrderItem
from .models import SellerProfile, AllowedSeller
from .serializers import SellerProfileSerializer
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
                "location_pincode": profile.location_pincode,
                "rating": str(profile.rating),
                "total_sales": str(profile.total_sales),
                "shipping_days": profile.shipping_days or []
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
                "error": "Access denied. Your credentials are not in our master curator registry. Please contact admin for invitation."
            }, status=403)

        if seller.role not in ['grower', 'admin', 'collector']:
            return Response({"error": "Access denied"}, status=403)
            
        profile, created = SellerProfile.objects.get_or_create(user=seller)
        
        data = request.data
        store_name = data.get('store_name')
        
        if store_name:
            # Check if store name is already taken by another user
            if SellerProfile.objects.filter(store_name=store_name).exclude(user=seller).exists():
                return Response({"error": "This studio name is already reserved in our sanctuary. Please choose another."}, status=400)
            profile.store_name = store_name
            profile.slug = slugify(store_name)
            
        profile.logo_url = data.get('logo_url', profile.logo_url)
        profile.icon_url = data.get('icon_url', profile.icon_url)
        profile.banner_url = data.get('banner_url', profile.banner_url)
        profile.brand_color = data.get('brand_color', profile.brand_color)
        profile.bio = data.get('bio', profile.bio)
        profile.tagline = data.get('expertise', profile.tagline)
        profile.location_city = data.get('location_city', profile.location_city)
        profile.location_pincode = data.get('location_pincode', profile.location_pincode)
        profile.gst_number = data.get('tax_id', profile.gst_number)
        profile.payout_type = data.get('payout_type', profile.payout_type)
        profile.payout_account = data.get('payout_account', profile.payout_account)
        profile.ifsc_code = data.get('ifsc_code', profile.ifsc_code)
        if 'shipping_days' in data:
            days = data.get('shipping_days')
            if isinstance(days, list):
                profile.shipping_days = [int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6]

        profile.save()

        # Upgrade user role and staff status
        if seller.role != 'admin':
            seller.role = 'grower'
            seller.is_staff = True
            seller.save(update_fields=['role', 'is_staff'])

        return Response({
            "message": "Sanctuary Identity updated successfully",
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
