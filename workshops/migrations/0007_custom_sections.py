from django.db import migrations, models
from django.db.models import Value


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0006_option_images'),
    ]

    operations = [
        migrations.AddField(
            model_name='workshop',
            name='custom_sections',
            field=models.JSONField(blank=True, db_default=Value([], output_field=models.JSONField()), default=list),
        ),
    ]
