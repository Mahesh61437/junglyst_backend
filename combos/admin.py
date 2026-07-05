from django.contrib import admin
from django.utils.html import format_html

from .models import Combo, ComboItem, ComboImage


class ComboItemInline(admin.TabularInline):
    model = ComboItem
    extra = 1
    autocomplete_fields = ('variant',)
    fields = ('variant', 'quantity', 'sort_order')


class ComboImageInline(admin.TabularInline):
    model = ComboImage
    extra = 1
    fields = ('image_url', 'is_primary', 'order')


@admin.register(Combo)
class ComboAdmin(admin.ModelAdmin):
    inlines = (ComboItemInline, ComboImageInline)
    list_display = (
        'name', 'effective_price_display', 'shipping_fee',
        'item_count', 'seller_count', 'in_stock', 'is_active', 'is_draft', 'is_deleted',
    )
    list_filter = ('is_active', 'is_draft', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ('name', 'tagline')
    readonly_fields = ('original_price_display', 'created_by', 'created_at', 'updated_at')

    fieldsets = (
        ('Basics', {
            'fields': ('name', 'slug', 'tagline', 'description', 'image_url'),
        }),
        ('Pricing', {
            'fields': ('price', 'original_price_display', 'shipping_fee'),
            'description': "Leave 'price' blank to charge the live sum of all component "
                           "prices. 'shipping_fee' is one fixed charge for the whole combo.",
        }),
        ('Visibility', {
            'fields': ('is_active', 'is_draft'),
        }),
        ('Audit', {
            'fields': ('created_by', 'created_at', 'updated_at'),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description='Price (effective)')
    def effective_price_display(self, obj):
        return f"₹{obj.effective_price:.2f}"

    @admin.display(description='Sum of components')
    def original_price_display(self, obj):
        if not obj.pk:
            return '—'
        return format_html("₹{}", f"{obj.original_price:.2f}")

    @admin.display(description='Items')
    def item_count(self, obj):
        return obj.items.count()

    @admin.display(description='Sellers')
    def seller_count(self, obj):
        return obj.seller_count

    @admin.display(boolean=True, description='In stock')
    def in_stock(self, obj):
        return obj.in_stock
