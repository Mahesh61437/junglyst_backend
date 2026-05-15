from django.contrib import admin
from .models import ShippingAddress, Shipment, LogisticsProviderSettings


@admin.register(LogisticsProviderSettings)
class LogisticsProviderSettingsAdmin(admin.ModelAdmin):
    list_display = ('active_provider', 'updated_at')
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        return not LogisticsProviderSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ShippingAddress)
class ShippingAddressAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'city', 'state', 'pincode', 'is_default', 'user')
    list_filter = ('is_default', 'state')
    search_fields = ('full_name', 'phone', 'pincode')


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ('order', 'seller', 'awb_number', 'courier_name', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('awb_number', 'nimbuspost_id')
    readonly_fields = ('created_at', 'updated_at')
