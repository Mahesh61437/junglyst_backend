"""
DRF serializers for the community feature.

Layered by purpose:
  - Read: nested, denormalized for cheap rendering on feed cards.
  - Write: thin, validates only what the client supplies.

Tags arrive as a list of strings on write; `get_or_create` handles
the slug/name CommunityTag rows. Profanity blocklist is consulted via
community.moderation before persistence.
"""
import re
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from core.models import Product
from .models import (
    CommunityProfile, Post, PostImage, CommunityTag, PostTag,
    UserFollow, TagFollow, Comment, PostLike, CommentLike,
    Report, PostType,
)
from .moderation import raise_if_blocked


# ── Helpers ──────────────────────────────────────────────────────────────────

YOUTUBE_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{6,20})'
)
VIMEO_RE = re.compile(r'vimeo\.com/(?:video/)?(\d{6,})')


def extract_youtube_id(url: str) -> str:
    m = YOUTUBE_RE.search(url or '')
    return m.group(1) if m else ''


def extract_vimeo_id(url: str) -> str:
    m = VIMEO_RE.search(url or '')
    return m.group(1) if m else ''


def parse_hashtags(body: str, explicit_tags=None) -> list:
    """
    Combine explicitly-supplied tag strings with hashtags parsed from body.
    Returns a deduped list of lowercase slugified tag strings (max 10).
    """
    found = set()
    for raw in (explicit_tags or []):
        s = slugify(str(raw))[:50]
        if s:
            found.add(s)
    for raw in re.findall(r'#([A-Za-z0-9_]{1,50})', body or ''):
        s = slugify(raw)[:50]
        if s:
            found.add(s)
    return list(found)[:10]


# ── User-facing nested serializers ───────────────────────────────────────────

class PublicAuthorSerializer(serializers.Serializer):
    """Minimal author info embedded in posts and comments."""
    id = serializers.UUIDField(read_only=True)
    handle = serializers.SerializerMethodField()
    avatar_url = serializers.URLField(read_only=True)
    is_verified_seller = serializers.BooleanField(read_only=True)

    def get_handle(self, user):
        profile = getattr(user, 'community_profile', None)
        return profile.handle if profile else ''


class CommunityProfileSerializer(serializers.ModelSerializer):
    """GET /api/community/users/@<handle>/ — public profile + viewer-relative flags."""
    handle = serializers.SlugField()
    user_id = serializers.UUIDField(source='user.id', read_only=True)
    avatar_url = serializers.URLField(source='user.avatar_url', read_only=True)
    is_verified_seller = serializers.BooleanField(source='user.is_verified_seller', read_only=True)
    location = serializers.CharField(source='user.location', read_only=True)

    is_followed_by_me = serializers.SerializerMethodField()

    class Meta:
        model = CommunityProfile
        fields = [
            'user_id', 'handle', 'bio', 'cover_image_url', 'avatar_url',
            'is_verified_seller', 'location',
            'follower_count', 'following_count', 'post_count',
            'is_followed_by_me', 'created_at',
        ]
        read_only_fields = [
            'follower_count', 'following_count', 'post_count', 'created_at',
        ]

    def get_is_followed_by_me(self, profile):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return UserFollow.objects.filter(
            follower=request.user, followee_id=profile.user_id,
        ).exists()

    def validate_handle(self, value):
        value = value.lower()
        existing = CommunityProfile.objects.filter(handle=value).exclude(
            user_id=self.instance.user_id if self.instance else None
        )
        if existing.exists():
            raise serializers.ValidationError("That handle is taken.")
        return value


# ── Tag ──────────────────────────────────────────────────────────────────────

class CommunityTagSerializer(serializers.ModelSerializer):
    is_followed_by_me = serializers.SerializerMethodField()

    class Meta:
        model = CommunityTag
        fields = ['slug', 'name', 'post_count', 'follower_count', 'is_followed_by_me']
        read_only_fields = ['post_count', 'follower_count']

    def get_is_followed_by_me(self, tag):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return TagFollow.objects.filter(user=request.user, tag=tag).exists()


# ── Post image (read-only here — upload happens in Week 2 media pipeline) ────

class PostImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostImage
        fields = ['id', 'image_url', 'thumbnail_url', 'width', 'height', 'order']


# ── Tagged product (minimal — for marketplace tie-in) ────────────────────────

class TaggedProductSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'name', 'slug', 'image']

    def get_image(self, product):
        imgs = list(product.images.all())
        primary = next((i for i in imgs if getattr(i, 'is_primary', False)), None)
        target = primary or (imgs[0] if imgs else None)
        return target.image_url if target else None


# ── Post ─────────────────────────────────────────────────────────────────────

class PostSerializer(serializers.ModelSerializer):
    """Feed-card representation."""
    author = PublicAuthorSerializer(read_only=True)
    images = PostImageSerializer(many=True, read_only=True)
    tags = serializers.SerializerMethodField()
    tagged_product = TaggedProductSerializer(read_only=True)
    is_liked_by_me = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            'id', 'author', 'body', 'post_type',
            'youtube_url', 'vimeo_url', 'youtube_video_id', 'vimeo_video_id',
            'tagged_product',
            'images', 'tags',
            'like_count', 'comment_count', 'is_liked_by_me',
            'created_at', 'edited_at',
        ]
        read_only_fields = [
            'id', 'author', 'youtube_video_id', 'vimeo_video_id',
            'like_count', 'comment_count', 'is_liked_by_me',
            'created_at', 'edited_at',
        ]

    def get_tags(self, post):
        return [pt.tag_id for pt in post.post_tags.all()]

    def get_is_liked_by_me(self, post):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return PostLike.objects.filter(user=request.user, post=post).exists()


