from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0012_suborder_promised_dates'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SellerSettlement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('is_settled', models.BooleanField(default=False)),
                ('settled_at', models.DateTimeField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('seller', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='settlements', to=settings.AUTH_USER_MODEL)),
                ('settled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='settlements_marked', to=settings.AUTH_USER_MODEL)),
                ('sub_order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='settlement', to='orders.suborder')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
