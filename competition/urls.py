from django.urls import path
from .views import CompetitionStatusView, CompetitionEntryView, CompetitionEntryCancelView, CompetitionImageUploadView

urlpatterns = [
    path('status/', CompetitionStatusView.as_view(), name='competition-status'),
    path('enter/', CompetitionEntryView.as_view(), name='competition-enter'),
    path('enter/<uuid:entry_id>/cancel/', CompetitionEntryCancelView.as_view(), name='competition-entry-cancel'),
    path('enter/<uuid:entry_id>/upload-image/', CompetitionImageUploadView.as_view(), name='competition-upload-image'),
]
