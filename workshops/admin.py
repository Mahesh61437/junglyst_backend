from django.contrib import admin

from .models import Workshop, WorkshopBooking, WorkshopOption, WorkshopPayout, WorkshopSession


class WorkshopOptionInline(admin.TabularInline):
    model = WorkshopOption
    extra = 0


class WorkshopSessionInline(admin.TabularInline):
    model = WorkshopSession
    extra = 0


@admin.register(Workshop)
class WorkshopAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'seller', 'status', 'city', 'commission_percent', 'created_at')
    list_filter = ('status', 'category', 'city')
    search_fields = ('title', 'city', 'seller__email')
    inlines = (WorkshopOptionInline, WorkshopSessionInline)
    raw_id_fields = ('seller', 'created_by')


@admin.register(WorkshopBooking)
class WorkshopBookingAdmin(admin.ModelAdmin):
    list_display = ('order', 'workshop_title', 'session_date', 'attendee_name', 'attendee_phone',
                    'participants', 'total_amount', 'status')
    list_filter = ('status', 'session_date')
    search_fields = ('order__order_number', 'attendee_name', 'attendee_phone', 'attendee_email')
    raw_id_fields = ('order', 'user', 'seller', 'workshop', 'session', 'option')


@admin.register(WorkshopPayout)
class WorkshopPayoutAdmin(admin.ModelAdmin):
    list_display = ('workshop', 'seller', 'amount', 'paid_on', 'reference')
    raw_id_fields = ('workshop', 'seller', 'recorded_by')
