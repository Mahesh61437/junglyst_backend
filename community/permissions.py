from rest_framework import permissions


class IsAuthorOrReadOnly(permissions.BasePermission):
    """Allow read for anyone; write only for the author of the object."""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return getattr(obj, 'author_id', None) == getattr(request.user, 'id', None)


class IsSelfOrReadOnly(permissions.BasePermission):
    """For profile endpoints: read anyone, write only own profile."""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        # obj is a CommunityProfile (user_id is its primary key)
        return getattr(obj, 'user_id', None) == getattr(request.user, 'id', None)
