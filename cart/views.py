from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from core.models import ProductVariant
from .models import Cart, CartItem
from .serializers import CartSerializer

class CartViewSet(viewsets.ModelViewSet):
    serializer_class = CartSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        user = self.request.user
        session_id = self.request.query_params.get('session_id')
        if user.is_authenticated:
            return Cart.objects.filter(user=user)
        if session_id:
            return Cart.objects.filter(session_id=session_id)
        return Cart.objects.none()

    @action(detail=False, methods=['post'])
    def add_item(self, request):
        cart_id = request.data.get('cart_id')
        variant_id = request.data.get('variant_id')
        quantity = int(request.data.get('quantity', 1))
        
        variant = ProductVariant.objects.get(id=variant_id)
        cart, created = Cart.objects.get_or_create(id=cart_id)
        
        item, item_created = CartItem.objects.get_or_create(
            cart=cart, variant=variant, 
            defaults={'product': variant.product, 'quantity': quantity}
        )
        if not item_created:
            item.quantity += quantity
            item.save()
            
        return Response(CartSerializer(cart).data)
