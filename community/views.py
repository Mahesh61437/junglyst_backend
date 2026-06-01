"""
DRF views for the community feature.

Week 1: chronological list + Post/Comment CRUD + like/follow toggles +
profile read/update + tag read. The personalized following/tag feed with
Redis caching lands in Week 3.
"""
from datetime import timedelta

from django.db.models import F, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import User
from core.pagination import StandardResultsSetPagination

from .models import (
    CommunityProfile, Post, PostImage, CommunityTag, PostTag,
    UserFollow, TagFollow, Comment, PostLike, CommentLike,
)
from .permissions import IsAuthorOrReadOnly, IsSelfOrReadOnly
from .serializers import (
    CommunityProfileSerializer,
    CommunityTagSerializer,
    PostSerializer, PostCreateSerializer, PostUpdateSerializer,
    CommentSerializer, CommentCreateSerializer, CommentUpdateSerializer,
    ReportSerializer,
)


POST_EDIT_WINDOW = timedelta(minutes=5)
COMMENT_EDIT_WINDOW = timedelta(minutes=5)


# ── Post querysets used in multiple views ────────────────────────────────────

def _post_queryset():
    """Base queryset with prefetches every Post-rendering view needs."""
    return (
        Post.objects
        .filter(is_blocked=False)
        .select_related('author', 'author__community_profile', 'tagged_product')
        .prefetch_related(
            Prefetch('images', queryset=PostImage.objects.order_by('order')),
            Prefetch('post_tags', queryset=PostTag.objects.select_related('tag')),
            Prefetch('tagged_product__images'),
        )
    )


# ── Posts ────────────────────────────────────────────────────────────────────

class PostListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/community/posts/           — chronological public feed
    POST /api/community/posts/           — create (auth required)
    """
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def get_queryset(self):
        return _post_queryset().order_by('-created_at')

    def get_serializer_class(self):
        return PostCreateSerializer if self.request.method == 'POST' else PostSerializer

    def create(self, request, *args, **kwargs):
        write_serializer = self.get_serializer(data=request.data)
        write_serializer.is_valid(raise_exception=True)
        post = write_serializer.save()
        # Return the rich read shape, not the thin create shape
        read_serializer = PostSerializer(post, context={'request': request})
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)


class PostDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/community/posts/<uuid>/
    PATCH  /api/community/posts/<uuid>/   (author only, within 5 min)
    DELETE /api/community/posts/<uuid>/   (author only)
    """
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsAuthorOrReadOnly]
    lookup_field = 'id'

    def get_queryset(self):
        return _post_queryset()

    def get_serializer_class(self):
        if self.request.method in ('PATCH', 'PUT'):
            return PostUpdateSerializer
        return PostSerializer

    def update(self, request, *args, **kwargs):
        post = self.get_object()
        # Edit window enforcement
        if timezone.now() - post.created_at > POST_EDIT_WINDOW:
            raise PermissionDenied("The 5-minute edit window has passed.")
        return super().update(request, *args, **kwargs)

    def perform_destroy(self, post):
        # SoftDeleteModel handles the .delete() override
        post.delete()
        CommunityProfile.objects.filter(user_id=post.author_id).update(
            post_count=F('post_count') - 1,
        )


class PostLikeToggleView(APIView):
    """POST /api/community/posts/<uuid>/like/ — toggle like."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, id):
        post = get_object_or_404(Post, id=id, is_blocked=False)
        like, created = PostLike.objects.get_or_create(user=request.user, post=post)
        if created:
            Post.objects.filter(pk=post.pk).update(like_count=F('like_count') + 1)
            return Response({'liked': True}, status=status.HTTP_201_CREATED)

        like.delete()
        Post.objects.filter(pk=post.pk).update(like_count=F('like_count') - 1)
        return Response({'liked': False})


class PostReportView(APIView):
    """POST /api/community/posts/<uuid>/report/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, id):
        post = get_object_or_404(Post, id=id)
        data = dict(request.data)
        data['target_type'] = 'post'
        data['target_id'] = str(post.id)
        ser = ReportSerializer(data=data, context={'request': request})
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


# ── Comments ─────────────────────────────────────────────────────────────────

class CommentListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/community/posts/<uuid>/comments/
    POST /api/community/posts/<uuid>/comments/
    """
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def get_post(self):
        return get_object_or_404(Post, id=self.kwargs['post_id'], is_blocked=False)

    def get_queryset(self):
        return (
            Comment.objects
            .filter(post_id=self.kwargs['post_id'], is_blocked=False)
            .select_related('author', 'author__community_profile')
            .order_by('created_at')
        )

    def get_serializer_class(self):
        return CommentCreateSerializer if self.request.method == 'POST' else CommentSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.method == 'POST':
            ctx['post'] = self.get_post()
        return ctx

    def create(self, request, *args, **kwargs):
        write_serializer = self.get_serializer(data=request.data)
        write_serializer.is_valid(raise_exception=True)
        comment = write_serializer.save()
        read = CommentSerializer(comment, context={'request': request})
        return Response(read.data, status=status.HTTP_201_CREATED)


class CommentDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/community/comments/<uuid>/
    PATCH  /api/community/comments/<uuid>/   (author only, within 5 min)
    DELETE /api/community/comments/<uuid>/   (author only)
    """
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsAuthorOrReadOnly]
    lookup_field = 'id'

    def get_queryset(self):
        return (
            Comment.objects
            .select_related('author', 'author__community_profile', 'post')
        )

    def get_serializer_class(self):
        if self.request.method in ('PATCH', 'PUT'):
            return CommentUpdateSerializer
        return CommentSerializer

    def update(self, request, *args, **kwargs):
        comment = self.get_object()
        if timezone.now() - comment.created_at > COMMENT_EDIT_WINDOW:
            raise PermissionDenied("The 5-minute edit window has passed.")
        return super().update(request, *args, **kwargs)

    def perform_destroy(self, comment):
        post_id = comment.post_id
        parent_id = comment.parent_id
        comment.delete()
        Post.objects.filter(pk=post_id).update(comment_count=F('comment_count') - 1)
        if parent_id:
            Comment.objects.filter(pk=parent_id).update(reply_count=F('reply_count') - 1)


