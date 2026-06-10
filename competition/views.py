from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from django.db.models import Count, Q
from django.db import IntegrityError
from datetime import datetime, date, time, timedelta, timezone as dt_timezone
from io import BytesIO
from PIL import Image, ImageOps
from django.core.cache import cache
from .models import CompetitionEntry, EntryVote
from .serializers import CompetitionEntrySerializer, PublicEntrySerializer
from . import cache as compcache
from core.storage import upload_to_firebase
from core.config_utils import get_config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    pillow_heif = None
    HEIC_SUPPORTED = False

IST = dt_timezone(timedelta(hours=5, minutes=30))
DEFAULT_LAUNCH_DATE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=IST)
MAX_ENTRIES = 500
MAX_IMAGES = 5
MAX_IMAGE_SIZE_MB = 10
MAX_IMAGE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024
MAX_DIMENSION = 2048  # px on longest side after resize
JPEG_QUALITY = 85
SUPPORT_INSTAGRAM = '@the.junglyst'

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
ALLOWED_MIME_TYPES = {
    'image/jpeg', 'image/png', 'image/webp',
    'image/heic', 'image/heif',
}

def _support_msg():
    return f'If this keeps happening, DM us on Instagram at {SUPPORT_INSTAGRAM} and we\'ll help you submit manually.'


def _parse_dd_mm_yyyy(raw):
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
    raw = (settings_data or {}).get('launch_date')
    d = _coerce_date(raw)
    if d is None:
        return DEFAULT_LAUNCH_DATE
    return datetime.combine(d, time.min, tzinfo=IST)


def _resolve_announcement_date_iso(settings_data):
    raw = (settings_data or {}).get('result_announcement_date')
    d = _coerce_date(raw)
    if d is None:
        return None
    return d.isoformat()


def _resolve_announcement_datetime(settings_data):
    raw = (settings_data or {}).get('result_announcement_date')
    d = _coerce_date(raw)
    if d is None:
        return None
    return datetime.combine(d, time.min, tzinfo=IST)


def _current_phase(now, launch_date, announcement_dt):
    """submission → voting → results.

    - submission: now < launch_date
    - voting: launch_date <= now < announcement_dt (or no announcement_dt set)
    - results: announcement_dt is set and now >= announcement_dt
    """
    if now < launch_date:
        return 'submission'
    if announcement_dt and now >= announcement_dt:
        return 'results'
    return 'voting'


def _is_allowed_file(file_obj):
    """Check by both MIME type and extension — phones often send wrong MIME for HEIC."""
    ext = '.' + file_obj.name.rsplit('.', 1)[-1].lower() if '.' in file_obj.name else ''
    return file_obj.content_type in ALLOWED_MIME_TYPES or ext in ALLOWED_EXTENSIONS


def _is_heic(file_obj):
    mime = getattr(file_obj, 'content_type', '') or ''
    name = getattr(file_obj, 'name', '') or ''
    return mime in ('image/heic', 'image/heif') or name.lower().endswith(('.heic', '.heif'))


def _process_image(file_obj):
    """
    Decode the image (any supported format including HEIC), apply EXIF
    orientation, resize to MAX_DIMENSION on the longest side if larger,
    and re-encode as JPEG. Returns a BytesIO ready to upload.

    HEIC files are opened directly via pillow_heif to avoid relying on
    Pillow's lazy registered opener which can silently fail on some platforms.
    """
    img = None
    try:
        # Ensure we read from the start regardless of prior seeks
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)

        if _is_heic(file_obj) and HEIC_SUPPORTED:
            # Read all bytes first — ensures complete file is in memory for libheif
            raw = file_obj.read()
            heif_file = pillow_heif.open_heif(BytesIO(raw), convert_hdr_to_8bit=True)
            img = heif_file.to_pillow()
        else:
            img = Image.open(file_obj)
            img.load()  # Force decode now, not lazily

        try:
            img = ImageOps.exif_transpose(img)  # fix phone/camera rotation
        except Exception:
            pass  # EXIF transpose failure is non-fatal — continue with unrotated image

        if img.mode != 'RGB':
            img = img.convert('RGB')

        w, h = img.size
        if w > MAX_DIMENSION or h > MAX_DIMENSION:
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

        buf = BytesIO()
        img.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
        buf.seek(0)
        buf.name = 'image.jpg'
        buf.content_type = 'image/jpeg'
        return buf
    except Exception as exc:
        raise ValueError(
            'Could not read the image. '
            'Make sure it is a valid JPEG, PNG, WebP, or HEIC file.'
        ) from exc
    finally:
        if img:
            img.close()


