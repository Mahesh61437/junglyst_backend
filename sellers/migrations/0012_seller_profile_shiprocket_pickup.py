from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0011_add_shipping_default_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='sellerprofile',
            name='shiprocket_pickup_location',
            field=models.CharField(
                blank=True,
                null=True,
                max_length=100,
                help_text='Shiprocket pickup location name (auto-set on first shipment)',
            ),
        ),
    ]
