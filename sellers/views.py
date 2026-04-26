from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.db.models import Sum
from core.models import ProductVariant, Product
from orders.models import OrderItem
from .models import SellerProfile
from .serializers import SellerProfileSerializer
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

        # Inventory Distribution
        out_of_stock = ProductVariant.objects.filter(product__seller=seller, stock=0).count()
        low_stock = ProductVariant.objects.filter(product__seller=seller, stock__gt=0, stock__lt=5).count()
        healthy_stock = ProductVariant.objects.filter(product__seller=seller, stock__gte=5).count()

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
                "brand_color": profile.brand_color,
                "bio": profile.bio,
                "location_city": profile.location_city,
                "location_pincode": profile.location_pincode,
                "rating": str(profile.rating),
                "total_sales": str(profile.total_sales)
            }
        }
        
        return Response(data)

    def post(self, request):
        seller = request.user
        # Allow collectors to upgrade during onboarding
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
        profile.banner_url = data.get('banner_url', profile.banner_url)
        profile.brand_color = data.get('brand_color', profile.brand_color)
        profile.bio = data.get('bio', profile.bio)
        profile.location_city = data.get('location_city', profile.location_city)
        profile.location_pincode = data.get('location_pincode', profile.location_pincode)
        
        profile.save()

        # Upgrade user role if they were a collector
        if seller.role == 'collector':
            seller.role = 'grower'
            seller.save()
        
        return Response({
            "message": "Sanctuary Identity updated successfully",
            "user": {
                "id": seller.id,
                "role": seller.role,
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
                "brand_color": profile.brand_color,
                "bio": profile.bio,
                "location_city": profile.location_city,
                "rating": str(profile.rating),
                "total_sales": str(profile.total_sales),
                "created_at": profile.created_at,
                "expertise_tags": profile.expertise_tags,
                "infrastructure_details": profile.infrastructure_details,
                "experience_years": profile.experience_years,
                "identity_verified": profile.identity_verified
            })
        except SellerProfile.DoesNotExist:
            return Response({"error": "Store not found"}, status=404)

class SellerProfileListView(generics.ListAPIView):
    queryset = SellerProfile.objects.filter(is_active=True).order_by('-rating')
    serializer_class = SellerProfileSerializer
    permission_classes = (permissions.AllowAny,)
    pagination_class = None # Return all for the directory page
