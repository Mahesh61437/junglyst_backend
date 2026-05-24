from django.contrib import admin, messages
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


# ---------------------------------------------------------------------------
# Shiprocket / NimbusPost API status → SubOrder status
# ---------------------------------------------------------------------------
_TRACKING_STATUS_MAP = {
    "booked": "booked", "pickup_scheduled": "booked",
    "picked_up": "shipped", "shipped": "shipped",
    "in_transit": "in_transit", "in transit": "in_transit",
    "reached at destination hub": "in_transit", "delayed": "in_transit",
    "out_for_delivery": "out_for_delivery", "out for delivery": "out_for_delivery",
    "delivered": "delivered",
    "undelivered": "delivery_failed", "delivery_failed": "delivery_failed",
    "delivery failed": "delivery_failed",
    "rto_initiated": "delivery_failed", "rto initiated": "delivery_failed",
    "rto_delivered": "cancelled", "rto delivered": "cancelled",
    "rto acknowledged": "cancelled",
    "cancelled": "cancelled", "canceled": "cancelled",
    "shipment cancelled": "cancelled", "order cancelled": "cancelled",
    "cancellation requested": "cancelled",
    "lost": "delivery_failed", "damaged": "delivery_failed",
}


def _apply_shipment_status(shipment: Shipment, new_status: str):
    """Write new_status to Shipment + SubOrder + Order (with proper rollup)."""
    from orders.models import SubOrder
    from shipping.views import _sync_order_status

    Shipment.objects.filter(pk=shipment.pk).update(status=new_status)
    SubOrder.objects.filter(awb_number=shipment.awb_number).update(status=new_status)
    _sync_order_status(shipment.order_id)


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ('order', 'seller', 'awb_number', 'courier_name', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('awb_number', 'nimbuspost_id')
    readonly_fields = ('created_at', 'updated_at')
    actions = ['action_sync_from_courier', 'action_force_cancel']

    @admin.action(description='Sync status from courier API (Shiprocket / NimbusPost)')
    def action_sync_from_courier(self, request, queryset):
        from shipping.services import get_logistics_service
        service = get_logistics_service()
        ok = skipped = failed = 0

        for shipment in queryset:
            if not shipment.awb_number:
                self.message_user(
                    request,
                    f"Shipment {shipment.pk} has no AWB — skipped.",
                    level=messages.WARNING,
                )
                skipped += 1
                continue

            result = service.track_shipment(shipment.awb_number)
            if not result or not result.get("status"):
                self.message_user(
                    request,
                    f"AWB {shipment.awb_number}: courier API returned no data.",
                    level=messages.WARNING,
                )
                failed += 1
                continue

            tracking = result.get("data", {})
            raw = (
                tracking.get("shipment_status")
                or tracking.get("current_status")
                or tracking.get("status")
                or ""
            ).lower().strip()

            new_status = _TRACKING_STATUS_MAP.get(raw) or _TRACKING_STATUS_MAP.get(raw.replace(" ", "_"))
            if not new_status:
                self.message_user(
                    request,
                    f"AWB {shipment.awb_number}: unknown courier status '{raw}' — use Force Cancel if needed.",
                    level=messages.WARNING,
                )
                failed += 1
                continue

            _apply_shipment_status(shipment, new_status)
            self.message_user(
                request,
                f"AWB {shipment.awb_number}: synced → {new_status}",
                level=messages.SUCCESS,
            )
            ok += 1

        if ok:
            self.message_user(request, f"{ok} shipment(s) synced successfully.", level=messages.SUCCESS)

    @admin.action(description='Force mark as CANCELLED (use when courier cancelled but webhook missed)')
    def action_force_cancel(self, request, queryset):
        ok = 0
        for shipment in queryset:
            _apply_shipment_status(shipment, "cancelled")
            ok += 1
        self.message_user(
            request,
            f"{ok} shipment(s) marked as cancelled and Order status rolled up.",
            level=messages.SUCCESS,
        )
