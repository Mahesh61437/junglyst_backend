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
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def add_item(self, request):
        variant_id = request.data.get('variant_id')
        product_id = request.data.get('productId')
        quantity = int(request.data.get('quantity', 1))
        
        from django.core.exceptions import ValidationError
        
        if not variant_id:
            if not product_id:
                return Response({"error": "variant_id or productId is required"}, status=400)
            try:
                variant = ProductVariant.objects.filter(product_id=product_id).first()
                if not variant:
                    return Response({"error": "No variants found for this product"}, status=404)
            except ValidationError:
                return Response({"error": "Invalid product ID"}, status=400)
        else:
            try:
                variant = ProductVariant.objects.get(id=variant_id)
            except (ProductVariant.DoesNotExist, ValidationError):
                return Response({"error": "Variant not found"}, status=404)
            
        cart = self.get_cart(request)
        
        try:
            item = CartItem.all_objects.get(cart=cart, variant=variant)
            if item.is_deleted:
                item.is_deleted = False
                item.deleted_at = None
                item.quantity = quantity
            else:
                item.quantity += quantity
            item.save()
        except CartItem.DoesNotExist:
            item = CartItem.objects.create(
                cart=cart, 
                variant=variant,
                product=variant.product,
                quantity=quantity
            )
            
        serializer = self.get_serializer(cart)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def update_item(self, request):
        item_id = request.data.get('item_id')
        quantity = request.data.get('quantity')
        
        try:
            item = CartItem.objects.get(id=item_id)
            if int(quantity) <= 0:
                item.delete()
            else:
                item.quantity = quantity
                item.save()
        except (CartItem.DoesNotExist, ValueError):
            return Response({"error": "Item not found"}, status=404)
            
        cart = self.get_cart(request)
        return Response(self.get_serializer(cart).data)
