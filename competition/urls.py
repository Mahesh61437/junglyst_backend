from django.urls import path
from .views import CompetitionStatusView, CompetitionEntryView

urlpatterns = [
    path('status/', CompetitionStatusView.as_view(), name='competition-status'),
    path('enter/', CompetitionEntryView.as_view(), name='competition-enter'),
]
