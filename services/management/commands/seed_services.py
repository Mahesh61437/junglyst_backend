"""Create the starter "Terrariums" service group and its custom-terrarium offering.

Idempotent: existing groups/offerings (matched by slug) are left untouched, so
re-running never overwrites what the admin has edited.

    python manage.py seed_services
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from services.models import FormFieldType, ServiceFormField, ServiceGroup, ServiceOffering

GROUP = {
    'name': 'Terrariums', 'slug': 'terrariums', 'show_on_home': True, 'home_sort_order': 1,
    'description': 'Closed and open terrariums designed around your space, theme and budget.',
}
OFFERING = {
    'name': 'Custom terrarium', 'slug': 'custom-terrarium', 'sort_order': 1,
    'description': 'Tell us the size, style and plants you like. We design it, send you a clear quote, and ship it ready to display.',
    'suggested_min_budget': Decimal('2500'),
}
QUESTIONS = (
    dict(key='terrarium_type', label='Type', field_type=FormFieldType.SELECT, required=True,
         choices=['Closed terrarium', 'Open terrarium', 'Bottle garden', 'Not sure yet']),
    dict(key='container_shape', label='Preferred shape', field_type=FormFieldType.SELECT,
         choices=['Cube', 'Round bowl', 'Rectangle', 'Hexagon', 'Bottle', 'Open to suggestions']),
    dict(key='approx_size_cm', label='Approximate size (longest side, cm)', field_type=FormFieldType.NUMBER,
         min_value=Decimal('10'), max_value=Decimal('150'), help_text='A rough number is fine.'),
    dict(key='theme', label='Theme', field_type=FormFieldType.MULTISELECT,
         choices=['Tropical jungle', 'Desert', 'Forest floor', 'Waterfall', 'Minimal', 'Fantasy']),
    dict(key='needed_by', label='Needed by (if there is a date)', field_type=FormFieldType.DATE),
    dict(key='occasion', label='Gift or occasion?', field_type=FormFieldType.TEXT, max_length=120),
)


class Command(BaseCommand):
    help = 'Create the starter Terrariums service group and custom-terrarium offering (idempotent).'

    @transaction.atomic
    def handle(self, *args, **options):
        group, group_created = ServiceGroup.objects.get_or_create(slug=GROUP['slug'], defaults=GROUP)
        offering, offering_created = ServiceOffering.objects.get_or_create(
            slug=OFFERING['slug'], defaults={**OFFERING, 'group': group},
        )
        if offering_created:
            ServiceFormField.objects.bulk_create(
                ServiceFormField(offering=offering, sort_order=index, **question) for index, question in enumerate(QUESTIONS)
            )
        self.stdout.write(self.style.SUCCESS(
            f'Group {"created" if group_created else "kept"}, offering {"created" if offering_created else "kept"}.'
        ))
