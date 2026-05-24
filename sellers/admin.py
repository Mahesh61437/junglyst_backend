from django.contrib import admin
from .models import (
    SellerProfile, AllowedSeller, SellerShippingConfig,
    ShippingDefaultConfig, SellerBlackoutDate,
)


@admin.register(SellerBlackoutDate)
class SellerBlackoutDateAdmin(admin.ModelAdmin):
    list_display = ('seller', 'start_date', 'end_date', 'reason', 'created_at')
    list_filter = ('start_date', 'end_date')
    search_fields = ('seller__store_name', 'reason')
    ordering = ('-start_date',)


class SellerBlackoutInline(admin.TabularInline):
    model = SellerBlackoutDate
    extra = 0


@admin.register(AllowedSeller)
class AllowedSellerAdmin(admin.ModelAdmin):
    list_display = ('email', 'phone', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('email', 'phone')


@admin.register(ShippingDefaultConfig)
class ShippingDefaultConfigAdmin(admin.ModelAdmin):
    list_display = ('item_category', 'tier1_max', 'tier1_fee', 'tier2_max', 'tier2_fee')
    ordering = ('item_category',)


@admin.register(SellerShippingConfig)
class SellerShippingConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'item_category', 'tier1_max', 'tier1_fee', 'tier2_max', 'tier2_fee', 'show_nudge_products')
    list_filter = ('item_category', 'show_nudge_products')
    search_fields = ('seller__seller_profile__store_name', 'seller__email')
    ordering = ('seller__seller_profile__store_name', 'item_category')


@admin.register(SellerProfile)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ('store_name', 'user', 'is_active', 'is_featured', 'identity_verified', 'total_sales', 'rating')
    list_filter = ('is_active', 'is_featured', 'identity_verified')
    search_fields = ('store_name', 'user__email', 'user__username')
    prepopulated_fields = {'slug': ('store_name',)}
    list_editable = ('is_featured', 'identity_verified', 'is_active')
    ordering = ('sort_order', '-created_at')
    inlines = [SellerBlackoutInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'store_name', 'slug', 'tagline', 'bio', 'is_active')
        }),
        ('Visual Identity', {
            'fields': ('logo_url', 'icon_url', 'banner_url', 'brand_color')
        }),
        ('Promotion & Authenticity', {
            'fields': ('is_featured', 'sort_order', 'identity_verified', 'expertise_tags', 'experience_years')
        }),
        ('Shipping Schedule', {
            'fields': ('shipping_days', 'daily_cutoff_time'),
            'description': 'Select weekdays this seller ships. 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun. Example: [0, 2, 4] for Mon/Wed/Fri. Orders placed after daily_cutoff_time on a shipping day roll to the next shipping day.',
        }),
        ('Operations', {
            'fields': ('location_city', 'location_pincode', 'gst_number', 'gst_document_url')
        }),
        ('Metrics', {
            'fields': ('total_sales', 'rating'),
            'classes': ('collapse',)
        }),
    )
