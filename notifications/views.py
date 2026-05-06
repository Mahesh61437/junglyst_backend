from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import AppNotification
from .serializers import AppNotificationSerializer


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
            return Response({'marked': updated})
        count = AppNotification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'marked': count})


class UnreadCountView(APIView):
    """GET /api/notifications/unread-count/"""
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        count = AppNotification.objects.filter(user=request.user, is_read=False).count()
        return Response({'count': count})
