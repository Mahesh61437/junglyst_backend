from django.db import migrations


def to_jng(apps, schema_editor):
    """Rename early QR-<year>-<hex> references to the JNG- shape used by order numbers."""
    QuoteRequest = apps.get_model('services', 'QuoteRequest')
    Order = apps.get_model('orders', 'Order')
    for quote in QuoteRequest.objects.filter(reference__startswith='QR-'):
        candidate = 'JNG-' + quote.reference[len('QR-'):]
        taken = (QuoteRequest.objects.filter(reference=candidate).exists()
                 or Order.objects.filter(order_number=candidate).exists())
        if not taken:
            quote.reference = candidate
            quote.save(update_fields=['reference'])


def to_qr(apps, schema_editor):
    QuoteRequest = apps.get_model('services', 'QuoteRequest')
    for quote in QuoteRequest.objects.filter(reference__startswith='JNG-'):
        quote.reference = 'QR-' + quote.reference[len('JNG-'):]
        quote.save(update_fields=['reference'])


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0002_additionalcharge'),
        ('orders', '0017_order_order_type'),
    ]

    operations = [migrations.RunPython(to_jng, to_qr)]
