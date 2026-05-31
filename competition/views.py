from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from datetime import datetime, date, time, timedelta, timezone as dt_timezone
from .models import CompetitionEntry
from .serializers import CompetitionEntrySerializer
from core.storage import upload_to_firebase
from core.config_utils import get_config

IST = dt_timezone(timedelta(hours=5, minutes=30))
DEFAULT_LAUNCH_DATE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=IST)
MAX_ENTRIES = 500


def _parse_dd_mm_yyyy(raw):
    """Parse a 'DD-MM-YYYY' string to a date, or return None."""
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split('-')
    if len(parts) != 3:
        return None
    try:
        d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _coerce_date(raw):
    """Accept 'DD-MM-YYYY' (preferred), ISO date, or ISO datetime. Return a date or None."""
    d = _parse_dd_mm_yyyy(raw)
    if d:
        return d
    if isinstance(raw, str):
        iso_date = parse_date(raw)
        if iso_date:
            return iso_date
        dt = parse_datetime(raw)
        if dt:
            return dt.date()
    return None


def _resolve_launch_date(settings_data):
    """Resolve launch_date from competition_settings as an IST-anchored datetime
    at midnight. Falls back to the default launch date when missing/invalid.
    """
    raw = (settings_data or {}).get('launch_date')
    d = _coerce_date(raw)
    if d is None:
        return DEFAULT_LAUNCH_DATE
    return datetime.combine(d, time.min, tzinfo=IST)


def _resolve_announcement_date_iso(settings_data):
    """Return result_announcement_date as 'YYYY-MM-DD' (ISO date) for the frontend."""
    raw = (settings_data or {}).get('result_announcement_date')
    d = _coerce_date(raw)
    if d is None:
        return None
    return d.isoformat()


class CompetitionStatusView(APIView):
    def get(self, request):
        now = timezone.now()
        settings_data = get_config('competition_settings') or {}
        launch_date = _resolve_launch_date(settings_data)
        result_announcement_date = _resolve_announcement_date_iso(settings_data)

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

        follows_raw = request.data.get('follows_instagram', 'false')
        follows_instagram = follows_raw in (True, 'true', 'True', '1')

        serializer = CompetitionEntrySerializer(data={
            'name': request.data.get('name', '').strip(),
            'email': email,
            'mobile': request.data.get('mobile', '').strip(),
            'about_aquarium': request.data.get('about_aquarium', '').strip(),
            'instagram_handle': request.data.get('instagram_handle', '').strip().lstrip('@'),
            'follows_instagram': follows_instagram,
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
