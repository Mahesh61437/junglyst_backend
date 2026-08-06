from rest_framework import generics, permissions as drf_permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.cache import cache
from django.db.models import Sum, Count, F, Value, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from orders.models import Order, OrderItem
from core.models import User, Product
from core.permissions import IsAdminUser

class AdminDashboardView(generics.GenericAPIView):
    permission_classes = (IsAdminUser,)

    def get(self, request):
        orders = Order.objects.all()
        
        metrics = {
            "platform_stats": {
                "total_revenue": orders.aggregate(total=Sum('total_amount'))['total'] or 0,
                "total_orders": orders.count(),
                "total_sellers": User.objects.filter(role='grower').count(),
                "total_products": Product.objects.count(),
                "active_users": User.objects.filter(role='collector').count(),
            },
            "order_distribution": {
                "pending": orders.filter(status='pending').count(),
                "placed": orders.filter(status='placed').count(),
                "processing": orders.filter(status='processing').count(),
                "shipped": orders.filter(status='shipped').count(),
                "delivered": orders.filter(status='delivered').count(),
            },
            "recent_orders": orders.order_by('-created_at')[:10].values(
                'order_number', 'total_amount', 'status', 'created_at'
            )
        }
        
        return Response(metrics)

class SuperAdminDashboardView(generics.GenericAPIView):
    permission_classes = (IsAdminUser,)

    def get(self, request):
        now = timezone.now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Overall Analytics
        orders_this_month = Order.objects.filter(created_at__gte=start_of_month)
        total_revenue_month = orders_this_month.aggregate(total=Sum('total_amount'))['total'] or 0
        total_orders_month = orders_this_month.count()
        
        total_sellers = User.objects.filter(role__in=['grower', 'admin'], is_verified_seller=True).count()
        total_users = User.objects.exclude(role='admin').count()

        # Seller Wise Analytics — single query with annotations instead of N+1 loop
        sellers = User.objects.filter(role__in=['grower', 'admin']).select_related('seller_profile').annotate(
            seller_orders_count=Count('order_items__order', distinct=True),
            seller_revenue=Coalesce(Sum(F('order_items__unit_price') * F('order_items__quantity')), Value(0, output_field=DecimalField())),
        )
        sellers_data = []
        for seller in sellers:
            profile = getattr(seller, 'seller_profile', None)
            store_name = profile.store_name if profile else (seller.get_full_name() or seller.username)
            sellers_data.append({
                'id': seller.id,
                'name': seller.get_full_name() or seller.username,
                'store_name': store_name,
                'email': seller.email,
                'phone': seller.phone,
                'total_orders': seller.seller_orders_count,
                'total_revenue': seller.seller_revenue or 0,
                'is_verified': seller.is_verified_seller,
            })

        # Categorized Orders — prefetch payment and seller_profile to avoid N+1
        from orders.models import SubOrder
        all_sub_orders_qs = SubOrder.objects.select_related(
            'order', 'order__user', 'order__payment', 'seller', 'seller__seller_profile'
        ).all().order_by('-created_at')

        all_orders = []
        for so in all_sub_orders_qs:
            user = so.order.user
            payment = getattr(so.order, 'payment', None)
            pg = payment.gateway if payment else None
            profile = getattr(so.seller, 'seller_profile', None)
            seller_store = profile.store_name if profile else (so.seller.get_full_name() or so.seller.username)

            all_orders.append({
                'id': so.order.id,
                'sub_order_id': so.id,
                'order_number': so.sub_order_number,  # Use sub-order number
                'total_amount': so.seller_total,
                'status': so.status,
                'created_at': so.created_at,
                'payment_gateway': so.order.payment_gateway or pg,
                'guest_email': so.order.guest_email,
                'guest_phone': so.order.guest_phone,
                'user__email': user.email if user else None,
                'user__phone': user.phone if user else None,
                'seller_name': seller_store,
                'seller_contact': so.seller.phone or so.seller.email,
            })
        
        pending_orders = [o for o in all_orders if o['status'] in ['placed', 'confirmed', 'packing']]
        transit_orders = [o for o in all_orders if o['status'] in ['shipped', 'in_transit', 'out_for_delivery']]
        delivered_orders = [o for o in all_orders if o['status'] == 'delivered']

        metrics = {
            "overall_analytics": {
                "revenue_this_month": total_revenue_month,
                "orders_this_month": total_orders_month,
                "total_sellers": total_sellers,
                "total_users": total_users,
            },
            "sellers": sellers_data,
            "orders": {
                "pending": pending_orders,
                "transit": transit_orders,
                "delivered": delivered_orders
            }
        }
        
        return Response(metrics)

