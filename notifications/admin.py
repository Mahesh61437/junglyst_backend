from django.contrib import admin
from .models import AppNotification, NewsletterSubscriber, ContactSubmission


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ('email', 'subscribed_at', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('email',)
    ordering = ('-subscribed_at',)


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'topic', 'submitted_at', 'is_resolved')
    list_filter = ('topic', 'is_resolved')
    search_fields = ('name', 'email', 'message')
    ordering = ('-submitted_at',)
    readonly_fields = ('name', 'email', 'phone', 'topic', 'message', 'submitted_at')


@admin.register(AppNotification)
class AppNotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'title', 'is_read', 'created_at')
    list_filter = ('is_read',)
    search_fields = ('user__email', 'title')
    ordering = ('-created_at',)
