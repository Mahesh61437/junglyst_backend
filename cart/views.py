from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from core.models import ProductVariant
from .models import Cart, CartItem
from .serializers import CartSerializer

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
        serializer = self.get_serializer(cart)
        import json
        print("CART DATA OUT:", json.dumps(serializer.data, default=str))
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
        
        item, created = CartItem.objects.get_or_create(
            cart=cart, 
            variant=variant,
            defaults={'product': variant.product, 'quantity': 0}
        )
        
        # Check total quantity against stock
        new_quantity = item.quantity + quantity
        if new_quantity > variant.stock:
            return Response({
                "error": f"Insufficient stock. Only {variant.stock} units available.",
                "available_stock": variant.stock
            }, status=400)
            
        item.quantity = new_quantity
        item.save()
            
        serializer = self.get_serializer(cart)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def update_item(self, request):
        item_id = request.data.get('item_id')
        quantity = int(request.data.get('quantity'))
        
        try:
            item = CartItem.objects.get(id=item_id)
            if quantity <= 0:
                item.delete()
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
        return Response(self.get_serializer(cart).data)