class GSTDashboardView(APIView):
    permission_classes = (IsAdminUser,)

    def get(self, request):
        month_str = request.query_params.get('month', None)
        if month_str:
            try:
                year, month = map(int, month_str.split('-'))
            except ValueError:
                year, month = timezone.now().year, timezone.now().month
        else:
            year, month = timezone.now().year, timezone.now().month

        items = OrderItem.objects.filter(
            order__created_at__year=year,
            order__created_at__month=month,
            order__status__in=['placed', 'processing', 'shipped', 'delivered']
        ).select_related('order', 'seller', 'variant')

        sellers_data = {}
        for item in items:
            seller_id = item.seller.id
            if seller_id not in sellers_data:
                try:
                    store_name = item.seller.seller_profile.store_name
                except Exception:
                    store_name = item.seller.get_full_name() or item.seller.username

                sellers_data[seller_id] = {
                    'seller_id': seller_id,
                    'store_name': store_name,
                    'seller_email': item.seller.email,
                    'orders_set': set(),
                    'gross_sales': 0,
                    'taxable_value': 0,
                    'total_gst': 0,
                    'platform_fee': 0,
                    'items': []
                }
            
            gross = float(item.unit_price) * item.quantity
            gst_rate = float(item.gst_percentage)
            comm_rate = float(item.variant.commission_rate) if item.variant else 10.0
            
            # reverse engineer base price
            factor = 1 + (gst_rate / 100) + (comm_rate / 100)
            base_price = gross / factor
            gst_amount = base_price * (gst_rate / 100)
            commission = base_price * (comm_rate / 100)

            sellers_data[seller_id]['gross_sales'] += gross
            sellers_data[seller_id]['taxable_value'] += base_price
            sellers_data[seller_id]['total_gst'] += gst_amount
            sellers_data[seller_id]['platform_fee'] += commission
            sellers_data[seller_id]['orders_set'].add(item.order.id)
            sellers_data[seller_id]['gst_percentage'] = gst_rate # representative
            
            sellers_data[seller_id]['items'].append({
                'order_id': item.order.order_number,
                'order_date': item.order.created_at.strftime('%d-%m-%Y'),
                'product_name': item.product_name,
                'qty': item.quantity,
                'gross_amount': round(gross, 2),
                'taxable_value': round(base_price, 2),
                'gst_rate': gst_rate,
                'cgst': round(gst_amount / 2, 2),
                'sgst': round(gst_amount / 2, 2),
                'total': round(gross, 2)
            })

        results = []
        for s in sellers_data.values():
            s['total_orders'] = len(s['orders_set'])
            del s['orders_set']
            
            s['cgst'] = s['total_gst'] / 2
            s['sgst'] = s['total_gst'] / 2
            s['platform_fee_gst'] = s['platform_fee'] * 0.18
            s['tcs_deducted'] = s['taxable_value'] * 0.01
            s['tds_deducted'] = s['gross_sales'] * 0.01
            
            s['net_settlement'] = s['gross_sales'] - s['platform_fee'] - s['platform_fee_gst'] - s['tcs_deducted'] - s['tds_deducted']
            
            # format numbers
            for key in ['gross_sales', 'taxable_value', 'total_gst', 'cgst', 'sgst', 'platform_fee', 'platform_fee_gst', 'tcs_deducted', 'tds_deducted', 'net_settlement']:
                s[key] = round(s[key], 2)

            results.append(s)

        return Response({"month": f"{year}-{month:02d}", "data": results})

