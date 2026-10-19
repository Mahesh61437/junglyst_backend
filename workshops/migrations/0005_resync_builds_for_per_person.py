from django.db import migrations
from django.db.models import F


def resync_builds(apps, schema_editor):
    """0004 reached production before the code that sets `builds`, so bookings
    made in between got the column default (1). For per-person options a booking
    builds one terrarium per participant; re-derive it so none of those
    bookings read as a shared build. Shared options are left as booked."""
    Booking = apps.get_model('workshops', 'WorkshopBooking')
    Booking.objects.filter(option__pricing='per_person').update(builds=F('participants'))


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0004_group_booking'),
    ]

    operations = [
        migrations.RunPython(resync_builds, migrations.RunPython.noop),
    ]
