from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import datetime, timezone as dt_timezone
from .models import CompetitionEntry
from .serializers import CompetitionEntrySerializer
from core.storage import upload_to_firebase
from core.config_utils import get_config

LAUNCH_DATE = datetime(2026, 5, 25, 0, 0, 0, tzinfo=dt_timezone.utc)
MAX_ENTRIES = 500


class CompetitionStatusView(APIView):
    def get(self, request):
        now = timezone.now()
        total_entries = CompetitionEntry.objects.count()
        slots_remaining = max(0, MAX_ENTRIES - total_entries)
        is_open = now < LAUNCH_DATE and slots_remaining > 0
        seconds_until_launch = max(0, int((LAUNCH_DATE - now).total_seconds()))

        winner = CompetitionEntry.objects.filter(is_winner=True).first()

        settings_data = get_config('competition_settings') or {}
        result_announcement_date = settings_data.get('result_announcement_date')

        return Response({
            'launch_date': LAUNCH_DATE.isoformat(),
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

        if now >= LAUNCH_DATE:
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