class CompetitionStatusView(APIView):
    def get(self, request):
        # Status is identical for every visitor (no per-user data), so a short
        # global cache absorbs the per-page-load traffic. Countdowns are derived
        # client-side from the date fields, so a small lag here is invisible.
        cached = cache.get(compcache.STATUS_KEY)
        if cached is not None:
            return Response(cached)

        now = timezone.now()
        settings_data = get_config('competition_settings') or {}
        launch_date = _resolve_launch_date(settings_data)
        result_announcement_date = _resolve_announcement_date_iso(settings_data)
        announcement_dt = _resolve_announcement_datetime(settings_data)
        phase = _current_phase(now, launch_date, announcement_dt)

        total_entries = CompetitionEntry.objects.count()
        slots_remaining = max(0, MAX_ENTRIES - total_entries)
        is_open = phase == 'submission' and slots_remaining > 0
        seconds_until_launch = max(0, int((launch_date - now).total_seconds()))
        seconds_until_results = (
            max(0, int((announcement_dt - now).total_seconds())) if announcement_dt else None
        )

        winners_published = phase == 'results' and CompetitionEntry.objects.filter(
            prize_tier__in=[c[0] for c in CompetitionEntry.PRIZE_CHOICES if c[0]],
        ).exists()

        payload = {
            'launch_date': launch_date.isoformat(),
            'is_open': is_open,
            'phase': phase,
            'total_entries': total_entries,
            'slots_remaining': slots_remaining,
            'max_entries': MAX_ENTRIES,
            'seconds_until_launch': seconds_until_launch,
            'seconds_until_results': seconds_until_results,
            'winners_published': winners_published,
            'prize_amount': 1000,
            'prize_currency': 'INR',
            'result_announcement_date': result_announcement_date,
            'image_limits': {
                'max_images': MAX_IMAGES,
                'max_size_mb': MAX_IMAGE_SIZE_MB,
                'max_dimension_px': MAX_DIMENSION,
                'allowed_types': ['JPEG', 'PNG', 'WebP', 'HEIC'],
            },
        }
        cache.set(compcache.STATUS_KEY, payload, compcache.STATUS_TTL)
        return Response(payload)


class CompetitionEntryView(APIView):
    """
    POST /api/competition/enter/
    Create the entry (no images). Frontend then calls CompetitionImageUploadView
    for each image one at a time.
    """
    permission_classes = [AllowAny]

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

        return Response({
            'success': True,
            'entry_id': str(entry.id),
            'name': entry.name,
            'slots_remaining': max(0, MAX_ENTRIES - CompetitionEntry.objects.count()),
            'image_limits': {
                'max_images': MAX_IMAGES,
                'max_size_mb': MAX_IMAGE_SIZE_MB,
                'max_dimension_px': MAX_DIMENSION,
                'allowed_types': ['JPEG', 'PNG', 'WebP', 'HEIC'],
            },
        }, status=status.HTTP_201_CREATED)


