from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add CONFIRMED and FAILED to OrderStatus; remove PLACED.
    Payment success → 'confirmed'; payment failure → 'failed'.
    No DB schema change — TextChoices are Python-only validation.
    """

    dependencies = [
        ('orders', '0008_alter_order_payment_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('confirmed', 'Confirmed'),
                    ('failed', 'Failed'),
                    ('processing', 'Processing'),
                    ('shipped', 'Shipped'),
                    ('delivered', 'Delivered'),
                    ('cancelled', 'Cancelled'),
                    ('returned', 'Returned'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
