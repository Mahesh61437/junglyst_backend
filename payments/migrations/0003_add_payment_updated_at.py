from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0002_payment_gateway'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='payment',
                    name='updated_at',
                    field=models.DateTimeField(auto_now=True),
                ),
            ],
            database_operations=[],
        ),
    ]
