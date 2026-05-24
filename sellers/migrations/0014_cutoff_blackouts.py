import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0013_alter_sellerprofile_pickup_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='sellerprofile',
            name='daily_cutoff_time',
            field=models.TimeField(
                default=datetime.time(12, 0),
                help_text='Daily order cut-off (IST). Orders placed on a shipping day after this time roll to next shipping day.',
            ),
        ),
        migrations.CreateModel(
            name='SellerBlackoutDate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_date', models.DateField()),
                ('end_date', models.DateField(help_text='Inclusive — last unavailable day')),
                ('reason', models.CharField(blank=True, default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('seller', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='blackout_dates',
                    to='sellers.sellerprofile',
                )),
            ],
            options={
                'verbose_name': 'Seller Blackout Date',
                'verbose_name_plural': 'Seller Blackout Dates',
                'ordering': ['start_date'],
            },
        ),
        migrations.AddIndex(
            model_name='sellerblackoutdate',
            index=models.Index(fields=['seller', 'start_date'], name='sellers_sel_seller__7b9f4d_idx'),
        ),
    ]
