from django.urls import path
from .views import GrowerDashboardView

urlpatterns = [
    path('dashboard/', GrowerDashboardView.as_view(), name='grower_dashboard'),
]
