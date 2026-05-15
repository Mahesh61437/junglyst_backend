from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.cache import cache
from .models import AppNotification
from .serializers import AppNotificationSerializer

# Cache TTL for unread counts — short enough to feel real-time, long enough to matter
_UNREAD_CACHE_TTL = 30  # seconds


def _unread_cache_key(user_id):
    return f'notif_unread_{user_id}'


def _invalidate_unread_cache(user_id):
    cache.delete(_unread_cache_key(user_id))


class NotificationListView(generics.ListAPIView):
    """GET /api/notifications/ — paginated, newest first, for the requesting user."""
    serializer_class = AppNotificationSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        return AppNotification.objects.filter(user=self.request.user)


class NotificationMarkReadView(APIView):
    """POST /api/notifications/mark-read/ — mark one or all notifications as read."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        notification_id = request.data.get('id')
        if notification_id:
            updated = AppNotification.objects.filter(
                id=notification_id, user=request.user
            ).update(is_read=True)
        else:
            updated = AppNotification.objects.filter(
                user=request.user, is_read=False
            ).update(is_read=True)

        # Bust the unread cache so next poll reflects the change immediately
        _invalidate_unread_cache(request.user.id)
        return Response({'marked': updated})


class UnreadCountView(APIView):
    """GET /api/notifications/unread-count/

    Cached per-user in Redis for 30 s — avoids a DB hit on every poll tick.
    Cache is invalidated whenever the user marks notifications as read.
    """
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        key = _unread_cache_key(request.user.id)
        count = cache.get(key)
        if count is None:
            count = AppNotification.objects.filter(
                user=request.user, is_read=False
            ).count()
            cache.set(key, count, _UNREAD_CACHE_TTL)
        return Response({'count': count})
