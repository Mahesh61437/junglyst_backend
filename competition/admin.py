from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import CompetitionEntry, EntryVote
from .views import (
    _process_image, _is_allowed_file, _is_heic,
    MAX_IMAGES, MAX_IMAGE_SIZE_BYTES, MAX_IMAGE_SIZE_MB,
)
from core.storage import upload_to_firebase


# ── Multiple-file widget/field (Django ≥4.2 dropped support for multiple=True
#    on ClearableFileInput — this is the official recipe.) ───────────────────
class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={
            'accept': 'image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif',
        }))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(d, initial) for d in data if d]
        if data in (None, ''):
            return []
        return [single_clean(data, initial)]


# ── Form: image management + prize-tier uniqueness ──────────────────────────
class CompetitionEntryAdminForm(forms.ModelForm):
    new_images = MultipleFileField(
        required=False,
        help_text=(
            f'Upload one or more new photos (JPEG / PNG / WebP / HEIC, max {MAX_IMAGE_SIZE_MB} MB each). '
            f'They are resized to 2048 px, re-encoded as JPEG, uploaded to Firebase, and appended to this entry. '
            f'Max {MAX_IMAGES} images total per entry.'
        ),
        label='Add new images',
    )
    images_to_remove = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text='Tick any photos you want to delete from this entry on save.',
        label='Remove existing images',
    )

    class Meta:
        model = CompetitionEntry
        # image_urls is managed exclusively via new_images / images_to_remove
        # below — never expose it as a form field, otherwise ModelForm validation
        # would silently overwrite it with whatever (or nothing) is in POST data.
        exclude = ['image_urls']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate remove-checkboxes with one labelled thumbnail per current URL.
        urls = list(self.instance.image_urls or [])
        choices = []
        for i, url in enumerate(urls, start=1):
            label = mark_safe(
                f'<div style="display:inline-flex;align-items:center;gap:8px;vertical-align:middle;">'
                f'<img src="{url}" style="height:60px;border-radius:6px;border:1px solid #ddd;" />'
                f'<span style="font-size:11px;color:#666;">Image #{i}</span>'
                f'</div>'
            )
            choices.append((url, label))
        self.fields['images_to_remove'].choices = choices
        if not choices:
            # Hide the field entirely when there's nothing to remove.
            self.fields['images_to_remove'].widget = forms.MultipleHiddenInput()

    def clean_new_images(self):
        # MultipleFileField returns a list of UploadedFile objects (possibly empty)
        files = self.cleaned_data.get('new_images') or []
        for f in files:
            if not _is_allowed_file(f):
                raise forms.ValidationError(
                    f'"{f.name}" is not a supported format. Use JPEG, PNG, WebP, or HEIC.'
                )
            if f.size > MAX_IMAGE_SIZE_BYTES:
                mb = round(f.size / (1024 * 1024), 1)
                raise forms.ValidationError(
                    f'"{f.name}" is {mb} MB — over the {MAX_IMAGE_SIZE_MB} MB limit.'
                )
        return files

    def clean(self):
        cleaned = super().clean()

        # Prize-tier uniqueness
        tier = cleaned.get('prize_tier') or ''
        if tier:
            qs = CompetitionEntry.objects.filter(prize_tier=tier)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                other = qs.first()
                raise forms.ValidationError(
                    f'"{dict(CompetitionEntry.PRIZE_CHOICES).get(tier, tier)}" is already assigned to '
                    f'{other.name} ({other.email}). Clear that entry first or pick a different tier.'
                )

        # Image-count guard: simulate the resulting list to make sure we stay <= MAX_IMAGES
        current = list(self.instance.image_urls or [])
        to_remove = set(cleaned.get('images_to_remove') or [])
        new_files = cleaned.get('new_images') or []
        resulting_count = len([u for u in current if u not in to_remove]) + len(new_files)
        if resulting_count > MAX_IMAGES:
            raise forms.ValidationError(
                f'This entry would end up with {resulting_count} images — the cap is {MAX_IMAGES}. '
                f'Tick more images under "Remove existing images" or upload fewer new ones.'
            )

        return cleaned


