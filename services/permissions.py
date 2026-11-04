from django.db.models import Q
from rest_framework import permissions

from core.models import User

# Platform-run stores (Plants In My Head, Pet Kadai, ...) carry the admin role but sell like any seller.
SELLER_ROLES = ('grower', 'admin')


def assignable_sellers():
    """Verified, active stores a custom order can be handed to."""
    return User.objects.filter(
        Q(role__in=SELLER_ROLES), is_active=True, is_verified_seller=True,
        seller_profile__isnull=False, seller_profile__is_active=True,
    ).select_related('seller_profile')


class IsSellerAccount(permissions.BasePermission):
    """A signed-in store owner. Views using it only ever expose the requests assigned to them."""

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and user.role in SELLER_ROLES
            and hasattr(user, 'seller_profile')
        )
