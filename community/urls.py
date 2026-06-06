from django.urls import path
from .views import (
    PostListCreateView, PostDetailView, PostLikeToggleView, PostReportView,
    CommentListCreateView, CommentDetailView, CommentLikeToggleView, CommentReportView,
    MyProfileView, ProfileByHandleView, UserPostsView, UserFollowToggleView,
    TagListView, TagDetailView, TagFollowToggleView, TagPostsView,
)


urlpatterns = [
    # Posts
    path('posts/', PostListCreateView.as_view(), name='community_post_list'),
    path('posts/<uuid:id>/', PostDetailView.as_view(), name='community_post_detail'),
    path('posts/<uuid:id>/like/', PostLikeToggleView.as_view(), name='community_post_like'),
    path('posts/<uuid:id>/report/', PostReportView.as_view(), name='community_post_report'),

    # Comments on a post
    path('posts/<uuid:post_id>/comments/', CommentListCreateView.as_view(), name='community_comment_list'),

    # Comment item
    path('comments/<uuid:id>/', CommentDetailView.as_view(), name='community_comment_detail'),
    path('comments/<uuid:id>/like/', CommentLikeToggleView.as_view(), name='community_comment_like'),
    path('comments/<uuid:id>/report/', CommentReportView.as_view(), name='community_comment_report'),

    # Profile
    path('users/me/', MyProfileView.as_view(), name='community_my_profile'),
    path('users/@<slug:handle>/', ProfileByHandleView.as_view(), name='community_profile_by_handle'),
    path('users/@<slug:handle>/posts/', UserPostsView.as_view(), name='community_user_posts'),
    path('users/<uuid:user_id>/follow/', UserFollowToggleView.as_view(), name='community_user_follow'),

    # Tags
    path('tags/', TagListView.as_view(), name='community_tag_list'),
    path('tags/<slug:slug>/', TagDetailView.as_view(), name='community_tag_detail'),
    path('tags/<slug:slug>/follow/', TagFollowToggleView.as_view(), name='community_tag_follow'),
    path('tags/<slug:slug>/posts/', TagPostsView.as_view(), name='community_tag_posts'),
]
