import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.cache import cache
from django.conf import settings
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from .models import AppNotification, NewsletterSubscriber, ContactSubmission
from .serializers import AppNotificationSerializer

logger = logging.getLogger(__name__)

def _admin_email():
    """Return the admin inbox — never returns a display-name formatted address."""
    raw = getattr(settings, 'ADMIN_EMAIL', None) or 'admin@junglyst.com'
    # Strip display name like "Junglyst <admin@junglyst.com>" → "admin@junglyst.com"
    if '<' in raw and '>' in raw:
        raw = raw.split('<')[1].rstrip('>')
    return raw.strip()


def _send(subject, body, to):
    """Send one email and log any error — raises nothing."""
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to],
            fail_silently=False,
        )
    except Exception as exc:
        logger.error("Email send failed | to=%s subject=%r error=%s", to, subject, exc)

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


class NewsletterSubscribeView(APIView):
    """POST /api/notifications/newsletter/subscribe/ — public endpoint."""
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        if not email:
            return Response({'error': 'Email is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Please enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        subscriber, created = NewsletterSubscriber.objects.get_or_create(
            email=email,
            defaults={'is_active': True},
        )
        if not created and subscriber.is_active:
            return Response({'message': 'You are already subscribed!'})
        if not created:
            subscriber.is_active = True
            subscriber.save(update_fields=['is_active'])

        _send(
            subject='Welcome to the Junglyst Registry',
            body=(
                'Hi,\n\nThank you for joining the Junglyst Registry!\n\n'
                'You will be the first to know about new arrivals, rare specimens, and expert care guides.\n\n'
                'Happy growing,\nThe Junglyst Team\nadmin@junglyst.com'
            ),
            to=email,
        )

        return Response({'message': 'Successfully subscribed! Check your email for a welcome note.'}, status=status.HTTP_201_CREATED)


class ContactFormView(APIView):
    """POST /api/notifications/contact/ — public endpoint."""
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        email = (request.data.get('email') or '').strip()
        phone = (request.data.get('phone') or '').strip()
        topic = (request.data.get('topic') or '').strip()
        message = (request.data.get('message') or '').strip()

        if not name or not email or not message:
            return Response({'error': 'Name, email, and message are required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Please enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        ContactSubmission.objects.create(
            name=name, email=email, phone=phone, topic=topic, message=message
        )

        _send(
            subject=f'[Junglyst Contact] {topic or "General"} — {name}',
            body=(
                f'New contact form submission:\n\n'
                f'Name: {name}\nEmail: {email}\nPhone: {phone or "—"}\n'
                f'Topic: {topic or "—"}\n\nMessage:\n{message}'
            ),
            to=_admin_email(),
        )

        _send(
            subject='We received your message — Junglyst',
            body=(
                f'Hi {name},\n\nThank you for reaching out to us.\n\n'
                f'We have received your message and will respond within 1 business day.\n\n'
                f'Your message:\n"{message}"\n\n'
                f'The Junglyst Team\nadmin@junglyst.com'
            ),
            to=email,
        )

        return Response({'message': 'Message sent! We will get back to you within 1 business day.'}, status=status.HTTP_201_CREATED)
