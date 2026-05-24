from django.db import migrations, models


DEFAULT_SHIPPING_DAYS = [0, 2, 4]  # Mon / Wed / Fri


def _default_shipping_days():
    return list(DEFAULT_SHIPPING_DAYS)


def backfill_empty_shipping_days(apps, schema_editor):
    SellerProfile = apps.get_model('sellers', 'SellerProfile')
    for profile in SellerProfile.objects.all():
        if not profile.shipping_days:
            profile.shipping_days = _default_shipping_days()
            profile.save(update_fields=['shipping_days'])


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0014_cutoff_blackouts'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sellerprofile',
            name='shipping_days',
            field=models.JSONField(
                blank=True,
                default=_default_shipping_days,
                help_text='Weekdays the seller ships: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun. Defaults to Mon/Wed/Fri.',
            ),
        ),
        migrations.RunPython(backfill_empty_shipping_days, reverse_code=migrations.RunPython.noop),
    ]
