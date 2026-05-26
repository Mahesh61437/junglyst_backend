"""
Management command: seed_categories
Replaces/creates the canonical aquarium category tree with subcategories.

Run:
    python manage.py seed_categories

Safe to re-run — updates existing records rather than duplicating.
GST percentages are set at the category level; subcategories inherit them (gst_percentage=None).
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify
from decimal import Decimal


# ---------------------------------------------------------------------------
# Canonical category tree
# GST is set per category; subcategories inherit (leave gst_percentage=None).
# Shipping type drives which shipping-rate tier is applied at checkout.
# ---------------------------------------------------------------------------
CATEGORIES_DATA = [
    {
        'name': 'Aquarium Accessories',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'Tank Accessories',
            'Aquarium Safety Mats',
            'Fish Nets',
        ],
    },
    {
        'name': 'Aquarium Filters',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'Accessories & Spares',
            'External Canister Filters',
            'External Filters',
            'Hang On Filters',
            'Internal Filters',
            'Sponge Filters',
        ],
    },
    {
        'name': 'Aquarium Lights',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'LED Lights',
            'Clip On Lights',
            'Lighting Accessories',
        ],
    },
    {
        'name': 'Aquarium Tanks',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('12.00'),
        'shipping_type': 'heavy',
        'subcategories': [
            'Ultra Crystal Clear Tanks',
            'Moulded Tanks',
            'Rimless Tanks',
        ],
    },
    {
        'name': 'Aquascaping',
        'gst_percentage': Decimal('12.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'heavy',
        'subcategories': [
            'Aqua Soil',
            'Rocks & Stones',
            'Driftwood',
            'Tools',
            'Fertilizers',
            'Starter Packs',
        ],
    },
    {
        'name': 'Planted Aquarium CO2',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'Complete CO2 Kits',
            'CO2 Regulators',
            'CO2 Diffusers',
            'CO2 Accessories',
        ],
    },
    {
        'name': 'Terrarium & Paludarium',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'heavy',
        'subcategories': [
            'Terrarium Tanks',
            'Terrarium Accessories',
            'Terrarium Moss',
            'Terrarium Plants',
        ],
    },
    {
        'name': 'Cooling Systems',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'Cooling Fans',
            'Chillers',
        ],
    },
    {
        'name': 'Fish & Aquarium Care',
        'gst_percentage': Decimal('5.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'Fish Food',
            'Water Conditioners',
            'Aquarium Medicines',
        ],
    },
    {
        'name': 'Brand & Specialized',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('12.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            'DOOA System',
            'ADA Products',
            'Chihiros',
            'Juwel Aquarium Line',
        ],
    },
    {
        'name': 'Furniture & Setup',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('12.00'),
        'shipping_type': 'heavy',
        'subcategories': [
            'Cabinets',
            'Aquarium Stands',
        ],
    },
    {
        'name': 'Plants',
        'gst_percentage': Decimal('5.00'),
        'commission_rate': Decimal('20.00'),
        'shipping_type': 'plant',
        'subcategories': [
            'Aquatic Plants',
            'Aquatic Moss',
            'Beginner Friendly',
            'Carnivorous Plants',
            'Carpet Plants',
            'Epiphytes',
            'Ferns',
            'Floating Plants',
            'Lily',
            'Mosses',
            'Orchids',
            'Pot Plants',
            'Rare & Exotic',
            'Succulents & Cacti',
            'Tissue Culture',
            'Tropical Plants',
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed canonical aquarium categories and subcategories'

    @transaction.atomic
    def handle(self, *args, **options):
        from core.models import Category, SubCategory

        self.stdout.write(self.style.MIGRATE_HEADING('Seeding categories…'))

        total_cats = 0
        total_subs = 0

        for cat_data in CATEGORIES_DATA:
            slug = slugify(cat_data['name'])
            cat, created = Category.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': cat_data['name'],
                    'gst_percentage': cat_data['gst_percentage'],
                    'commission_rate': cat_data['commission_rate'],
                    'shipping_type': cat_data['shipping_type'],
                },
            )
            if not created:
                cat.name = cat_data['name']
                cat.gst_percentage = cat_data['gst_percentage']
                cat.commission_rate = cat_data['commission_rate']
                cat.shipping_type = cat_data['shipping_type']
                cat.is_deleted = False
                cat.save()

            action = 'Created' if created else 'Updated'
            self.stdout.write(f'  {action}: {cat.name}  (GST {cat.gst_percentage}%)')
            total_cats += 1

            for sub_name in cat_data.get('subcategories', []):
                sub_slug = slugify(f"{cat_data['name']}-{sub_name}")
                sub, sub_created = SubCategory.objects.get_or_create(
                    category=cat,
                    name=sub_name,
                    defaults={
                        'slug': sub_slug,
                        # Inherits GST & commission from parent (null = inherit)
                        'gst_percentage': None,
                        'commission_rate': None,
                    },
                )
                if not sub_created:
                    sub.name = sub_name
                    sub.category = cat
                    sub.slug = sub_slug
                    sub.is_deleted = False
                    sub.save()

                sub_action = 'Created' if sub_created else 'Updated'
                self.stdout.write(f'    {sub_action}: {sub.name}')
                total_subs += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {total_cats} categories, {total_subs} subcategories.'
        ))
