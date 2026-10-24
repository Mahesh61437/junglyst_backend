from django.db import migrations, models
from django.db.models import Value


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0005_resync_builds_for_per_person'),
    ]

    operations = [
        migrations.AddField(
            model_name='workshopoption',
            name='images',
            field=models.JSONField(blank=True, db_default=Value([], output_field=models.JSONField()), default=list),
        ),
    ]
