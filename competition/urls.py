from django.urls import path
from .views import (
    CompetitionStatusView,
    CompetitionEntryView,
    CompetitionEntryCancelView,
    CompetitionImageUploadView,
    CompetitionEntryListView,
    CompetitionEntryDetailView,
    EntryVoteView,
    CompetitionWinnersView,
)

urlpatterns = [
    path('status/', CompetitionStatusView.as_view(), name='competition-status'),
    path('enter/', CompetitionEntryView.as_view(), name='competition-enter'),
    path('enter/<uuid:entry_id>/cancel/', CompetitionEntryCancelView.as_view(), name='competition-entry-cancel'),
    path('enter/<uuid:entry_id>/upload-image/', CompetitionImageUploadView.as_view(), name='competition-upload-image'),
    path('entries/', CompetitionEntryListView.as_view(), name='competition-entries'),
    path('entries/<uuid:entry_id>/', CompetitionEntryDetailView.as_view(), name='competition-entry-detail'),
    path('entries/<uuid:entry_id>/vote/', EntryVoteView.as_view(), name='competition-entry-vote'),
    path('winners/', CompetitionWinnersView.as_view(), name='competition-winners'),
]
