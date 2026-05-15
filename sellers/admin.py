from django.contrib import admin
from .models import SellerProfile, AllowedSeller

@admin.register(AllowedSeller)
class AllowedSellerAdmin(admin.ModelAdmin):
    list_display = ('email', 'phone', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('email', 'phone')

@admin.register(SellerProfile)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ('store_name', 'user', 'is_active', 'is_featured', 'identity_verified', 'total_sales', 'rating')
    list_filter = ('is_active', 'is_featured', 'identity_verified')
    search_fields = ('store_name', 'user__email', 'user__username')
    prepopulated_fields = {'slug': ('store_name',)}
    list_editable = ('is_featured', 'identity_verified', 'is_active')
    ordering = ('sort_order', '-created_at')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'store_name', 'slug', 'tagline', 'bio', 'is_active')
        }),
        ('Visual Identity', {
            'fields': ('logo_url', 'banner_url', 'brand_color')
        }),
        ('Promotion & Authenticity', {
            'fields': ('is_featured', 'sort_order', 'identity_verified', 'expertise_tags', 'experience_years')
        }),
        ('Operations', {
            'fields': ('location_city', 'location_pincode', 'gst_number', 'gst_document_url')
        }),
        ('Metrics', {
            'fields': ('total_sales', 'rating'),
            'classes': ('collapse',)
        }),
    )
