from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from datetime import datetime, time, timezone as dt_timezone
from .models import CompetitionEntry
from .serializers import CompetitionEntrySerializer
from core.storage import upload_to_firebase
from core.config_utils import get_config

DEFAULT_LAUNCH_DATE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt_timezone.utc)
MAX_ENTRIES = 500


def _resolve_launch_date(settings_data):
    """Parse launch_date from competition_settings; fall back to default.

    Accepts ISO datetime ("2026-06-01T00:00:00+05:30") or plain date
    ("2026-06-01", interpreted as midnight in the current Django timezone).
    """
    raw = (settings_data or {}).get('launch_date')
    if not raw:
        return DEFAULT_LAUNCH_DATE
    dt = parse_datetime(raw)
    if dt is not None:
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    d = parse_date(raw)
    if d is not None:
        return timezone.make_aware(datetime.combine(d, time.min), timezone.get_current_timezone())
    return DEFAULT_LAUNCH_DATE


class CompetitionStatusView(APIView):
    def get(self, request):
        now = timezone.now()
        settings_data = get_config('competition_settings') or {}
        launch_date = _resolve_launch_date(settings_data)
        result_announcement_date = settings_data.get('result_announcement_date')

        total_entries = CompetitionEntry.objects.count()
        slots_remaining = max(0, MAX_ENTRIES - total_entries)
        is_open = now < launch_date and slots_remaining > 0
        seconds_until_launch = max(0, int((launch_date - now).total_seconds()))

        winner = CompetitionEntry.objects.filter(is_winner=True).first()

        return Response({
            'launch_date': launch_date.isoformat(),
            'is_open': is_open,
            'total_entries': total_entries,
            'slots_remaining': slots_remaining,
            'max_entries': MAX_ENTRIES,
            'seconds_until_launch': seconds_until_launch,
            'winner_announced': winner is not None,
            'winner': {'name': winner.name} if winner else None,
            'prize_amount': 1000,
            'prize_currency': 'INR',
            'result_announcement_date': result_announcement_date,
        })


class CompetitionEntryView(APIView):
    def post(self, request):
        now = timezone.now()
        launch_date = _resolve_launch_date(get_config('competition_settings'))

        if now >= launch_date:
            return Response(
                {'error': 'Competition is closed. The winner will be announced soon.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        total_entries = CompetitionEntry.objects.count()
        if total_entries >= MAX_ENTRIES:
            return Response(
                {'error': 'Competition is full. All 500 submission slots have been filled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = request.data.get('email', '').lower().strip()
        if CompetitionEntry.objects.filter(email=email).exists():
            return Response(
                {'error': 'This email has already been registered for the competition.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        images = request.FILES.getlist('images')
        if not images:
            return Response(
                {'error': 'At least one image of your aquascape is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(images) > 5:
            return Response(
                {'error': 'Maximum 5 images allowed per entry.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = CompetitionEntrySerializer(data={
            'name': request.data.get('name', '').strip(),
            'email': email,
            'mobile': request.data.get('mobile', '').strip(),
            'about_aquarium': request.data.get('about_aquarium', '').strip(),
        })

        if not serializer.is_valid():
            return Response({'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        entry = serializer.save(image_urls=[])

        image_urls = []
        try:
            for img in images:
                url = upload_to_firebase(img, str(entry.id), 'competition')
                image_urls.append(url)
            entry.image_urls = image_urls
            entry.save(update_fields=['image_urls'])
        except Exception as e:
            # Entry is saved even if image upload fails; admin can handle manually
            pass

        return Response({
            'success': True,
            'message': 'Your aquascape entry has been registered! Good luck!',
            'entry_id': str(entry.id),
            'name': entry.name,
            'slots_remaining': max(0, MAX_ENTRIES - CompetitionEntry.objects.count()),
        }, status=status.HTTP_201_CREATED)
