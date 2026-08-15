from datetime import datetime, timedelta

from rest_framework import generics, permissions as drf_permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.cache import cache
from django.db.models import Sum, Count, F, Q, Value, DecimalField
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
        from orders.models import SubOrder, SubOrderStatus, OrderStatus, PaymentStatus

        rng = self._resolve_range(request)

        # ── Overall analytics ────────────────────────────────────────────────
        # Revenue counts only orders whose payment actually completed, and
        # excludes cancelled/returned orders. Summing every order (including
        # abandoned/failed checkouts) was inflating the figure.
        orders_in_range = self._apply_range(Order.objects.all(), rng, 'created_at')
        paid_orders = orders_in_range.filter(
            payment_status=PaymentStatus.COMPLETED
        ).exclude(status__in=[OrderStatus.CANCELLED, OrderStatus.RETURNED])

        total_revenue_month = paid_orders.aggregate(total=Sum('total_amount'))['total'] or 0
        total_orders_month = paid_orders.count()

        unpaid_orders = orders_in_range.exclude(payment_status=PaymentStatus.COMPLETED)
        unpaid_value = unpaid_orders.aggregate(total=Sum('total_amount'))['total'] or 0
        unpaid_count = unpaid_orders.count()

        total_sellers = User.objects.filter(role__in=['grower', 'admin'], is_verified_seller=True).count()
        total_users = User.objects.exclude(role='admin').count()

        # Seller Wise Analytics — single query with annotations instead of N+1 loop.
        # Same rule as above: realised revenue only, within the selected window.
        # Positive status list (rather than a negated Q) so the annotate filter
        # stays a plain JOIN condition across the multi-valued order_items rel.
        countable_statuses = [
            s for s in OrderStatus.values
            if s not in (OrderStatus.CANCELLED, OrderStatus.RETURNED)
        ]
        item_filter = (
            Q(order_items__order__payment_status=PaymentStatus.COMPLETED)
            & Q(order_items__order__status__in=countable_statuses)
        )
        if rng['start'] is not None:
            item_filter &= Q(order_items__order__created_at__gte=rng['start'])
        if rng['end'] is not None:
            item_filter &= Q(order_items__order__created_at__lte=rng['end'])

        sellers = User.objects.filter(role__in=['grower', 'admin']).select_related('seller_profile').annotate(
            seller_orders_count=Count('order_items__order', distinct=True, filter=item_filter),
            seller_revenue=Coalesce(
                Sum(F('order_items__unit_price') * F('order_items__quantity'), filter=item_filter),
                Value(0, output_field=DecimalField()),
            ),
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

        # Orders — every sub-order in the window, no status is filtered out.
        # Prefetch payment and seller_profile to avoid N+1.
        all_sub_orders_qs = self._apply_range(
            SubOrder.objects.select_related(
                'order', 'order__user', 'order__payment', 'seller', 'seller__seller_profile'
            ).all(),
            rng, 'created_at',
        ).order_by('-created_at')

        status_labels = dict(SubOrderStatus.choices)
        payment_labels = dict(PaymentStatus.choices)

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
                'master_order_number': so.order.order_number,
                'total_amount': so.seller_total,
                'status': so.status,
                'status_label': status_labels.get(so.status, so.status),
                'order_status': so.order.status,
                'payment_status': so.order.payment_status,
                'payment_status_label': payment_labels.get(so.order.payment_status, so.order.payment_status),
                'created_at': so.created_at,
                'payment_gateway': so.order.payment_gateway or pg,
                'guest_email': so.order.guest_email,
                'guest_phone': so.order.guest_phone,
                'user__email': user.email if user else None,
                'user__phone': user.phone if user else None,
                'seller_name': seller_store,
                'seller_contact': so.seller.phone or so.seller.email,
            })

        # Completed payments drive the main stream; everything else (pending,
        # failed, refunded) is surfaced separately so it never skews the view.
        paid_rows = [o for o in all_orders if o['payment_status'] == PaymentStatus.COMPLETED]
        unpaid_rows = [o for o in all_orders if o['payment_status'] != PaymentStatus.COMPLETED]

        status_counts = {value: 0 for value, _ in SubOrderStatus.choices}
        for o in paid_rows:
            status_counts[o['status']] = status_counts.get(o['status'], 0) + 1

        metrics = {
            "date_range": {
                "start": rng['start'],
                "end": rng['end'],
                "label": rng['label'],
            },
            "overall_analytics": {
                "revenue_this_month": total_revenue_month,
                "orders_this_month": total_orders_month,
                "total_sellers": total_sellers,
                "total_users": total_users,
                "unpaid_orders": unpaid_count,
                "unpaid_value": unpaid_value,
            },
            "sellers": sellers_data,
            "status_choices": [{"value": v, "label": l} for v, l in SubOrderStatus.choices],
            "status_counts": status_counts,
            "orders": {
                "all": paid_rows,
                "unpaid": unpaid_rows,
            },
        }

        return Response(metrics)

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_range(request):
        """Reporting window for the dashboard.

        Accepts ``?start=YYYY-MM-DD&end=YYYY-MM-DD`` or a named ``?period=`` of
        ``7d``, ``30d``, ``90d``, ``this_month``, ``last_month``, ``ytd``,
        ``all``. Defaults to the current month so the headline cards keep their
        original meaning when no filter is applied.
        """
        now = timezone.now()
        tz = timezone.get_current_timezone()
        params = request.query_params
        period = (params.get('period') or '').strip().lower()
        start_str, end_str = params.get('start'), params.get('end')

        def _aware(d, end_of_day=False):
            t = datetime.combine(d, datetime.max.time() if end_of_day else datetime.min.time())
            return timezone.make_aware(t, tz)

        if start_str and end_str:
            try:
                s = datetime.strptime(start_str, '%Y-%m-%d').date()
                e = datetime.strptime(end_str, '%Y-%m-%d').date()
                return {
                    'start': _aware(s), 'end': _aware(e, end_of_day=True),
                    'label': f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}",
                }
            except ValueError:
                pass  # malformed dates fall through to the default window

        if period == 'all':
            return {'start': None, 'end': None, 'label': 'All time'}
        if period == 'last_month':
            first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = first_this - timedelta(microseconds=1)
            start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return {'start': start, 'end': end, 'label': start.strftime('%B %Y')}
        if period == 'ytd':
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return {'start': start, 'end': now, 'label': f'YTD {now.year}'}
        if period in ('7d', '30d', '90d'):
            days = int(period[:-1])
            start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            return {'start': start, 'end': now, 'label': f'Last {days} days'}

        # 'this_month' and anything unrecognised
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {'start': start, 'end': now, 'label': now.strftime('%B %Y')}

    @staticmethod
    def _apply_range(qs, rng, field):
        if rng.get('start') is not None:
            qs = qs.filter(**{f'{field}__gte': rng['start']})
        if rng.get('end') is not None:
            qs = qs.filter(**{f'{field}__lte': rng['end']})
        return qs

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
