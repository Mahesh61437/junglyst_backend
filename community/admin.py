from django.contrib import admin
from django.utils.html import format_html
from .models import (
    CommunityProfile, Post, PostImage, CommunityTag, PostTag,
    UserFollow, TagFollow, Comment, PostLike, CommentLike,
    Report, BlockedWord,
)


@admin.register(CommunityProfile)
class CommunityProfileAdmin(admin.ModelAdmin):
    list_display = ['handle', 'user', 'follower_count', 'following_count', 'post_count', 'is_suspended', 'created_at']
    list_filter = ['is_suspended', 'is_deleted']
    search_fields = ['handle', 'user__email', 'user__username']
    readonly_fields = ['follower_count', 'following_count', 'post_count', 'created_at', 'updated_at']
    raw_id_fields = ['user']


class PostImageInline(admin.TabularInline):
    model = PostImage
    extra = 0
    readonly_fields = ['preview']
    fields = ['order', 'image_url', 'preview', 'width', 'height']

    def preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="max-height:80px;border-radius:4px;" />', obj.image_url)
        return '—'


class PostTagInline(admin.TabularInline):
    model = PostTag
    extra = 0
    raw_id_fields = ['tag']


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'author', 'post_type', 'like_count', 'comment_count', 'is_blocked', 'created_at']
    list_filter = ['post_type', 'is_blocked', 'is_deleted', 'created_at']
    search_fields = ['body', 'author__email', 'author__username']
    readonly_fields = ['id', 'like_count', 'comment_count', 'created_at', 'edited_at', 'blocked_at']
    raw_id_fields = ['author', 'tagged_product', 'blocked_by']
    inlines = [PostImageInline, PostTagInline]
    list_per_page = 50


@admin.register(CommunityTag)
class CommunityTagAdmin(admin.ModelAdmin):
    list_display = ['slug', 'name', 'post_count', 'follower_count', 'is_blocked', 'created_at']
    list_filter = ['is_blocked']
    search_fields = ['slug', 'name']
    readonly_fields = ['post_count', 'follower_count', 'created_at']


@admin.register(UserFollow)
class UserFollowAdmin(admin.ModelAdmin):
    list_display = ['follower', 'followee', 'created_at']
    search_fields = ['follower__email', 'followee__email']
    raw_id_fields = ['follower', 'followee']


@admin.register(TagFollow)
class TagFollowAdmin(admin.ModelAdmin):
    list_display = ['user', 'tag', 'created_at']
    search_fields = ['user__email', 'tag__slug']
    raw_id_fields = ['user', 'tag']


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'author', 'post', 'parent', 'like_count', 'is_blocked', 'created_at']
    list_filter = ['is_blocked', 'is_deleted', 'created_at']
    search_fields = ['body', 'author__email']
    readonly_fields = ['id', 'like_count', 'reply_count', 'created_at', 'edited_at']
    raw_id_fields = ['post', 'author', 'parent']


@admin.register(PostLike)
class PostLikeAdmin(admin.ModelAdmin):
    list_display = ['user', 'post', 'created_at']
    raw_id_fields = ['user', 'post']


@admin.register(CommentLike)
class CommentLikeAdmin(admin.ModelAdmin):
    list_display = ['user', 'comment', 'created_at']
    raw_id_fields = ['user', 'comment']


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'reporter', 'target_type', 'reason', 'status', 'created_at']
    list_filter = ['status', 'target_type', 'reason', 'created_at']
    search_fields = ['details', 'reporter__email']
    readonly_fields = ['id', 'created_at']
    raw_id_fields = ['reporter', 'reviewed_by']
    list_per_page = 50

    def __str__(self):
        return f"Report"


@admin.register(BlockedWord)
class BlockedWordAdmin(admin.ModelAdmin):
    list_display = ['word', 'severity', 'added_by', 'created_at']
    list_filter = ['severity']
    search_fields = ['word']
    raw_id_fields = ['added_by']
