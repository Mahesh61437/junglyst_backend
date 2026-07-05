from rest_framework import generics, permissions
from django.db.models import Prefetch

from .models import Combo, ComboItem
from .serializers import ComboListSerializer, ComboDetailSerializer


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
