from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Prefetch
from core.models import ProductVariant
from .models import Cart, CartItem
from .serializers import CartSerializer


def _cart_with_prefetch(cart):
    """Re-fetch cart with all related data in a fixed number of queries."""
    return (
        Cart.objects.prefetch_related(
            Prefetch(
                'items',
                queryset=CartItem.objects.select_related(
                    'variant',
                    'product',
                    'product__seller',
                    'product__seller__seller_profile',
                ).prefetch_related('product__images'),
            )
        ).get(pk=cart.pk)
    )

class CartViewSet(viewsets.ModelViewSet):
    serializer_class = CartSerializer
    permission_classes = (permissions.AllowAny,)

    def get_cart(self, request):
        if request.user.is_authenticated:
            cart, _ = Cart.objects.get_or_create(user=request.user)
            return cart
        
        session_id = request.query_params.get('session_id') or request.data.get('session_id')
        if not session_id:
            # Fallback for simple testing or session-less requests
            # In production, middleware should ensure session_id exists
            session_id = request.session.session_key or "anonymous_session"
            
        cart, _ = Cart.objects.get_or_create(session_id=session_id)
        return cart

    def list(self, request):
        cart = self.get_cart(request)
        serializer = self.get_serializer(_cart_with_prefetch(cart))
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def add_item(self, request):
        variant_id = request.data.get('variant_id')
        quantity = int(request.data.get('quantity', 1))
        
        if not variant_id:
            return Response({"error": "variant_id is required"}, status=400)
            
        try:
            variant = ProductVariant.objects.get(id=variant_id)
        except ProductVariant.DoesNotExist:
            return Response({"error": "Variant not found"}, status=404)
            
        cart = self.get_cart(request)

        # Use all_objects to handle soft-deleted items (unique constraint includes deleted rows)
        try:
            item = CartItem.all_objects.get(cart=cart, variant=variant)
            if item.is_deleted:
                item.is_deleted = False
                item.deleted_at = None
                item.quantity = 0
        except CartItem.DoesNotExist:
            item = CartItem(cart=cart, variant=variant, product=variant.product, quantity=0)
        
        # Check total quantity against stock
        new_quantity = item.quantity + quantity
        if new_quantity > variant.stock:
            return Response({
                "error": f"Insufficient stock. Only {variant.stock} units available.",
                "available_stock": variant.stock
            }, status=400)
            
        item.quantity = new_quantity
        item.save()

        serializer = self.get_serializer(_cart_with_prefetch(cart))
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def update_item(self, request):
        item_id = request.data.get('item_id')
        quantity = int(request.data.get('quantity'))
        
        try:
            item = CartItem.objects.get(id=item_id)
            if quantity <= 0:
                # Hard delete to avoid unique constraint conflicts on re-add
                from django.db.models import Model
                Model.delete(item)
            else:
                # Check against stock
                if quantity > item.variant.stock:
                    return Response({
                        "error": f"Insufficient stock. Only {item.variant.stock} units available.",
                        "available_stock": item.variant.stock
                    }, status=400)
                
                item.quantity = quantity
                item.save()
        except (CartItem.DoesNotExist, ValueError):
            return Response({"error": "Item not found"}, status=404)
            
        cart = self.get_cart(request)
        return Response(self.get_serializer(_cart_with_prefetch(cart)).data)
