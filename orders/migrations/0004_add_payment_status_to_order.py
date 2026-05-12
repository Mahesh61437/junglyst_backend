from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0003_add_actual_shipment_dims_to_suborder'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "UPDATE orders_order SET payment_status='pending' WHERE payment_status IS NULL;",
                    reverse_sql="UPDATE orders_order SET payment_status=NULL WHERE payment_status='pending';",
                ),
                migrations.RunSQL(
                    "ALTER TABLE orders_order ALTER COLUMN payment_status SET DEFAULT 'pending';",
                    reverse_sql="ALTER TABLE orders_order ALTER COLUMN payment_status DROP DEFAULT;",
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='order',
                    name='payment_status',
                    field=models.CharField(max_length=50, default='pending'),
                ),
            ],
        ),
    ]
