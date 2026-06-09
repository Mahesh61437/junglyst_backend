from django.conf import settings
from django.db import models
import uuid


class CompetitionEntry(models.Model):
    PRIZE_NONE = ''
    PRIZE_FIRST = 'first'
    PRIZE_SECOND = 'second'
    PRIZE_THIRD = 'third'
    PRIZE_CONSOLATION = 'consolation'
    PRIZE_MYSTERY = 'mystery'

    PRIZE_CHOICES = [
        (PRIZE_NONE, '—'),
        (PRIZE_FIRST, '1st Place'),
        (PRIZE_SECOND, '2nd Place'),
        (PRIZE_THIRD, '3rd Place'),
        (PRIZE_CONSOLATION, '4th — Consolation'),
        (PRIZE_MYSTERY, 'Mystery Box'),
    ]

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
    prize_tier = models.CharField(
        max_length=20, choices=PRIZE_CHOICES, blank=True, default=PRIZE_NONE,
        help_text='Set to assign a prize. Only one entry per tier; saving here also flips is_winner.',
    )

    class Meta:
        ordering = ['submitted_at']
        verbose_name = 'Competition Entry'
        verbose_name_plural = 'Competition Entries'

    def __str__(self):
        return f"{self.name} ({self.email})"


class EntryVote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(CompetitionEntry, on_delete=models.CASCADE, related_name='votes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='competition_votes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('entry', 'user')
        indexes = [models.Index(fields=['entry'])]

    def __str__(self):
        return f"{self.user_id} → {self.entry_id}"
