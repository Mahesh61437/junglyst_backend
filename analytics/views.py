from rest_framework import generics, permissions
from rest_framework.response import Response
from django.db.models import Sum, Count
from orders.models import Order, OrderItem
from core.models import User, Product

class AdminDashboardView(generics.GenericAPIView):
    permission_classes = (permissions.IsAdminUser,)

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
