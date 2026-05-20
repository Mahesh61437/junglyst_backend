from django.contrib import admin
from django.utils.html import format_html
from .models import CompetitionEntry


@admin.register(CompetitionEntry)
class CompetitionEntryAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'email', 'mobile', 'submitted_at',
        'image_count', 'is_winner', 'is_disqualified',
    ]
    list_filter = ['is_winner', 'is_disqualified', 'submitted_at']
    search_fields = ['name', 'email', 'mobile']
    readonly_fields = ['id', 'submitted_at', 'image_preview']
    ordering = ['submitted_at']
    list_per_page = 50

    fieldsets = (
        ('Contestant', {
            'fields': ('id', 'name', 'email', 'mobile', 'submitted_at'),
        }),
        ('Entry', {
            'fields': ('about_aquarium', 'image_preview'),
        }),
        ('Status', {
            'fields': ('is_winner', 'is_disqualified'),
        }),
    )

    def image_count(self, obj):
        return len(obj.image_urls)
    image_count.short_description = 'Images'

    def image_preview(self, obj):
        if not obj.image_urls:
            return '—'
        imgs = ''.join(
            f'<img src="{url}" style="max-height:120px;margin:4px;border-radius:6px;" />'
            for url in obj.image_urls
        )
        return format_html(f'<div style="display:flex;flex-wrap:wrap;gap:4px;">{imgs}</div>')
    image_preview.short_description = 'Submitted Images'
