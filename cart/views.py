from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Prefetch
from core.models import ProductVariant
from .models import Cart, CartItem
from .serializers import CartSerializer, CheckoutNudgeProductSerializer


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
                ).prefetch_related('product__images', 'variant__images'),
            )
        ).get(pk=cart.pk)
    )

class CartViewSet(viewsets.ModelViewSet):
    serializer_class = CartSerializer
    permission_classes = (permissions.AllowAny,)
    queryset = Cart.objects.none()   # satisfies ModelViewSet; all actions use get_cart()

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
            variant = ProductVariant.objects.select_related('product').get(id=variant_id)
        except ProductVariant.DoesNotExist:
            return Response({"error": "Variant not found"}, status=404)

        cart = self.get_cart(request)

        # SHIP-003: enforce the 3-seller cap server-side. The frontend has an
        # optimistic check, but it loses races, can be skipped by the guest→login
        # cart merge in syncCartWithBackend, and is bypassed by any direct API call.
        # Only checkout previously enforced this, which let buyers accumulate carts
        # they couldn't actually place. Cap here is the single source of truth.
        incoming_seller_id = variant.product.seller_id
        existing_seller_ids = set(
            cart.items.exclude(variant=variant).values_list('product__seller_id', flat=True)
        )
        if incoming_seller_id not in existing_seller_ids and len(existing_seller_ids) >= 3:
            return Response({
                "error": "Your cart supports up to 3 sellers. Remove an item to add from a new seller."
            }, status=400)

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

    @action(detail=False, methods=['get'], url_path='shipping-configs')
    def shipping_configs(self, request):
        """Return per-seller shipping tier configs for the given seller IDs.

        Query param: seller_ids=UUID1,UUID2,...
        Response: { "seller_id": { "light": {...}, "heavy": {...} }, ... }
        """
        from sellers.models import SellerShippingConfig
        raw = request.query_params.get('seller_ids', '')
        seller_ids = [s.strip() for s in raw.split(',') if s.strip()]
        if not seller_ids:
            return Response({})

        configs = SellerShippingConfig.objects.filter(seller_id__in=seller_ids)
        result = {}
        for cfg in configs:
            sid = str(cfg.seller_id)
            result.setdefault(sid, {})[cfg.item_category] = {
                'tier1_max': float(cfg.tier1_max),
                'tier1_fee': float(cfg.tier1_fee),
                'tier2_max': float(cfg.tier2_max),
                'tier2_fee': float(cfg.tier2_fee),
                'show_nudge_products': cfg.show_nudge_products,
            }
        return Response(result)

    @action(detail=False, methods=['get'], url_path='nudge-products')
    def nudge_products(self, request):
        """Return a handful of products from a seller to help the buyer reach
        the next shipping tier.

        Query param: seller_id=UUID
        Excludes products already in the current cart.
        """
        from core.models import Product
        seller_id = request.query_params.get('seller_id')
        if not seller_id:
            return Response({'error': 'seller_id required'}, status=400)

        cart = self.get_cart(request)
        in_cart_product_ids = set(
            str(item.product_id) for item in cart.items.select_related('product').all()
        )

        products = (
            Product.objects
            .filter(seller_id=seller_id, is_active=True)
            .exclude(id__in=in_cart_product_ids)
            .prefetch_related('variants', 'images')
        )

        result = []
        for p in products:
            variant = (
                p.variants.filter(is_active=True, stock__gt=0)
                .order_by('price')
                .first()
            )
            if not variant:
                continue
            img = p.images.first()
            result.append({
                'id': str(p.id),
                'name': p.name,
                'slug': p.slug,
                'price': float(variant.price),
                'variant_id': str(variant.id),
                'item_category': variant.item_category,
                'image_url': img.image_url if img else None,
                'stock': variant.stock,
            })
            if len(result) >= 6:
                break

        return Response(result)

    @action(detail=False, methods=['get'], url_path='checkout-nudge')
    def checkout_nudge(self, request):
        """
        GET /api/cart/checkout-nudge/
        Returns up to 8 complementary products grouped by source category.

        Priority rules:
        - One pass per complement rule (ordered by rule priority), 2 slots each.
        - Within each rule: same-rule-seller first → other cart sellers → rest.
        - If 3+ distinct sellers are already in the cart, only show products
          from those same sellers (avoid adding a 4th seller).
        - No duplicate products across rules.
        """
        from core.models import Product, CategoryComplement
        from django.db.models import BooleanField, Case, When, Value

        # Q1: cart items with categories
        cart = self.get_cart(request)
        items = list(
            cart.items.select_related('product')
            .prefetch_related('product__categories')
            .all()
        )
        if not items:
            return Response({'recommendations': []})

        cart_product_ids = {item.product_id for item in items}
        cart_seller_ids  = {item.product.seller_id for item in items}

        # source_category_id → set of seller_ids with that category in cart
        source_cat_to_sellers: dict = {}
        for item in items:
            for cat in item.product.categories.all():
                source_cat_to_sellers.setdefault(cat.id, set()).add(item.product.seller_id)

        # Q2: complement rules in priority order, with source category name
        rules = list(
            CategoryComplement.objects.filter(
                source_category_id__in=source_cat_to_sellers.keys()
            ).select_related('source_category')
            .prefetch_related('target_categories')
            .order_by('priority')
        )
        if not rules:
            return Response({'recommendations': []})

        # If 3+ sellers in cart → only recommend products from those sellers
        restrict_to_cart_sellers = len(cart_seller_ids) >= 3

        # Collect all target category IDs across all rules and build
        # a map: target_category_id → list of rule indices that want it.
        # This lets us fetch all candidates in ONE query.
        SLOTS_PER_RULE = 2
        MAX_TOTAL = 8

        rule_target_map = {}  # rule_index → set of target_cat_ids
        all_target_cat_ids = set()
        for i, rule in enumerate(rules):
            tids = {tc.id for tc in rule.target_categories.all()}  # from prefetch cache
            rule_target_map[i] = tids
            all_target_cat_ids |= tids

        if not all_target_cat_ids:
            return Response({'recommendations': []})

        # Q3: single query for all candidates across all rules
        qs = Product.objects.filter(
            is_active=True, is_draft=False,
            categories__id__in=all_target_cat_ids,
            variants__stock__gt=0, variants__is_active=True,
        ).exclude(id__in=cart_product_ids)

        if restrict_to_cart_sellers:
            qs = qs.filter(seller_id__in=cart_seller_ids)

        candidates = list(
            qs.annotate(
                from_cart_seller=Case(
                    When(seller_id__in=cart_seller_ids, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
            )
            .select_related('seller', 'seller__seller_profile')
            .prefetch_related('variants', 'images', 'categories')
            .distinct()
            .order_by('-from_cart_seller', '-rating')
        )

        # Build a lookup: product_id → set of category_ids (from prefetch cache)
        product_cat_ids: dict = {
            p.id: {c.id for c in p.categories.all()} for p in candidates
        }

        # Bucket candidates into rules in-Python, 2 slots per rule
        seen_ids: set = set(cart_product_ids)
        result = []

        for i, rule in enumerate(rules):
            if len(result) >= MAX_TOTAL:
                break
            target_ids = rule_target_map[i]
            rule_seller_ids = source_cat_to_sellers.get(rule.source_category_id, set())

            rule_picks = []
            for p in candidates:
                if p.id in seen_ids:
                    continue
                if not (product_cat_ids[p.id] & target_ids):
                    continue
                rule_picks.append(p)

            # Sort: same-rule-seller first, then same-cart-seller, then by rating
            rule_picks.sort(key=lambda p: (
                0 if p.seller_id in rule_seller_ids else 1,
                0 if p.from_cart_seller else 1,
                -float(p.rating or 0),
            ))

            for p in rule_picks[:SLOTS_PER_RULE]:
                seen_ids.add(p.id)
                p._source_category_name = rule.source_category.name
                result.append(p)

        data = CheckoutNudgeProductSerializer(result, many=True).data
        return Response({'recommendations': data})
