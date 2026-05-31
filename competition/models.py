from django.db import models
import uuid


class CompetitionEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    mobile = models.CharField(max_length=15)
    about_aquarium = models.TextField()
    image_urls = models.JSONField(default=list, blank=True)
    instagram_handle = models.CharField(max_length=100, blank=True, default='')
    follows_instagram = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)
    is_winner = models.BooleanField(default=False)
    is_disqualified = models.BooleanField(default=False)

    class Meta:
        ordering = ['submitted_at']
        verbose_name = 'Competition Entry'
        verbose_name_plural = 'Competition Entries'

    def __str__(self):
        return f"{self.name} ({self.email})"
