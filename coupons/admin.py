from django.contrib import admin

from .models import Coupon, CouponRedemption


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'describe_discount', 'funded_by', 'seller', 'min_cart_value',
                    'is_active', 'valid_from', 'valid_until', 'times_used')
    list_filter = ('is_active', 'discount_type', 'funded_by', 'categories')
    search_fields = ('code', 'name', 'seller__email', 'seller__seller_profile__store_name')
    filter_horizontal = ('categories',)
    raw_id_fields = ('seller',)
    readonly_fields = ('created_by', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('code', 'name', 'description', 'is_active')}),
        ('Discount', {'fields': ('discount_type', 'discount_value', 'max_discount_amount', 'funded_by')}),
        ('Eligibility', {'fields': ('seller', 'categories', 'min_cart_value')}),
        ('Validity & limits', {'fields': ('valid_from', 'valid_until', 'usage_limit_total', 'usage_limit_per_user')}),
        ('Audit', {'fields': ('created_by', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Used')
    def times_used(self, obj):
        return obj.redemptions_counted().count()

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    list_display = ('coupon', 'order', 'user', 'guest_email', 'discount_amount', 'created_at')
    list_filter = ('coupon',)
    search_fields = ('coupon__code', 'order__order_number', 'user__email', 'guest_email')
    readonly_fields = ('coupon', 'order', 'user', 'guest_email', 'discount_amount', 'created_at')

    def has_add_permission(self, request):
        return False
