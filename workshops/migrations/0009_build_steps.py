from django.db import migrations, models
from django.db.models import Value


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0008_workshop_notice'),
    ]

    operations = [
        migrations.AddField(
            model_name='workshop',
            name='build_steps',
            field=models.JSONField(blank=True, db_default=Value([], output_field=models.JSONField()), default=list),
        ),
    ]
