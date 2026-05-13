from django.contrib import admin
from .models import Payment, PaymentGatewaySettings


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        'order_number', 'gateway', 'status', 'gateway_status',
        'amount', 'method', 'bank_reference', 'created_at', 'paid_at',
    )
    list_filter = ('gateway', 'status', 'gateway_status', 'method', 'created_at')
    search_fields = (
        'order__order_number',
        'cashfree_order_id', 'cashfree_payment_id',
        'razorpay_order_id', 'razorpay_payment_id',
        'bank_reference',
        'error_code', 'error_message',
    )
    readonly_fields = (
        'order', 'gateway', 'cashfree_order_id', 'cashfree_session_id',
        'cashfree_payment_id', 'razorpay_order_id', 'razorpay_payment_id',
        'razorpay_signature', 'amount', 'method', 'status',
        'bank_reference', 'gateway_status', 'error_code', 'error_message',
        'gateway_response', 'paid_at', 'created_at', 'updated_at',
    )
    date_hierarchy = 'created_at'

    fieldsets = (
        ('Order', {
            'fields': ('order', 'amount', 'status', 'paid_at'),
        }),
        ('Gateway Details', {
            'fields': ('gateway', 'method', 'gateway_status', 'bank_reference'),
        }),
        ('Cashfree', {
            'classes': ('collapse',),
            'fields': ('cashfree_order_id', 'cashfree_session_id', 'cashfree_payment_id'),
        }),
        ('Razorpay', {
            'classes': ('collapse',),
            'fields': ('razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature'),
        }),
        ('Error / Failure Info', {
            'classes': ('collapse',),
            'fields': ('error_code', 'error_message'),
        }),
        ('Raw Gateway Response', {
            'classes': ('collapse',),
            'fields': ('gateway_response',),
            'description': 'Full JSON response from the payment gateway — use this to investigate disputes.',
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def order_number(self, obj):
        return obj.order.order_number
    order_number.short_description = 'Order #'
    order_number.admin_order_field = 'order__order_number'

    def has_add_permission(self, request):
        return False  # Payments are created by the checkout flow, not manually

    def has_delete_permission(self, request, obj=None):
        return False  # Never delete payment records


@admin.register(PaymentGatewaySettings)
class PaymentGatewaySettingsAdmin(admin.ModelAdmin):
    list_display = ('active_gateway', 'updated_at')
