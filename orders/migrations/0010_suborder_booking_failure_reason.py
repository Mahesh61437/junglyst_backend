from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0009_update_order_status_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='suborder',
            name='booking_failure_reason',
            field=models.TextField(blank=True, null=True),
        ),
    ]