class PostCreateSerializer(serializers.ModelSerializer):
    """
    Request body shape:
        {
          "body": "got my first monstera! #monstera #propagation",
          "post_type": "text" | "image" | "youtube" | "vimeo",
          "youtube_url": "...",
          "vimeo_url": "...",
          "tagged_product_id": "<uuid>" (optional),
          "tags": ["monstera", "propagation"] (optional — merged with hashtags from body),
          "image_urls": ["https://...", "..."] (optional, for image posts)
        }
    """
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False, default=list,
    )
    tagged_product_id = serializers.UUIDField(required=False, allow_null=True)
    image_urls = serializers.ListField(
        child=serializers.URLField(max_length=1000),
        required=False, default=list, max_length=10,
    )

    class Meta:
        model = Post
        fields = [
            'body', 'post_type',
            'youtube_url', 'vimeo_url',
            'tagged_product_id', 'tags', 'image_urls',
        ]

    def validate(self, attrs):
        post_type = attrs.get('post_type', PostType.TEXT)
        body = attrs.get('body', '') or ''
        youtube_url = attrs.get('youtube_url', '') or ''
        vimeo_url = attrs.get('vimeo_url', '') or ''
        image_urls = attrs.get('image_urls') or []

        if post_type == PostType.YOUTUBE:
            if not youtube_url:
                raise serializers.ValidationError({'youtube_url': 'Required for YouTube posts.'})
            vid = extract_youtube_id(youtube_url)
            if not vid:
                raise serializers.ValidationError({'youtube_url': 'Could not parse a YouTube video ID.'})
            attrs['youtube_video_id'] = vid

        if post_type == PostType.VIMEO:
            if not vimeo_url:
                raise serializers.ValidationError({'vimeo_url': 'Required for Vimeo posts.'})
            vid = extract_vimeo_id(vimeo_url)
            if not vid:
                raise serializers.ValidationError({'vimeo_url': 'Could not parse a Vimeo video ID.'})
            attrs['vimeo_video_id'] = vid

        if post_type == PostType.IMAGE and not image_urls:
            raise serializers.ValidationError({'image_urls': 'Image posts need at least one image.'})

        if post_type == PostType.TEXT and not body.strip():
            raise serializers.ValidationError({'body': 'Text posts need a body.'})

        # Profanity check on body
        raise_if_blocked(body)

        # Resolve tagged_product_id → instance
        tp_id = attrs.pop('tagged_product_id', None)
        if tp_id:
            try:
                attrs['tagged_product'] = Product.objects.get(id=tp_id, is_active=True)
            except Product.DoesNotExist:
                raise serializers.ValidationError({'tagged_product_id': 'Product not found or inactive.'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context['request']
        explicit_tags = validated_data.pop('tags', [])
        image_urls = validated_data.pop('image_urls', [])
        body = validated_data.get('body', '') or ''

        post = Post.objects.create(author=request.user, **validated_data)

        # Tags: combine explicit + hashtags from body
        tag_slugs = parse_hashtags(body, explicit_tags)
        if tag_slugs:
            tag_objs = []
            for slug in tag_slugs:
                tag, _ = CommunityTag.objects.get_or_create(
                    slug=slug, defaults={'name': slug},
                )
                tag_objs.append(tag)
            PostTag.objects.bulk_create(
                [PostTag(post=post, tag=t) for t in tag_objs],
                ignore_conflicts=True,
            )
            CommunityTag.objects.filter(slug__in=tag_slugs).update(
                post_count=F('post_count') + 1,
            )

        # Images: caller has already uploaded to Firebase and supplies URLs.
        # Full multi-image pipeline (Pillow thumbnails) lands in Week 2.
        if image_urls:
            PostImage.objects.bulk_create([
                PostImage(post=post, image_url=url, order=i)
                for i, url in enumerate(image_urls)
            ])

        # Denorm: bump author post_count
        CommunityProfile.objects.filter(user=request.user).update(
            post_count=F('post_count') + 1,
        )
        return post


class PostUpdateSerializer(serializers.ModelSerializer):
    """Body-only edit within 5-minute window (enforced in the view)."""
    class Meta:
        model = Post
        fields = ['body']

    def validate_body(self, value):
        raise_if_blocked(value)
        return value

    def update(self, instance, validated_data):
        instance.body = validated_data.get('body', instance.body)
        instance.edited_at = timezone.now()
        instance.save(update_fields=['body', 'edited_at'])
        return instance


# ── Comment ──────────────────────────────────────────────────────────────────

class CommentSerializer(serializers.ModelSerializer):
    author = PublicAuthorSerializer(read_only=True)
    is_liked_by_me = serializers.SerializerMethodField()
    parent_id = serializers.UUIDField(source='parent.id', read_only=True, allow_null=True)

    class Meta:
        model = Comment
        fields = [
            'id', 'post', 'author', 'parent_id', 'body',
            'like_count', 'reply_count', 'is_liked_by_me',
            'created_at', 'edited_at',
        ]
        read_only_fields = [
            'id', 'post', 'author', 'parent_id', 'like_count', 'reply_count',
            'is_liked_by_me', 'created_at', 'edited_at',
        ]

    def get_is_liked_by_me(self, comment):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return CommentLike.objects.filter(user=request.user, comment=comment).exists()


class CommentCreateSerializer(serializers.ModelSerializer):
    parent_id = serializers.UUIDField(required=False, allow_null=True, write_only=True)

    class Meta:
        model = Comment
        fields = ['body', 'parent_id']

    def validate_body(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Comment body cannot be empty.")
        raise_if_blocked(value)
        return value

    def validate(self, attrs):
        parent_id = attrs.pop('parent_id', None)
        post = self.context.get('post')
        if parent_id:
            try:
                parent = Comment.objects.get(id=parent_id, post=post)
            except Comment.DoesNotExist:
                raise serializers.ValidationError({'parent_id': 'Parent comment not found on this post.'})
            if parent.parent_id:
                raise serializers.ValidationError(
                    {'parent_id': 'Replies cannot be nested more than 1 level deep.'}
                )
            attrs['parent'] = parent
        attrs['post'] = post
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context['request']
        comment = Comment.objects.create(author=request.user, **validated_data)

        Post.objects.filter(pk=comment.post_id).update(
            comment_count=F('comment_count') + 1,
        )
        if comment.parent_id:
            Comment.objects.filter(pk=comment.parent_id).update(
                reply_count=F('reply_count') + 1,
            )
        return comment


class CommentUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ['body']

    def validate_body(self, value):
        raise_if_blocked(value)
        return value

    def update(self, instance, validated_data):
        instance.body = validated_data.get('body', instance.body)
        instance.edited_at = timezone.now()
        instance.save(update_fields=['body', 'edited_at'])
        return instance


# ── Reports ──────────────────────────────────────────────────────────────────

class ReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = [
            'id', 'target_type', 'target_id', 'reason', 'details',
            'status', 'created_at',
        ]
        read_only_fields = ['id', 'status', 'created_at']

    def create(self, validated_data):
        return Report.objects.create(
            reporter=self.context['request'].user,
            **validated_data,
        )
