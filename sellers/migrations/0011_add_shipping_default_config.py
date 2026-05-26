from django.db import migrations, models


def seed_defaults(apps, schema_editor):
    ShippingDefaultConfig = apps.get_model('sellers', 'ShippingDefaultConfig')
    ShippingDefaultConfig.objects.bulk_create([
        ShippingDefaultConfig(
            item_category='light',
            tier1_max=699, tier1_fee=99,
            tier2_max=999, tier2_fee=49,
        ),
        ShippingDefaultConfig(
            item_category='heavy',
            tier1_max=999,  tier1_fee=99,
            tier2_max=1499, tier2_fee=49,
        ),
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0010_add_seller_shipping_config'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShippingDefaultConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_category', models.CharField(choices=[('light', 'Light'), ('heavy', 'Heavy')], max_length=10, unique=True)),
                ('tier1_max', models.DecimalField(decimal_places=2, max_digits=10, help_text='Default subtotal (₹) below which tier1_fee applies')),
                ('tier1_fee', models.DecimalField(decimal_places=2, max_digits=8,  help_text='Default shipping fee for subtotals below tier1_max')),
                ('tier2_max', models.DecimalField(decimal_places=2, max_digits=10, help_text='Default subtotal (₹) below which tier2_fee applies')),
                ('tier2_fee', models.DecimalField(decimal_places=2, max_digits=8,  help_text='Default shipping fee for subtotals between tier1_max and tier2_max')),
            ],
            options={
                'verbose_name': 'Shipping Default Config',
                'verbose_name_plural': 'Shipping Default Configs',
            },
        ),
        migrations.RunPython(seed_defaults, migrations.RunPython.noop),
    ]
