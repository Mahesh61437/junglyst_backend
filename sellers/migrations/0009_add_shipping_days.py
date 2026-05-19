from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sellers', '0008_add_account_holder_name_expand_payout_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='sellerprofile',
            name='shipping_days',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Weekdays the seller ships: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun',
            ),
        ),
    ]