class SellerGSTDashboardView(APIView):
    permission_classes = (drf_permissions.IsAuthenticated,)

    def get(self, request):
        user = request.user
        if user.role != 'grower' and user.role != 'admin':
            return Response({"error": "Unauthorized"}, status=403)
            
        month_str = request.query_params.get('month', None)
        if month_str:
            try:
                year, month = map(int, month_str.split('-'))
            except ValueError:
                year, month = timezone.now().year, timezone.now().month
        else:
            year, month = timezone.now().year, timezone.now().month

        items = OrderItem.objects.filter(
            seller=user,
            order__created_at__year=year,
            order__created_at__month=month,
            order__status__in=['placed', 'processing', 'shipped', 'delivered']
        ).select_related('order', 'variant')

        try:
            store_name = user.seller_profile.store_name
        except Exception:
            store_name = user.get_full_name() or user.username

        seller_data = {
            'seller_id': user.id,
            'store_name': store_name,
            'seller_email': user.email,
            'orders_set': set(),
            'gross_sales': 0,
            'taxable_value': 0,
            'total_gst': 0,
            'platform_fee': 0,
            'items': []
        }

        for item in items:
            gross = float(item.unit_price) * item.quantity
            gst_rate = float(item.gst_percentage)
            comm_rate = float(item.variant.commission_rate) if item.variant else 10.0
            
            factor = 1 + (gst_rate / 100) + (comm_rate / 100)
            base_price = gross / factor
            gst_amount = base_price * (gst_rate / 100)
            commission = base_price * (comm_rate / 100)

            seller_data['gross_sales'] += gross
            seller_data['taxable_value'] += base_price
            seller_data['total_gst'] += gst_amount
            seller_data['platform_fee'] += commission
            seller_data['orders_set'].add(item.order.id)
            seller_data['gst_percentage'] = gst_rate
            
            seller_data['items'].append({
                'order_id': item.order.order_number,
                'order_date': item.order.created_at.strftime('%d-%m-%Y'),
                'product_name': item.product_name,
                'qty': item.quantity,
                'gross_amount': round(gross, 2),
                'taxable_value': round(base_price, 2),
                'gst_rate': gst_rate,
                'cgst': round(gst_amount / 2, 2),
                'sgst': round(gst_amount / 2, 2),
                'total': round(gross, 2)
            })

        seller_data['total_orders'] = len(seller_data['orders_set'])
        del seller_data['orders_set']
        
        seller_data['cgst'] = seller_data['total_gst'] / 2
        seller_data['sgst'] = seller_data['total_gst'] / 2
        seller_data['platform_fee_gst'] = seller_data['platform_fee'] * 0.18
        seller_data['tcs_deducted'] = seller_data['taxable_value'] * 0.01
        seller_data['tds_deducted'] = seller_data['gross_sales'] * 0.01
        
        seller_data['net_settlement'] = seller_data['gross_sales'] - seller_data['platform_fee'] - seller_data['platform_fee_gst'] - seller_data['tcs_deducted'] - seller_data['tds_deducted']
        
        for key in ['gross_sales', 'taxable_value', 'total_gst', 'cgst', 'sgst', 'platform_fee', 'platform_fee_gst', 'tcs_deducted', 'tds_deducted', 'net_settlement']:
            seller_data[key] = round(seller_data[key], 2)

        return Response({"month": f"{year}-{month:02d}", "data": seller_data})

class AuthorizeGrowerView(APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request, pk):
        try:
            user = User.objects.get(id=pk)
            user.role = 'admin'
            user.is_verified_seller = True
            user.is_staff = True
            user.save()
            return Response({"message": "User authorized successfully. Role updated to admin."}, status=200)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

class RejectGrowerView(APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request, pk):
        try:
            user = User.objects.get(id=pk)
            user.role = 'collector'
            user.is_verified_seller = False
            user.save()
            return Response({"message": "Grower request rejected. Role updated to collector."}, status=200)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

class UserSearchView(APIView):
    permission_classes = (IsAdminUser,)

    def get(self, request):
        from django.db.models import Q
        from sellers.models import AllowedSeller, SellerProfile
        q = request.query_params.get('q', '').strip()
        if len(q) < 2:
            return Response([])
        users = User.objects.filter(
            Q(email__icontains=q) | Q(username__icontains=q) | Q(full_name__icontains=q)
        ).prefetch_related('seller_profile')[:10]
        result = []
        for u in users:
            is_allowed = AllowedSeller.objects.filter(email__iexact=u.email, is_active=True).exists()
            profile = getattr(u, 'seller_profile', None)
            result.append({
                'id': str(u.id),
                'email': u.email,
                'username': u.username,
                'full_name': getattr(u, 'full_name', ''),
                'role': u.role,
                'is_allowed': is_allowed,
                'store_name': profile.store_name if profile else None,
            })
        return Response(result)

