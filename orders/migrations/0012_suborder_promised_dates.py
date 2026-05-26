from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0011_alter_suborder_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='suborder',
            name='promised_ship_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='suborder',
            name='promised_delivery_min',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='suborder',
            name='promised_delivery_max',
            field=models.DateField(blank=True, null=True),
        ),
    ]
