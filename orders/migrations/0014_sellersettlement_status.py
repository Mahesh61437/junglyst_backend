from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0013_sellersettlement'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='sellersettlement',
            name='is_settled',
        ),
        migrations.AddField(
            model_name='sellersettlement',
            name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('completed', 'Completed')],
                default='pending',
                max_length=10,
            ),
        ),
    ]
