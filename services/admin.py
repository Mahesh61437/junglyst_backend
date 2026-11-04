from django.contrib import admin

from .models import QuoteEvent, QuoteRequest, ServiceFormField, ServiceGroup, ServiceOffering


class ServiceFormFieldInline(admin.TabularInline):
    model = ServiceFormField
    extra = 0


@admin.register(ServiceGroup)
class ServiceGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'show_on_home', 'home_sort_order')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(ServiceOffering)
class ServiceOfferingAdmin(admin.ModelAdmin):
    list_display = ('name', 'group', 'is_active', 'sort_order')
    list_filter = ('group', 'is_active')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [ServiceFormFieldInline]


class QuoteEventInline(admin.TabularInline):
    model = QuoteEvent
    extra = 0
    can_delete = False
    readonly_fields = ('actor', 'action', 'changes', 'created_at')


@admin.register(QuoteRequest)
class QuoteRequestAdmin(admin.ModelAdmin):
    list_display = ('reference', 'offering', 'contact_name', 'state_code', 'status', 'assigned_seller', 'created_at')
    list_filter = ('status', 'offering__group')
    search_fields = ('reference', 'contact_name', 'contact_phone', 'contact_email')
    readonly_fields = ('reference', 'answers', 'form_snapshot', 'order', 'created_at', 'updated_at')
    inlines = [QuoteEventInline]
