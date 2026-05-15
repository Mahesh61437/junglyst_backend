from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Legacy branch migration kept for graph merge (0007).

    Schema changes live in 0004_order_payment_status (portable AddField).
    The previous version used PostgreSQL-only ALTER COLUMN SQL, which breaks
    on SQLite when DB_HOST is unset.
    """

    dependencies = [
        ('orders', '0003_add_actual_shipment_dims_to_suborder'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name='order',
                    name='payment_status',
                    field=models.CharField(max_length=50, default='pending'),
                ),
            ],
        ),
    ]
