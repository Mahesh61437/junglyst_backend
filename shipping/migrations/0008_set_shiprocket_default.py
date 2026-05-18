from django.db import migrations


def set_shiprocket_as_default(apps, schema_editor):
    LogisticsProviderSettings = apps.get_model('shipping', 'LogisticsProviderSettings')
    obj, _ = LogisticsProviderSettings.objects.get_or_create(id=1)
    obj.active_provider = 'shiprocket'
    obj.save()


def revert_to_nimbuspost(apps, schema_editor):
    LogisticsProviderSettings = apps.get_model('shipping', 'LogisticsProviderSettings')
    LogisticsProviderSettings.objects.filter(id=1).update(active_provider='nimbuspost')


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0007_logistics_provider_settings'),
    ]

    operations = [
        migrations.RunPython(set_shiprocket_as_default, revert_to_nimbuspost),
    ]