# ── Admin ───────────────────────────────────────────────────────────────────
@admin.register(CompetitionEntry)
class CompetitionEntryAdmin(admin.ModelAdmin):
    form = CompetitionEntryAdminForm
    list_display = [
        'name', 'email', 'mobile', 'instagram_handle', 'follows_instagram',
        'submitted_at', 'image_count', 'vote_count_display', 'prize_tier_display',
        'is_disqualified',
    ]
    list_filter = ['prize_tier', 'is_disqualified', 'follows_instagram', 'submitted_at']
    search_fields = ['name', 'email', 'mobile', 'instagram_handle']
    # `image_urls` is excluded by the form; we never want anyone editing the raw JSON
    # — the new_images upload + images_to_remove checkboxes are the only path.
    readonly_fields = ['id', 'submitted_at', 'image_preview', 'vote_count_display']
    ordering = ['submitted_at']
    list_per_page = 50
    actions = ['assign_first', 'assign_second', 'assign_third', 'assign_consolation', 'assign_mystery', 'clear_prize']

    fieldsets = (
        ('Contestant', {
            'fields': ('id', 'name', 'email', 'mobile', 'instagram_handle', 'follows_instagram', 'submitted_at'),
        }),
        ('Entry', {
            'fields': ('about_aquarium',),
        }),
        ('Images', {
            'fields': ('image_preview', 'new_images', 'images_to_remove'),
            'description': mark_safe(
                'Upload new photos under <b>Add new images</b> — they go through the same resize + Firebase upload '
                f'pipeline as the public submission flow. Each entry can hold up to <b>{MAX_IMAGES}</b> images. '
                'Tick checkboxes under <b>Remove existing images</b> to drop unwanted photos on save.'
            ),
        }),
        ('Voting & Winner', {
            'fields': ('vote_count_display', 'prize_tier', 'is_winner', 'is_disqualified'),
            'description': mark_safe(
                'Set <b>prize_tier</b> to mark this entry as a winner in that slot. '
                'Each tier (1st, 2nd, 3rd, 4th-Consolation, Mystery Box) can be assigned to only one entry. '
                '<code>is_winner</code> is updated automatically from prize_tier on save.'
            ),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_vc=Count('votes'))

    def save_model(self, request, obj, form, change):
        # Keep is_winner in sync with whether a prize_tier is set
        obj.is_winner = bool(obj.prize_tier)

        # 1. Drop URLs the admin ticked for removal
        to_remove = set(form.cleaned_data.get('images_to_remove') or [])
        current = list(obj.image_urls or [])
        removed_count = 0
        if to_remove:
            kept = [u for u in current if u not in to_remove]
            removed_count = len(current) - len(kept)
            obj.image_urls = kept

        # 2. Need a primary key before uploading (used as the Firebase folder)
        new_files = form.cleaned_data.get('new_images') or []
        if new_files and obj.pk is None:
            obj.save()
            change = True

        # 3. Upload each new file through the same resize + Firebase pipeline
        uploaded_urls = []
        upload_errors = []
        for f in new_files:
            processed = None
            try:
                processed = _process_image(f)
                url = upload_to_firebase(processed, str(obj.pk), 'competition')
                uploaded_urls.append(url)
            except Exception as exc:
                upload_errors.append((getattr(f, 'name', 'image'), str(exc)))
            finally:
                if processed is not None:
                    try:
                        processed.close()
                    except Exception:
                        pass

        if uploaded_urls:
            obj.image_urls = list(obj.image_urls or []) + uploaded_urls

        # 4. Persist final state (handles both create and update)
        super().save_model(request, obj, form, change)

        # 5. Surface results to the admin
        if removed_count:
            self.message_user(
                request,
                f'Removed {removed_count} image{"s" if removed_count != 1 else ""} from this entry. '
                f'(File still exists on Firebase — only the link was dropped.)',
                level=messages.WARNING,
            )
        if uploaded_urls:
            self.message_user(
                request,
                f'Uploaded {len(uploaded_urls)} new image{"s" if len(uploaded_urls) != 1 else ""}.',
                level=messages.SUCCESS,
            )
        for name, err in upload_errors:
            self.message_user(request, f'Could not upload "{name}": {err}', level=messages.ERROR)

    def image_count(self, obj):
        return len(obj.image_urls)
    image_count.short_description = 'Images'

    def vote_count_display(self, obj):
        return getattr(obj, '_vc', obj.votes.count())
    vote_count_display.short_description = 'Votes'
    vote_count_display.admin_order_field = '_vc'

    def prize_tier_display(self, obj):
        if not obj.prize_tier:
            return '—'
        label = dict(CompetitionEntry.PRIZE_CHOICES).get(obj.prize_tier, obj.prize_tier)
        color = {
            CompetitionEntry.PRIZE_FIRST: '#c9972b',
            CompetitionEntry.PRIZE_SECOND: '#a8a29e',
            CompetitionEntry.PRIZE_THIRD: '#b87333',
            CompetitionEntry.PRIZE_CONSOLATION: '#6b7280',
            CompetitionEntry.PRIZE_MYSTERY: '#9333ea',
        }.get(obj.prize_tier, '#374151')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;border-radius:10px;font-weight:600;font-size:11px;">{}</span>',
            color, label,
        )
    prize_tier_display.short_description = 'Prize'
    prize_tier_display.admin_order_field = 'prize_tier'

    def image_preview(self, obj):
        if not obj.image_urls:
            return mark_safe('<em style="color:#888">No images yet — upload one below.</em>')
        items = []
        for i, url in enumerate(obj.image_urls, start=1):
            items.append(
                f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px;">'
                f'<a href="{url}" target="_blank" rel="noopener">'
                f'<img src="{url}" style="max-height:140px;border-radius:8px;border:1px solid #ddd;" />'
                f'</a>'
                f'<span style="font-size:11px;color:#666;font-weight:600;">#{i}</span>'
                f'</div>'
            )
        return mark_safe(
            f'<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;">{"".join(items)}</div>'
        )
    image_preview.short_description = 'Current images'

    # ── Bulk actions (prize assignment) ──────────────────────────────────
    def _assign_tier(self, request, queryset, tier):
        if queryset.count() != 1:
            self.message_user(
                request,
                f'Select exactly one entry to assign a prize. You selected {queryset.count()}.',
                level=messages.ERROR,
            )
            return
        CompetitionEntry.objects.filter(prize_tier=tier).update(prize_tier='', is_winner=False)
        entry = queryset.first()
        entry.prize_tier = tier
        entry.is_winner = True
        entry.save(update_fields=['prize_tier', 'is_winner'])
        label = dict(CompetitionEntry.PRIZE_CHOICES).get(tier, tier)
        self.message_user(request, f'Assigned "{label}" to {entry.name}.', level=messages.SUCCESS)

    def assign_first(self, request, queryset):
        self._assign_tier(request, queryset, CompetitionEntry.PRIZE_FIRST)
    assign_first.short_description = 'Assign as 1st Place'

    def assign_second(self, request, queryset):
        self._assign_tier(request, queryset, CompetitionEntry.PRIZE_SECOND)
    assign_second.short_description = 'Assign as 2nd Place'

    def assign_third(self, request, queryset):
        self._assign_tier(request, queryset, CompetitionEntry.PRIZE_THIRD)
    assign_third.short_description = 'Assign as 3rd Place'

    def assign_consolation(self, request, queryset):
        self._assign_tier(request, queryset, CompetitionEntry.PRIZE_CONSOLATION)
    assign_consolation.short_description = 'Assign as 4th — Consolation'

    def assign_mystery(self, request, queryset):
        self._assign_tier(request, queryset, CompetitionEntry.PRIZE_MYSTERY)
    assign_mystery.short_description = 'Assign as Mystery Box'

    def clear_prize(self, request, queryset):
        n = queryset.update(prize_tier='', is_winner=False)
        # .update() bypasses signals, so refresh the public caches explicitly.
        from .cache import bump_cache_version
        bump_cache_version()
        self.message_user(request, f'Cleared prize from {n} entries.', level=messages.SUCCESS)
    clear_prize.short_description = 'Clear prize from selected entries'


@admin.register(EntryVote)
class EntryVoteAdmin(admin.ModelAdmin):
    list_display = ['entry', 'user', 'created_at']
    list_filter = ['created_at']
    search_fields = ['entry__name', 'entry__email', 'user__email']
    readonly_fields = ['id', 'created_at']
    raw_id_fields = ['entry', 'user']
