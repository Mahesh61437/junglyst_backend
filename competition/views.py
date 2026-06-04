from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from datetime import datetime, date, time, timedelta, timezone as dt_timezone
from io import BytesIO
from PIL import Image, ImageOps
from .models import CompetitionEntry
from .serializers import CompetitionEntrySerializer
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
            'image_limits': {
                'max_images': MAX_IMAGES,
                'max_size_mb': MAX_IMAGE_SIZE_MB,
                'max_dimension_px': MAX_DIMENSION,
                'allowed_types': ['JPEG', 'PNG', 'WebP', 'HEIC'],
            },
        })


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
