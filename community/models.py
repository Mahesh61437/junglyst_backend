import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import User, SoftDeleteModel, Product


class CommunityProfile(SoftDeleteModel):
    """Community-side data for a User. Auto-created on User save via signal."""
    user = models.OneToOneField(
        User, on_delete=models.CASCADE,
        related_name='community_profile', primary_key=True,
    )
    handle = models.SlugField(max_length=30, unique=True, db_index=True)
    bio = models.TextField(max_length=300, blank=True, default='')
    cover_image_url = models.URLField(max_length=1000, blank=True, null=True)

    # Denormalized counters — reconciled by Celery task; updated on event hot path.
    follower_count = models.PositiveIntegerField(default=0)
    following_count = models.PositiveIntegerField(default=0)
    post_count = models.PositiveIntegerField(default=0)

    is_suspended = models.BooleanField(default=False)
    suspended_reason = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"@{self.handle}"


class PostType(models.TextChoices):
    TEXT = 'text', _('Text')
    IMAGE = 'image', _('Image')
    YOUTUBE = 'youtube', _('YouTube')
    VIMEO = 'vimeo', _('Vimeo')


class Post(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='community_posts')
    body = models.TextField(max_length=1000, blank=True, default='')
    post_type = models.CharField(max_length=10, choices=PostType.choices, default=PostType.TEXT)

    # Only the field matching post_type is populated.
    youtube_url = models.URLField(max_length=500, blank=True, default='')
    vimeo_url = models.URLField(max_length=500, blank=True, default='')
    youtube_video_id = models.CharField(max_length=20, blank=True, default='')
    vimeo_video_id = models.CharField(max_length=20, blank=True, default='')

    # Marketplace tie-in — a post can reference one product.
    tagged_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='community_posts',
    )

    like_count = models.PositiveIntegerField(default=0)
    comment_count = models.PositiveIntegerField(default=0)

    is_blocked = models.BooleanField(default=False)
    blocked_reason = models.CharField(max_length=255, blank=True, default='')
    blocked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    blocked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['author', '-created_at']),
            models.Index(fields=['is_blocked', '-created_at']),
        ]

    def __str__(self):
        preview = self.body[:50] if self.body else f"[{self.post_type}]"
        return f"{self.author_id} — {preview}"


class PostImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='images')
    image_url = models.URLField(max_length=1000)
    thumbnail_url = models.URLField(max_length=1000, blank=True, default='')
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']


class CommunityTag(models.Model):
    """Open hashtag for community posts. Distinct from core.Tag (product tags)."""
    slug = models.SlugField(max_length=50, primary_key=True)
    name = models.CharField(max_length=50)
    post_count = models.PositiveIntegerField(default=0)
    follower_count = models.PositiveIntegerField(default=0)
    is_blocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-post_count', 'slug']

    def __str__(self):
        return f"#{self.slug}"


class PostTag(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='post_tags')
    tag = models.ForeignKey(CommunityTag, on_delete=models.CASCADE, related_name='post_tags')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('post', 'tag')
        indexes = [
            models.Index(fields=['tag', '-created_at']),
        ]


class UserFollow(models.Model):
    follower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='following_set')
    followee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='follower_set')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('follower', 'followee')
        indexes = [
            models.Index(fields=['followee', '-created_at']),
            models.Index(fields=['follower', '-created_at']),
        ]

    def __str__(self):
        return f"{self.follower_id} → {self.followee_id}"


class TagFollow(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='community_tag_follows')
    tag = models.ForeignKey(CommunityTag, on_delete=models.CASCADE, related_name='followers')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'tag')


class Comment(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='community_comments')
    # Max nesting depth = 1 (replies cannot have replies). Enforced in clean().
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='replies',
    )
    body = models.TextField(max_length=500)

    like_count = models.PositiveIntegerField(default=0)
    reply_count = models.PositiveIntegerField(default=0)

    is_blocked = models.BooleanField(default=False)
    blocked_reason = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['post', 'parent', 'created_at']),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.parent_id and self.parent and self.parent.parent_id:
            raise ValidationError("Replies cannot be nested more than 1 level deep.")


class PostLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='post_likes')
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'post')
        indexes = [
            models.Index(fields=['post', '-created_at']),
        ]


class CommentLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comment_likes')
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'comment')


class ReportReason(models.TextChoices):
    SPAM = 'spam', _('Spam')
    INAPPROPRIATE = 'inappropriate', _('Inappropriate / NSFW')
    HARASSMENT = 'harassment', _('Harassment / Bullying')
    MISINFORMATION = 'misinformation', _('False information')
    OTHER = 'other', _('Other')


class ReportStatus(models.TextChoices):
    PENDING = 'pending', _('Pending')
    REVIEWING = 'reviewing', _('Reviewing')
    RESOLVED = 'resolved', _('Resolved')
    DISMISSED = 'dismissed', _('Dismissed')


class ReportTargetType(models.TextChoices):
    POST = 'post', _('Post')
    COMMENT = 'comment', _('Comment')
    USER = 'user', _('User')


class Report(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reports_filed')
    target_type = models.CharField(max_length=10, choices=ReportTargetType.choices)
    target_id = models.UUIDField()
    reason = models.CharField(max_length=20, choices=ReportReason.choices)
    details = models.TextField(max_length=500, blank=True, default='')

    status = models.CharField(max_length=15, choices=ReportStatus.choices, default=ReportStatus.PENDING)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reports_reviewed',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    action_taken = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['target_type', 'target_id']),
        ]


class BlockedWordSeverity(models.TextChoices):
    WARN = 'warn', _('Warn (allow but flag)')
    BLOCK = 'block', _('Block (prevent submit)')


class BlockedWord(models.Model):
    word = models.CharField(max_length=100, unique=True)
    severity = models.CharField(
        max_length=10, choices=BlockedWordSeverity.choices,
        default=BlockedWordSeverity.BLOCK,
    )
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['word']

    def __str__(self):
        return self.word
