from rest_framework import generics, permissions as drf_permissions
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
        all_orders = Order.objects.all().order_by('-created_at').values(
            'id', 'order_number', 'total_amount', 'status', 'created_at', 'guest_email', 'user__email'
        )
        
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