class SetGrowerView(APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request, pk):
        from sellers.models import AllowedSeller, SellerProfile
        try:
            user = User.objects.get(id=pk)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)

        action = request.data.get('action', 'grant')

        if action == 'grant':
            AllowedSeller.objects.get_or_create(email=user.email, defaults={'is_active': True})
            user.role = 'grower'
            user.is_staff = True
            user.save(update_fields=['role', 'is_staff'])
            SellerProfile.objects.get_or_create(user=user)
            return Response({'message': f'{user.email} is now a grower', 'role': user.role})

        elif action == 'revoke':
            AllowedSeller.objects.filter(email__iexact=user.email).delete()
            user.role = 'collector'
            user.is_staff = False
            user.save(update_fields=['role', 'is_staff'])
            return Response({'message': f'{user.email} grower access revoked', 'role': user.role})

        return Response({'error': 'Invalid action'}, status=400)


class ClearCacheView(APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request):
        cache.clear()
        return Response({'message': 'Cache cleared successfully.'}, status=200)


# ---------------------------------------------------------------------------
# Stock & Price Sync
# ---------------------------------------------------------------------------

from decimal import Decimal

_SYNC_CACHE_PREFIX = 'stock_sync:pending:'

_LAST_SYNCED_KEY = 'stock_sync:last_synced:{source}'


def _get_last_synced(source: str) -> str | None:
    return cache.get(_LAST_SYNCED_KEY.format(source=source))


def _set_last_synced(source: str):
    from django.utils import timezone
    ts = timezone.now().strftime('%d %b %Y, %I:%M %p')
    cache.set(_LAST_SYNCED_KEY.format(source=source), ts, 60 * 60 * 24 * 30)


class StockSyncStatusView(APIView):
    """GET /analytics/super-admin/stock-sync/status/ — last synced timestamps."""
    permission_classes = (IsAdminUser,)

    def get(self, request):
        return Response({
            'petkadai': _get_last_synced('petkadai'),
            'himadri': _get_last_synced('himadri'),
        })


class StockSyncPreviewView(APIView):
    """
    POST /analytics/super-admin/stock-sync/preview/
    Body: {
      "source": "petkadai" | "himadri",
      "seller_commission": 14.0,
      "buyer_commission":  0.0
    }
    Kicks off a Celery scrape+diff task. Returns {job_id} immediately.
    Poll GET /analytics/super-admin/stock-sync/job/<job_id>/ for results.
    """
    permission_classes = (IsAdminUser,)

    def post(self, request):
        from core.tasks import scrape_and_sync

        source = request.data.get('source', '').lower()
        if source not in ('petkadai', 'himadri'):
            return Response({'error': 'source must be petkadai or himadri'}, status=400)

        seller_email = (request.data.get('seller_email') or '').strip()
        if not seller_email:
            return Response({'error': 'seller_email is required.'}, status=400)

        try:
            source_markup     = float(request.data['source_markup'])     if 'source_markup'     in request.data else None
            seller_commission = float(request.data['seller_commission']) if 'seller_commission' in request.data else None
            buyer_commission  = float(request.data['buyer_commission'])  if 'buyer_commission'  in request.data else None
        except Exception:
            return Response({'error': 'Commission values must be numbers.'}, status=400)

        task = scrape_and_sync.delay(source, seller_email, source_markup, seller_commission, buyer_commission)
        return Response({'job_id': task.id, 'source': source}, status=202)


