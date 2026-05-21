import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0009_add_shipping_days'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SellerShippingConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_category', models.CharField(choices=[('light', 'Light'), ('heavy', 'Heavy')], max_length=10)),
                ('tier1_max', models.DecimalField(decimal_places=2, max_digits=10, help_text='Subtotal (₹) below which tier1_fee applies')),
                ('tier1_fee', models.DecimalField(decimal_places=2, max_digits=8, help_text='Shipping fee for subtotals below tier1_max')),
                ('tier2_max', models.DecimalField(decimal_places=2, max_digits=10, help_text='Subtotal (₹) below which tier2_fee applies (must be > tier1_max)')),
                ('tier2_fee', models.DecimalField(decimal_places=2, max_digits=8, help_text='Shipping fee for subtotals between tier1_max and tier2_max')),
                ('show_nudge_products', models.BooleanField(default=False, help_text="Show this seller's products in cart nudge to help buyers reach free shipping")),
                ('seller', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shipping_configs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Seller Shipping Config',
                'verbose_name_plural': 'Seller Shipping Configs',
                'unique_together': {('seller', 'item_category')},
            },
        ),
    ]