class CompetitionEntryCancelView(APIView):
    """
    DELETE /api/competition/enter/<entry_id>/cancel/
    Deletes an entry only if zero images have been uploaded yet.
    Called by the frontend to clean up an orphaned entry when image upload fails.
    """
    permission_classes = [AllowAny]

    def delete(self, request, entry_id):
        try:
            entry = CompetitionEntry.objects.get(id=entry_id)
        except (CompetitionEntry.DoesNotExist, ValueError):
            return Response({'error': 'Entry not found.'}, status=status.HTTP_404_NOT_FOUND)

        if entry.image_urls:
            # Entry already has images — do not allow deletion
            return Response(
                {'error': 'Entry cannot be cancelled after images have been uploaded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entry.delete()
        return Response({'success': True, 'message': 'Entry cancelled.'}, status=status.HTTP_200_OK)


class CompetitionImageUploadView(APIView):
    """
    POST /api/competition/enter/<entry_id>/upload-image/
    Upload one image at a time. Frontend calls this sequentially per image.
    Image is resized to max 2048px and re-encoded as JPEG before upload.
    """
    permission_classes = [AllowAny]

    def post(self, request, entry_id):
        try:
            entry = CompetitionEntry.objects.get(id=entry_id)
        except (CompetitionEntry.DoesNotExist, ValueError):
            return Response(
                {
                    'error': 'Entry not found.',
                    'support': _support_msg(),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if len(entry.image_urls) >= MAX_IMAGES:
            return Response(
                {
                    'error': f'You\'ve already uploaded the maximum of {MAX_IMAGES} images for this entry.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        image = request.FILES.get('image')
        if not image:
            return Response(
                {
                    'error': 'No image received. Please select a photo and try again.',
                    'hint': f'Accepted formats: JPEG, PNG, WebP, HEIC — max {MAX_IMAGE_SIZE_MB} MB per image.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not _is_allowed_file(image):
            return Response(
                {
                    'error': f'"{image.name}" is not a supported format.',
                    'hint': f'Please upload a JPEG, PNG, WebP, or HEIC file. Max {MAX_IMAGE_SIZE_MB} MB per image.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if image.size > MAX_IMAGE_SIZE_BYTES:
            actual_mb = round(image.size / (1024 * 1024), 1)
            return Response(
                {
                    'error': f'"{image.name}" is {actual_mb} MB — over the {MAX_IMAGE_SIZE_MB} MB limit.',
                    'hint': f'Please reduce the file size to under {MAX_IMAGE_SIZE_MB} MB and try again. '
                            f'If you\'re unable to resize it, DM us on Instagram at {SUPPORT_INSTAGRAM} and we\'ll help you submit manually.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            processed = _process_image(image)
        except ValueError:
            return Response(
                {
                    'error': f'We couldn\'t read "{image.name}".',
                    'hint': f'Make sure the file is a valid JPEG, PNG, WebP, or HEIC image under {MAX_IMAGE_SIZE_MB} MB. '
                            f'{_support_msg()}',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            url = upload_to_firebase(processed, str(entry.id), 'competition')
        except Exception:
            return Response(
                {
                    'error': 'We couldn\'t upload your image right now.',
                    'hint': f'Please try again in a moment. {_support_msg()}',
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        finally:
            processed.close()

        entry.image_urls = entry.image_urls + [url]
        entry.save(update_fields=['image_urls'])

        images_uploaded = len(entry.image_urls)
        return Response({
            'success': True,
            'image_url': url,
            'images_uploaded': images_uploaded,
            'images_remaining': MAX_IMAGES - images_uploaded,
        }, status=status.HTTP_200_OK)


def _voted_ids_for(request, entry_ids):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return set()
    return set(
        EntryVote.objects.filter(user=user, entry_id__in=entry_ids).values_list('entry_id', flat=True)
    )


def _voted_str_ids_for(request, entry_ids):
    """Same as _voted_ids_for but returns string UUIDs, to match serialized ids."""
    return {str(x) for x in _voted_ids_for(request, entry_ids)}


class CompetitionEntryListView(APIView):
    """
    GET /api/competition/entries/
    Public list of all submitted entries with at least one image. No PII.
    Annotated with vote_count and (for logged-in users) has_voted.

    Query params:
      - sort: 'top' (votes desc, default) | 'new' (submitted_at desc) | 'old'
      - limit: int (default 200, max 500)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        sort = request.query_params.get('sort', 'top')
        try:
            limit = min(int(request.query_params.get('limit', 200)), 500)
        except (ValueError, TypeError):
            limit = 200

        # Shared base list (entries + vote_count, no per-user data) is cached;
        # the per-user has_voted flag is overlaid below on every request.
        key = compcache.entries_key(sort, limit)
        base = cache.get(key)
        if base is None:
            qs = (
                CompetitionEntry.objects
                .filter(is_disqualified=False)
                .exclude(image_urls=[])
                .annotate(vote_count=Count('votes'))
            )
            if sort == 'new':
                qs = qs.order_by('-submitted_at')
            elif sort == 'old':
                qs = qs.order_by('submitted_at')
            else:  # top
                qs = qs.order_by('-vote_count', '-submitted_at')

            serialized = PublicEntrySerializer(
                list(qs[:limit]), many=True, context={'voted_entry_ids': set()}
            ).data
            base = [dict(d) for d in serialized]  # plain dicts for caching
            cache.set(key, base, compcache.ENTRIES_TTL)

        voted_ids = _voted_str_ids_for(request, [d['id'] for d in base])
        results = [{**d, 'has_voted': d['id'] in voted_ids} for d in base]
        return Response({'count': len(results), 'sort': sort, 'results': results})


class CompetitionEntryDetailView(APIView):
    """
    GET /api/competition/entries/<entry_id>/
    Single public entry — powers the standalone entry page (its own URL, so it
    can be opened in a new tab / deep-linked). No PII; vote_count + has_voted.
    """
    permission_classes = [AllowAny]

    def get(self, request, entry_id):
        key = f'competition:entry:{compcache.cache_version()}:{entry_id}'
        base = cache.get(key)
        if base is None:
            try:
                entry = (
                    CompetitionEntry.objects
                    .filter(is_disqualified=False)
                    .annotate(vote_count=Count('votes'))
                    .get(id=entry_id)
                )
            except (CompetitionEntry.DoesNotExist, ValueError):
                return Response({'error': 'Entry not found.'}, status=status.HTTP_404_NOT_FOUND)
            base = dict(PublicEntrySerializer(entry, context={'voted_entry_ids': set()}).data)
            cache.set(key, base, compcache.ENTRIES_TTL)

        voted_ids = _voted_str_ids_for(request, [base['id']])
        return Response({**base, 'has_voted': base['id'] in voted_ids})


class EntryVoteView(APIView):
    """
    POST /api/competition/entries/<entry_id>/vote/  → toggle vote (idempotent).
    Body: {} — no payload required. Vote attaches to request.user.

    Voting is only allowed during the 'voting' phase.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, entry_id):
        settings_data = get_config('competition_settings') or {}
        now = timezone.now()
        launch_date = _resolve_launch_date(settings_data)
        announcement_dt = _resolve_announcement_datetime(settings_data)
        phase = _current_phase(now, launch_date, announcement_dt)

        if phase != 'voting':
            msg = (
                'Voting opens after submissions close.' if phase == 'submission'
                else 'Voting has ended — results are live.'
            )
            return Response({'error': msg, 'phase': phase}, status=status.HTTP_400_BAD_REQUEST)

        try:
            entry = CompetitionEntry.objects.get(id=entry_id, is_disqualified=False)
        except (CompetitionEntry.DoesNotExist, ValueError):
            return Response({'error': 'Entry not found.'}, status=status.HTTP_404_NOT_FOUND)

        existing = EntryVote.objects.filter(entry=entry, user=request.user).first()
        if existing:
            existing.delete()
            voted = False
        else:
            try:
                EntryVote.objects.create(entry=entry, user=request.user)
                voted = True
            except IntegrityError:
                voted = True  # race — vote already exists

        count = EntryVote.objects.filter(entry=entry).count()
        return Response({'voted': voted, 'vote_count': count, 'entry_id': str(entry.id)})


class CompetitionWinnersView(APIView):
    """
    GET /api/competition/winners/
    Returns entries that have been assigned a prize_tier, ordered by tier.
    Only meaningful in the 'results' phase but always returns whatever is set,
    so admins can preview.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        settings_data = get_config('competition_settings') or {}
        now = timezone.now()
        launch_date = _resolve_launch_date(settings_data)
        announcement_dt = _resolve_announcement_datetime(settings_data)
        phase = _current_phase(now, launch_date, announcement_dt)

        # Cached base winners list (no per-user data). Invalidated promptly when
        # an admin changes a prize_tier (CompetitionEntry save bumps the version).
        key = compcache.winners_key()
        base = cache.get(key)
        if base is None:
            tier_order = [
                CompetitionEntry.PRIZE_FIRST,
                CompetitionEntry.PRIZE_SECOND,
                CompetitionEntry.PRIZE_THIRD,
                CompetitionEntry.PRIZE_CONSOLATION,
                CompetitionEntry.PRIZE_MYSTERY,
            ]
            winners_qs = (
                CompetitionEntry.objects
                .filter(prize_tier__in=tier_order)
                .annotate(vote_count=Count('votes'))
            )
            winners_by_tier = {w.prize_tier: w for w in winners_qs}
            # Ordered list following tier_order; missing tiers omitted.
            ordered = [winners_by_tier[t] for t in tier_order if t in winners_by_tier]
            serialized = PublicEntrySerializer(
                ordered, many=True, context={'voted_entry_ids': set()}
            ).data
            base = [dict(d) for d in serialized]
            cache.set(key, base, compcache.WINNERS_TTL)

        voted_ids = _voted_str_ids_for(request, [d['id'] for d in base])
        data = [{**d, 'has_voted': d['id'] in voted_ids} for d in base]
        return Response({
            'phase': phase,
            'published': phase == 'results',
            'result_announcement_date': _resolve_announcement_date_iso(settings_data),
            'winners': data,
        })
