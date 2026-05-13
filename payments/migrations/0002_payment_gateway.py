from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0001_initial'),
    ]

    operations = [
        # Column already exists in the production DB with NOT NULL constraint.
        # Update Django state only; skip the DDL so the migration is idempotent.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='payment',
                    name='gateway',
                    field=models.CharField(default='razorpay', max_length=50),
                ),
            ],
            database_operations=[],
        ),
    ]
