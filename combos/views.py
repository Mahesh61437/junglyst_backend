from rest_framework import generics, permissions, status
from rest_framework.response import Response
from django.db.models import Prefetch

from core.models import ProductVariant
from core.views import IsAdminOrSuperAdmin
from .models import Combo, ComboItem
from .serializers import ComboListSerializer, ComboDetailSerializer, ComboAdminSerializer


def _items_prefetch():
    """Load each combo's components with everything the serializers need,
    in a fixed number of queries (variant → product → seller + images)."""
    return Prefetch(
        'items',
        queryset=ComboItem.objects.select_related(
            'variant',
            'variant__product',
            'variant__product__seller',
            'variant__product__seller__seller_profile',
        ).prefetch_related(
            'variant__images',
            'variant__product__images',
        ),
    )


class ComboListView(generics.ListAPIView):
    """GET /api/combos/ — published, active combos for the storefront."""
    serializer_class = ComboListSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        qs = (
            Combo.objects
            .filter(is_active=True, is_draft=False)
            .prefetch_related(_items_prefetch())
        )
        params = self.request.query_params
        if params.get('featured') in ('1', 'true', 'True'):
            qs = qs.filter(is_featured=True)
        combo_type = params.get('type')
        if combo_type:
            qs = qs.filter(combo_type=combo_type)
        return qs


class ComboDetailView(generics.RetrieveAPIView):
    """GET /api/combos/<slug>/ — full combo with components grouped by seller."""
    serializer_class = ComboDetailSerializer
    permission_classes = (permissions.AllowAny,)
    lookup_field = 'slug'

    def get_queryset(self):
        return (
            Combo.objects
            .filter(is_active=True, is_draft=False)
            .prefetch_related(_items_prefetch(), 'images')
        )


def _set_combo_items(combo, items_payload):
    """Replace a combo's items wholesale from [{variant_id, quantity}, ...].

    The builder UI always sends the full desired item list on save, so a
    delete-then-recreate is simpler and safer than diffing against what's
    already there.
    """
    combo.items.all().delete()
    rows = []
    for i, entry in enumerate(items_payload or []):
        variant_id = entry.get('variant_id')
        if not variant_id:
            continue
        try:
            variant = ProductVariant.objects.get(id=variant_id)
        except (ProductVariant.DoesNotExist, ValueError, TypeError):
            continue
        quantity = max(1, int(entry.get('quantity') or 1))
        rows.append(ComboItem(combo=combo, variant=variant, quantity=quantity, sort_order=i))
    ComboItem.objects.bulk_create(rows)


class ComboAdminListCreateView(generics.ListCreateAPIView):
    """SuperAdmin combo builder — GET lists every combo (incl. drafts/inactive),
    POST creates one with its component items."""
    serializer_class = ComboAdminSerializer
    permission_classes = (IsAdminOrSuperAdmin,)

    def get_queryset(self):
        return Combo.objects.prefetch_related(_items_prefetch()).order_by('-created_at')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        combo = serializer.save(created_by=request.user)
        _set_combo_items(combo, request.data.get('items'))
        combo.refresh_from_db()
        return Response(self.get_serializer(combo).data, status=status.HTTP_201_CREATED)


class ComboAdminDetailView(generics.RetrieveUpdateDestroyAPIView):
    """SuperAdmin combo builder — GET/PATCH/PUT/DELETE a single combo by id."""
    serializer_class = ComboAdminSerializer
    permission_classes = (IsAdminOrSuperAdmin,)
    lookup_field = 'id'

    def get_queryset(self):
        return Combo.objects.prefetch_related(_items_prefetch())

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        combo = serializer.save()
        if 'items' in request.data:
            _set_combo_items(combo, request.data.get('items'))
            combo.refresh_from_db()
        return Response(self.get_serializer(combo).data)