class CommentLikeToggleView(APIView):
    """POST /api/community/comments/<uuid>/like/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, id):
        comment = get_object_or_404(Comment, id=id, is_blocked=False)
        like, created = CommentLike.objects.get_or_create(user=request.user, comment=comment)
        if created:
            Comment.objects.filter(pk=comment.pk).update(like_count=F('like_count') + 1)
            return Response({'liked': True}, status=status.HTTP_201_CREATED)
        like.delete()
        Comment.objects.filter(pk=comment.pk).update(like_count=F('like_count') - 1)
        return Response({'liked': False})


class CommentReportView(APIView):
    """POST /api/community/comments/<uuid>/report/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, id):
        comment = get_object_or_404(Comment, id=id)
        data = dict(request.data)
        data['target_type'] = 'comment'
        data['target_id'] = str(comment.id)
        ser = ReportSerializer(data=data, context={'request': request})
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_201_CREATED)


# ── Users / Profiles ─────────────────────────────────────────────────────────

class MyProfileView(generics.RetrieveUpdateAPIView):
    """
    GET   /api/community/users/me/
    PATCH /api/community/users/me/
    """
    serializer_class = CommunityProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        profile, _ = CommunityProfile.objects.get_or_create(user=self.request.user)
        return profile


class ProfileByHandleView(generics.RetrieveAPIView):
    """GET /api/community/users/@<handle>/"""
    serializer_class = CommunityProfileSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'handle'

    def get_queryset(self):
        return CommunityProfile.objects.select_related('user')


class UserPostsView(generics.ListAPIView):
    """GET /api/community/users/@<handle>/posts/"""
    serializer_class = PostSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        handle = self.kwargs['handle']
        profile = get_object_or_404(CommunityProfile, handle=handle)
        return _post_queryset().filter(author_id=profile.user_id).order_by('-created_at')


class UserFollowToggleView(APIView):
    """POST /api/community/users/<uuid>/follow/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        if str(user_id) == str(request.user.id):
            raise ValidationError("You can't follow yourself.")
        target = get_object_or_404(User, id=user_id)
        follow, created = UserFollow.objects.get_or_create(
            follower=request.user, followee=target,
        )
        if created:
            CommunityProfile.objects.filter(user_id=target.id).update(
                follower_count=F('follower_count') + 1,
            )
            CommunityProfile.objects.filter(user_id=request.user.id).update(
                following_count=F('following_count') + 1,
            )
            return Response({'following': True}, status=status.HTTP_201_CREATED)

        follow.delete()
        CommunityProfile.objects.filter(user_id=target.id).update(
            follower_count=F('follower_count') - 1,
        )
        CommunityProfile.objects.filter(user_id=request.user.id).update(
            following_count=F('following_count') - 1,
        )
        return Response({'following': False})


# ── Tags ─────────────────────────────────────────────────────────────────────

class TagListView(generics.ListAPIView):
    """GET /api/community/tags/?q=mon — search/list."""
    serializer_class = CommunityTagSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = CommunityTag.objects.filter(is_blocked=False)
        q = self.request.query_params.get('q', '').strip().lower()
        if q:
            qs = qs.filter(slug__startswith=q)
        return qs.order_by('-post_count')


class TagDetailView(generics.RetrieveAPIView):
    """GET /api/community/tags/<slug>/"""
    serializer_class = CommunityTagSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        return CommunityTag.objects.filter(is_blocked=False)


class TagFollowToggleView(APIView):
    """POST /api/community/tags/<slug>/follow/"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug):
        tag = get_object_or_404(CommunityTag, slug=slug, is_blocked=False)
        follow, created = TagFollow.objects.get_or_create(user=request.user, tag=tag)
        if created:
            CommunityTag.objects.filter(pk=tag.pk).update(
                follower_count=F('follower_count') + 1,
            )
            return Response({'following': True}, status=status.HTTP_201_CREATED)
        follow.delete()
        CommunityTag.objects.filter(pk=tag.pk).update(
            follower_count=F('follower_count') - 1,
        )
        return Response({'following': False})


class TagPostsView(generics.ListAPIView):
    """GET /api/community/tags/<slug>/posts/"""
    serializer_class = PostSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        slug = self.kwargs['slug']
        return (
            _post_queryset()
            .filter(post_tags__tag_id=slug)
            .order_by('-created_at')
            .distinct()
        )
