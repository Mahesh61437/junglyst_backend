from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0007_custom_sections'),
    ]

    operations = [
        migrations.AddField(
            model_name='workshop',
            name='notice',
            field=models.CharField(blank=True, db_default='', default='', max_length=300),
        ),
        migrations.AddField(
            model_name='workshop',
            name='notice_tone',
            field=models.CharField(choices=[('info', 'Info'), ('warning', 'Important'), ('success', 'Good news')],
                                   db_default='info', default='info', max_length=10),
        ),
    ]
