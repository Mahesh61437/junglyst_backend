from django.urls import path
from .views import (
    NotificationListView, NotificationMarkReadView, UnreadCountView,
    NewsletterSubscribeView, ContactFormView,
)

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification_list'),
    path('mark-read/', NotificationMarkReadView.as_view(), name='notification_mark_read'),
    path('unread-count/', UnreadCountView.as_view(), name='notification_unread_count'),
    path('newsletter/subscribe/', NewsletterSubscribeView.as_view(), name='newsletter_subscribe'),
    path('contact/', ContactFormView.as_view(), name='contact_form'),
]
