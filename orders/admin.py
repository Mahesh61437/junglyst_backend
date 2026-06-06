from django.contrib import admin
from .models import Order, OrderItem, SubOrder


class OrderItemInline(admin.TabularInline):
    """
    Snapshot-only inline. Product/variant/seller are raw-id fields to avoid
    rendering huge <select> dropdowns when the catalog or user table is large —
    that's what was timing out / 500'ing the Order change page in production.
    """
    model = OrderItem
    extra = 0
    can_delete = False
    raw_id_fields = ('product', 'variant', 'seller', 'sub_order')
    fields = (
        'product_name', 'variant_name', 'quantity',
        'unit_price', 'gst_percentage',
        'seller', 'sub_order',
    )
    readonly_fields = ('product_name', 'variant_name', 'quantity', 'unit_price', 'gst_percentage')

    def has_add_permission(self, request, obj=None):
        return False


class SubOrderInline(admin.TabularInline):
    model = SubOrder
    extra = 0
    can_delete = False
    raw_id_fields = ('seller',)
    fields = (
        'sub_order_number', 'seller', 'status',
        'subtotal', 'shipping_fee', 'seller_total',
        'awb_number', 'courier_name',
    )
    readonly_fields = ('sub_order_number', 'subtotal', 'shipping_fee', 'seller_total')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('order_number', 'user', 'status', 'total_amount', 'is_paid', 'created_at')
    list_filter = ('status', 'is_paid', 'payment_status', 'is_deleted')
    search_fields = ('order_number', 'user__email', 'guest_email', 'awb_number')
    raw_id_fields = ('user',)
    readonly_fields = (
        'id', 'order_number', 'shipping_address',
        'subtotal', 'shipping_fee', 'gst_total', 'total_amount',
        'created_at', 'updated_at',
    )
    inlines = [SubOrderInline, OrderItemInline]


@admin.register(SubOrder)
class SubOrderAdmin(admin.ModelAdmin):
    list_display = ('sub_order_number', 'order', 'seller', 'status', 'seller_total', 'awb_number', 'created_at')
    list_filter = ('status', 'is_deleted')
    search_fields = ('sub_order_number', 'order__order_number', 'awb_number', 'seller__email')
    raw_id_fields = ('order', 'seller')
    readonly_fields = (
        'id', 'sub_order_number', 'order',
        'subtotal', 'shipping_fee', 'seller_total',
        'created_at', 'updated_at',
    )