class StockSyncJobView(APIView):
    """GET /analytics/super-admin/stock-sync/job/<job_id>/ — poll Celery task status."""
    permission_classes = (IsAdminUser,)

    def get(self, request, job_id):
        from celery.result import AsyncResult
        from django.db import close_old_connections
        from django.db.utils import OperationalError

        try:
            result = AsyncResult(job_id)
            state = result.state  # PENDING, STARTED, PROGRESS, SUCCESS, FAILURE

            if state in ('PENDING', 'STARTED'):
                return Response({'status': 'pending', 'progress': 'Starting…'})

            if state == 'PROGRESS':
                meta = result.info or {}
                return Response({'status': 'scraping', 'progress': meta.get('progress', '')})

            if state == 'SUCCESS':
                return Response({'status': 'done', **result.result})

            if state == 'FAILURE':
                return Response({'status': 'error', 'error': str(result.result)})

            return Response({'status': state.lower(), 'progress': ''})
        except OperationalError:
            # Transient DB connection drop (Railway's proxy silently kills idle
            # connections during long scrapes) — don't kill the whole sync over
            # one flaky poll. Discard the dead connection so the next request
            # opens a fresh one, and tell the frontend to just keep polling.
            close_old_connections()
            return Response({'status': 'pending', 'progress': 'Reconnecting…'})


class StockSyncApplyView(APIView):
    """
    POST /analytics/super-admin/stock-sync/apply/
    Body: {"session_key": "...", "approved_ids": ["variant-uuid", ...]}
    Applies approved price + commission changes.
    """
    permission_classes = (IsAdminUser,)

    def post(self, request):
        from core.models import ProductVariant
        from core.feed import invalidate_feed_cache

        session_key = request.data.get('session_key', '')
        approved_ids = request.data.get('approved_ids', [])

        if not session_key:
            return Response({'error': 'session_key is required'}, status=400)

        pending = cache.get(f'{_SYNC_CACHE_PREFIX}{session_key}')
        if pending is None:
            return Response({'error': 'Session expired or not found. Run preview again.'}, status=404)

        from core.models import User

        applied = 0
        errors = []

        # If the sync specified a commission override, update it on the seller profile
        # ONCE per seller (not per variant) — variant.save() reads from seller.seller_commission_rate.
        # Doing it per-variant would be redundant and risks partial updates.
        seller_commission_updated: set[int] = set()

        for vid in approved_ids:
            change = pending.get(vid)
            if not change:
                errors.append(f'{vid}: not in pending session')
                continue
            try:
                variant = ProductVariant.objects.select_related('product__seller').get(id=vid)
                variant.base_price = Decimal(str(change['new_base']))

                # Update the seller's commission rate if the sync specified one and it differs.
                # This is the field that ProductVariant.save() actually reads to compute price.
                sync_rate = change.get('seller_commission')
                if sync_rate is not None:
                    seller = variant.product.seller
                    new_rate = Decimal(str(sync_rate))
                    if seller.id not in seller_commission_updated and seller.seller_commission_rate != new_rate:
                        seller.seller_commission_rate = new_rate
                        seller.save(update_fields=['seller_commission_rate'])
                        seller_commission_updated.add(seller.id)

                variant.save()  # recomputes price = base_price * (1 + seller.seller_commission_rate/100)
                applied += 1
            except ProductVariant.DoesNotExist:
                errors.append(f'{vid}: variant not found')
            except Exception as e:
                errors.append(f'{vid}: {e}')

        if applied > 0:
            invalidate_feed_cache()
            cache.delete(f'{_SYNC_CACHE_PREFIX}{session_key}')

        return Response({'applied': applied, 'errors': errors})


class StockSyncImportView(APIView):
    """
    POST /analytics/super-admin/stock-sync/import/
    Body: { "session_key": "...", "approved_skus": ["SKU1", ...], "seller_email": "...", "seller_commission": 10 }
    Creates new Product + ProductVariant + ProductImage for each approved SKU.
    """
    permission_classes = (IsAdminUser,)

    def post(self, request):
        from analytics.sync_utils import import_new_products

        session_key = request.data.get('session_key', '').strip()
        approved_skus = request.data.get('approved_skus', [])
        seller_email = (request.data.get('seller_email') or '').strip()

        if not session_key:
            return Response({'error': 'session_key is required'}, status=400)
        if not seller_email:
            return Response({'error': 'seller_email is required'}, status=400)
        if not approved_skus:
            return Response({'error': 'No SKUs selected.'}, status=400)

        try:
            seller_commission = Decimal(str(request.data['seller_commission'])) if 'seller_commission' in request.data else None
        except Exception:
            return Response({'error': 'seller_commission must be a number.'}, status=400)

        result = import_new_products(session_key, approved_skus, seller_email, seller_commission)
        return Response(result)
