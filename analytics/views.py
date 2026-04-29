from rest_framework import generics, permissions as drf_permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Sum, Count, F
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
        
        total_sellers = User.objects.filter(role='grower').count()
        total_users = User.objects.exclude(role='admin').count()

        # Seller Wise Analytics
        sellers = User.objects.filter(role='grower')
        sellers_data = []
        for seller in sellers:
            # Orders containing items from this seller
            seller_items = OrderItem.objects.filter(seller=seller)
            seller_orders_count = seller_items.values('order').distinct().count()
            # seller revenue = sum of (unit_price * quantity) + gst? Let's just sum unit_price * quantity
            seller_revenue = seller_items.aggregate(
                total=Sum(F('unit_price') * F('quantity'))
            )['total'] or 0

            try:
                store_name = seller.seller_profile.store_name
            except Exception:
                store_name = seller.get_full_name() or seller.username

            sellers_data.append({
                'id': seller.id,
                'name': seller.get_full_name() or seller.username,
                'store_name': store_name,
                'email': seller.email,
                'phone': seller.phone,
                'total_orders': seller_orders_count,
                'total_revenue': seller_revenue,
                'is_verified': seller.is_verified_seller,
            })

        # Categorized Orders
        all_orders_qs = Order.objects.prefetch_related('items__seller').all().order_by('-created_at')
        
        all_orders = []
        for o in all_orders_qs:
            sellers_in_order = list({item.seller for item in o.items.all() if item.seller})
            if sellers_in_order:
                seller_name = ", ".join([s.get_full_name() or s.username for s in sellers_in_order])
                # Prefer phone, fallback to email
                contacts = []
                for s in sellers_in_order:
                    contacts.append(s.phone if s.phone else s.email)
                seller_contact = ", ".join(contacts)
            else:
                seller_name = "N/A"
                seller_contact = "N/A"

            all_orders.append({
                'id': o.id,
                'order_number': o.order_number,
                'total_amount': o.total_amount,
                'status': o.status,
                'created_at': o.created_at,
                'guest_email': o.guest_email,
                'guest_phone': o.guest_phone,
                'user__email': o.user.email if o.user else None,
                'user__phone': o.user.phone if o.user else None,
                'seller_name': seller_name,
                'seller_contact': seller_contact,
            })
        
        pending_orders = [o for o in all_orders if o['status'] in ['pending', 'placed', 'processing']]
        transit_orders = [o for o in all_orders if o['status'] == 'shipped']
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


