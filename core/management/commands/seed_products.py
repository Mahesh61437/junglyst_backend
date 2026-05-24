"""
Management command: seed_products
Creates categories, subcategories, shipping rates, and 10 products per seller.
Run: python manage.py seed_products
Safe to re-run — skips already-existing items by name/slug.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify
from decimal import Decimal
import random

CATEGORIES_DATA = [
    {
        'name': 'Aquatic Plants',
        'gst_percentage': Decimal('12.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'plant',
        'subcategories': [
            {'name': 'Stem Plants',      'gst_percentage': None, 'commission_rate': None},
            {'name': 'Rosette Plants',   'gst_percentage': None, 'commission_rate': None},
            {'name': 'Epiphytes',        'gst_percentage': None, 'commission_rate': None},
            {'name': 'Carpeting Plants', 'gst_percentage': None, 'commission_rate': None},
            {'name': 'Floating Plants',  'gst_percentage': None, 'commission_rate': None},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,    'max_weight_grams': 500,  'rate': Decimal('49.00'),  'free_above_order_value': Decimal('699.00')},
            {'min_weight_grams': 500,  'max_weight_grams': 1500, 'rate': Decimal('79.00'),  'free_above_order_value': Decimal('999.00')},
            {'min_weight_grams': 1500, 'max_weight_grams': None, 'rate': Decimal('119.00'), 'free_above_order_value': Decimal('1499.00')},
        ],
    },
    {
        'name': 'Mosses & Liverworts',
        'gst_percentage': Decimal('5.00'),
        'commission_rate': Decimal('15.00'),
        'shipping_type': 'plant',
        'subcategories': [
            {'name': 'Mosses',     'gst_percentage': None,         'commission_rate': None},
            {'name': 'Liverworts', 'gst_percentage': None,         'commission_rate': None},
            {'name': 'Riccia',     'gst_percentage': Decimal('5.00'), 'commission_rate': None},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,   'max_weight_grams': 300,  'rate': Decimal('39.00'), 'free_above_order_value': Decimal('499.00')},
            {'min_weight_grams': 300, 'max_weight_grams': None, 'rate': Decimal('69.00'), 'free_above_order_value': Decimal('799.00')},
        ],
    },
    {
        'name': 'Rare Ferns',
        'gst_percentage': Decimal('12.00'),
        'commission_rate': Decimal('18.00'),
        'shipping_type': 'plant',
        'subcategories': [
            {'name': 'Aquatic Ferns',    'gst_percentage': None, 'commission_rate': None},
            {'name': 'Terrestrial Ferns','gst_percentage': None, 'commission_rate': None},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,    'max_weight_grams': 500,  'rate': Decimal('59.00'),  'free_above_order_value': Decimal('999.00')},
            {'min_weight_grams': 500,  'max_weight_grams': None, 'rate': Decimal('99.00'),  'free_above_order_value': Decimal('1999.00')},
        ],
    },
    {
        'name': 'Hardscape & Substrate',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('12.00'),
        'shipping_type': 'heavy',
        'subcategories': [
            {'name': 'Driftwood',     'gst_percentage': Decimal('18.00'), 'commission_rate': None},
            {'name': 'Rocks & Stones','gst_percentage': Decimal('18.00'), 'commission_rate': None},
            {'name': 'Aqua Substrate','gst_percentage': Decimal('18.00'), 'commission_rate': Decimal('10.00')},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,    'max_weight_grams': 1000, 'rate': Decimal('99.00'),  'free_above_order_value': Decimal('1499.00')},
            {'min_weight_grams': 1000, 'max_weight_grams': 3000, 'rate': Decimal('149.00'), 'free_above_order_value': Decimal('2499.00')},
            {'min_weight_grams': 3000, 'max_weight_grams': None, 'rate': Decimal('199.00'), 'free_above_order_value': None},
        ],
    },
    {
        'name': 'Live Food & Feeders',
        'gst_percentage': Decimal('5.00'),
        'commission_rate': Decimal('12.00'),
        'shipping_type': 'plant',
        'subcategories': [
            {'name': 'Daphnia & Moina',  'gst_percentage': None, 'commission_rate': None},
            {'name': 'Brine Shrimp',     'gst_percentage': None, 'commission_rate': None},
            {'name': 'Micro Worms',      'gst_percentage': None, 'commission_rate': None},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,   'max_weight_grams': 200,  'rate': Decimal('49.00'), 'free_above_order_value': Decimal('399.00')},
            {'min_weight_grams': 200, 'max_weight_grams': None, 'rate': Decimal('79.00'), 'free_above_order_value': Decimal('699.00')},
        ],
    },
    {
        'name': 'Accessories & Tools',
        'gst_percentage': Decimal('18.00'),
        'commission_rate': Decimal('10.00'),
        'shipping_type': 'accessory',
        'subcategories': [
            {'name': 'Fertilizers',       'gst_percentage': Decimal('18.00'), 'commission_rate': None},
            {'name': 'CO2 Equipment',     'gst_percentage': Decimal('18.00'), 'commission_rate': None},
            {'name': 'Lights & Filters',  'gst_percentage': Decimal('18.00'), 'commission_rate': None},
            {'name': 'Tools & Scissors',  'gst_percentage': Decimal('12.00'), 'commission_rate': None},
        ],
        'shipping_rates': [
            {'min_weight_grams': 0,    'max_weight_grams': 500,  'rate': Decimal('79.00'),  'free_above_order_value': Decimal('999.00')},
            {'min_weight_grams': 500,  'max_weight_grams': 2000, 'rate': Decimal('119.00'), 'free_above_order_value': Decimal('1999.00')},
            {'min_weight_grams': 2000, 'max_weight_grams': None, 'rate': Decimal('169.00'), 'free_above_order_value': None},
        ],
    },
]

# 10 new products per seller — wide variety of categories, care levels, price ranges
PRODUCT_TEMPLATES = [
    # Aquatic Plants / Stem Plants
    {
        'name': 'Rotala Macrandra',
        'tagline': 'Vibrant red stem with demanding care needs',
        'scientific_name': 'Rotala macrandra',
        'category': 'Aquatic Plants', 'subcategory': 'Stem Plants',
        'care_level': 'Advanced', 'light_requirements': 'High',
        'growth_rate': 'Fast', 'co2_requirement': 'High',
        'water_temperature': '22–28°C', 'ph_range': '6.0–7.5',
        'origin': 'India', 'is_rare': True,
        'variants': [
            {'name': 'Single Stem', 'base_price': Decimal('120'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 30, 'packed_weight_grams': 80, 'item_category': 'light'},
            {'name': '5 Stems Bundle', 'base_price': Decimal('550'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 15, 'packed_weight_grams': 200, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1518568814500-bf0f8d125f46?w=600',
    },
    # Aquatic Plants / Carpeting Plants
    {
        'name': 'Hemianthus Callitrichoides (HC Cuba)',
        'tagline': 'The finest carpeting plant for aquascapes',
        'scientific_name': 'Hemianthus callitrichoides',
        'category': 'Aquatic Plants', 'subcategory': 'Carpeting Plants',
        'care_level': 'Advanced', 'light_requirements': 'High',
        'growth_rate': 'Slow', 'co2_requirement': 'High',
        'water_temperature': '20–26°C', 'ph_range': '5.5–7.0',
        'origin': 'Cuba', 'is_rare': True,
        'variants': [
            {'name': '5×5cm Portion', 'base_price': Decimal('199'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 20, 'packed_weight_grams': 60, 'item_category': 'light'},
            {'name': '10×10cm Portion', 'base_price': Decimal('349'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 10, 'packed_weight_grams': 120, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1543163521-1bf539c55dd2?w=600',
    },
    # Aquatic Plants / Epiphytes
    {
        'name': 'Bucephalandra Mini Coin',
        'tagline': 'Compact epiphyte with shimmering leaves',
        'scientific_name': 'Bucephalandra sp. Mini Coin',
        'category': 'Aquatic Plants', 'subcategory': 'Epiphytes',
        'care_level': 'Easy', 'light_requirements': 'Low',
        'growth_rate': 'Slow', 'co2_requirement': 'Low',
        'water_temperature': '22–28°C', 'ph_range': '6.0–7.5',
        'origin': 'Borneo', 'is_rare': True,
        'variants': [
            {'name': 'Rhizome Cutting', 'base_price': Decimal('280'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 12, 'packed_weight_grams': 90, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1585687433888-21a0b0cac5db?w=600',
    },
    # Aquatic Plants / Rosette Plants
    {
        'name': 'Echinodorus Ozelot Red',
        'tagline': 'Spotted rosette with deep red coloration',
        'scientific_name': 'Echinodorus ozelot',
        'category': 'Aquatic Plants', 'subcategory': 'Rosette Plants',
        'care_level': 'Easy', 'light_requirements': 'Medium',
        'growth_rate': 'Moderate', 'co2_requirement': 'Low',
        'water_temperature': '20–28°C', 'ph_range': '6.5–7.5',
        'origin': 'South America', 'is_rare': False,
        'variants': [
            {'name': 'Small Plant', 'base_price': Decimal('150'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 25, 'packed_weight_grams': 120, 'item_category': 'light'},
            {'name': 'Large Plant', 'base_price': Decimal('299'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 10, 'packed_weight_grams': 220, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1476231682828-37e571bc172f?w=600',
    },
    # Mosses / Mosses
    {
        'name': 'Christmas Moss',
        'tagline': 'Branching fronds resembling fir tree branches',
        'scientific_name': 'Vesicularia montagnei',
        'category': 'Mosses & Liverworts', 'subcategory': 'Mosses',
        'care_level': 'Easy', 'light_requirements': 'Low',
        'growth_rate': 'Moderate', 'co2_requirement': 'Low',
        'water_temperature': '18–28°C', 'ph_range': '6.0–7.5',
        'origin': 'Asia', 'is_rare': False,
        'variants': [
            {'name': 'Golf Ball Portion', 'base_price': Decimal('99'), 'gst_rate': Decimal('5'), 'commission_rate': Decimal('15'), 'stock': 40, 'packed_weight_grams': 50, 'item_category': 'light'},
            {'name': 'Fist-Size Portion', 'base_price': Decimal('179'), 'gst_rate': Decimal('5'), 'commission_rate': Decimal('15'), 'stock': 20, 'packed_weight_grams': 100, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1523348837708-15d4a09cfac2?w=600',
    },
    # Rare Ferns / Aquatic Ferns
    {
        'name': 'Bolbitis Difformis Baby Leaf',
        'tagline': 'Miniature aquatic fern with delicate texture',
        'scientific_name': 'Bolbitis difformis',
        'category': 'Rare Ferns', 'subcategory': 'Aquatic Ferns',
        'care_level': 'Medium', 'light_requirements': 'Medium',
        'growth_rate': 'Slow', 'co2_requirement': 'Medium',
        'water_temperature': '20–28°C', 'ph_range': '6.0–7.5',
        'origin': 'Southeast Asia', 'is_rare': True,
        'variants': [
            {'name': 'Single Rhizome', 'base_price': Decimal('220'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('18'), 'stock': 15, 'packed_weight_grams': 90, 'item_category': 'light'},
            {'name': '3 Rhizome Pack', 'base_price': Decimal('580'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('18'), 'stock': 8, 'packed_weight_grams': 200, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1501004318641-b39e6451bec6?w=600',
    },
    # Hardscape / Driftwood
    {
        'name': 'Spider Wood — Aquascape Grade',
        'tagline': 'Intricately branched wood for natural layouts',
        'scientific_name': None,
        'category': 'Hardscape & Substrate', 'subcategory': 'Driftwood',
        'care_level': 'Easy', 'light_requirements': 'Low',
        'growth_rate': 'Slow', 'co2_requirement': 'Low',
        'water_temperature': None, 'ph_range': None,
        'origin': 'South Asia', 'is_rare': False,
        'variants': [
            {'name': 'Small (15–25cm)', 'base_price': Decimal('249'), 'gst_rate': Decimal('18'), 'commission_rate': Decimal('12'), 'stock': 20, 'packed_weight_grams': 800, 'item_category': 'heavy', 'length': 30, 'width': 20, 'height': 15},
            {'name': 'Medium (25–40cm)', 'base_price': Decimal('499'), 'gst_rate': Decimal('18'), 'commission_rate': Decimal('12'), 'stock': 10, 'packed_weight_grams': 1500, 'item_category': 'heavy', 'length': 45, 'width': 30, 'height': 20},
        ],
        'image_url': 'https://images.unsplash.com/photo-1441974231531-c6227db76b6e?w=600',
    },
    # Accessories / Fertilizers
    {
        'name': 'Aqua Flourish Pro — Macro Fertilizer',
        'tagline': 'Complete macro nutrients for demanding stem plants',
        'scientific_name': None,
        'category': 'Accessories & Tools', 'subcategory': 'Fertilizers',
        'care_level': 'Easy', 'light_requirements': 'Medium',
        'growth_rate': 'Fast', 'co2_requirement': 'Low',
        'water_temperature': None, 'ph_range': None,
        'origin': 'India', 'is_rare': False,
        'variants': [
            {'name': '250ml Bottle', 'base_price': Decimal('349'), 'gst_rate': Decimal('18'), 'commission_rate': Decimal('10'), 'stock': 50, 'packed_weight_grams': 400, 'item_category': 'heavy'},
            {'name': '500ml Bottle', 'base_price': Decimal('599'), 'gst_rate': Decimal('18'), 'commission_rate': Decimal('10'), 'stock': 30, 'packed_weight_grams': 700, 'item_category': 'heavy'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1516253593875-bd7ba052fbc5?w=600',
    },
    # Live Food
    {
        'name': 'Live Daphnia Magna Culture',
        'tagline': 'High-protein live food for nano fish and bettas',
        'scientific_name': 'Daphnia magna',
        'category': 'Live Food & Feeders', 'subcategory': 'Daphnia & Moina',
        'care_level': 'Medium', 'light_requirements': 'Low',
        'growth_rate': 'Fast', 'co2_requirement': 'Low',
        'water_temperature': '18–26°C', 'ph_range': '7.0–8.0',
        'origin': 'India', 'is_rare': False,
        'variants': [
            {'name': '250ml Bag',  'base_price': Decimal('79'),  'gst_rate': Decimal('5'), 'commission_rate': Decimal('12'), 'stock': 60, 'packed_weight_grams': 350, 'item_category': 'light'},
            {'name': '500ml Bag',  'base_price': Decimal('139'), 'gst_rate': Decimal('5'), 'commission_rate': Decimal('12'), 'stock': 40, 'packed_weight_grams': 600, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1559827291-72ee739d0d9a?w=600',
    },
    # Aquatic Plants / Floating
    {
        'name': 'Salvinia Minima',
        'tagline': 'Fast-growing floating cover for shrimp tanks',
        'scientific_name': 'Salvinia minima',
        'category': 'Aquatic Plants', 'subcategory': 'Floating Plants',
        'care_level': 'Easy', 'light_requirements': 'Medium',
        'growth_rate': 'Fast', 'co2_requirement': 'Low',
        'water_temperature': '18–30°C', 'ph_range': '6.0–8.0',
        'origin': 'South America', 'is_rare': False,
        'variants': [
            {'name': 'Cup Portion', 'base_price': Decimal('69'),  'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 80, 'packed_weight_grams': 60, 'item_category': 'light'},
            {'name': 'Large Bag',   'base_price': Decimal('149'), 'gst_rate': Decimal('12'), 'commission_rate': Decimal('15'), 'stock': 50, 'packed_weight_grams': 150, 'item_category': 'light'},
        ],
        'image_url': 'https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=600',
    },
]


class Command(BaseCommand):
    help = 'Seed categories, subcategories, shipping rates, and 10 new products per seller'

    def add_arguments(self, parser):
        parser.add_argument('--clear-products', action='store_true',
                            help='Delete all existing seed products before re-seeding')

    @transaction.atomic
    def handle(self, *args, **options):
        from core.models import (
            Category, SubCategory, CategoryShippingRate,
            Product, ProductVariant, ProductImage
        )
        from core.models import User

        # ── 1. Categories & Subcategories ────────────────────────────────────
        self.stdout.write('Seeding categories...')
        cat_objs = {}
        subcat_objs = {}

        for cat_data in CATEGORIES_DATA:
            cat, created = Category.objects.get_or_create(
                slug=slugify(cat_data['name']),
                defaults={
                    'name': cat_data['name'],
                    'gst_percentage': cat_data['gst_percentage'],
                    'commission_rate': cat_data['commission_rate'],
                    'shipping_type': cat_data['shipping_type'],
                }
            )
            if not created:
                # Update GST/commission in case they changed
                cat.gst_percentage = cat_data['gst_percentage']
                cat.commission_rate = cat_data['commission_rate']
                cat.shipping_type = cat_data['shipping_type']
                cat.save()
            cat_objs[cat_data['name']] = cat
            action = 'Created' if created else 'Updated'
            self.stdout.write(f'  {action} category: {cat.name}')

            # Subcategories
            for sub_data in cat_data.get('subcategories', []):
                slug = slugify(f"{cat_data['name']}-{sub_data['name']}")
                sub, sub_created = SubCategory.objects.get_or_create(
                    slug=slug,
                    defaults={
                        'category': cat,
                        'name': sub_data['name'],
                        'gst_percentage': sub_data['gst_percentage'],
                        'commission_rate': sub_data['commission_rate'],
                    }
                )
                if not sub_created:
                    sub.gst_percentage = sub_data['gst_percentage']
                    sub.commission_rate = sub_data['commission_rate']
                    sub.save()
                subcat_objs[f"{cat_data['name']}|{sub_data['name']}"] = sub
                sub_action = 'Created' if sub_created else 'Updated'
                self.stdout.write(f'    {sub_action} subcategory: {sub.name}')

            # Shipping rates for this category
            for rate_data in cat_data.get('shipping_rates', []):
                rate, rate_created = CategoryShippingRate.objects.get_or_create(
                    category=cat,
                    sub_category=None,
                    min_weight_grams=rate_data['min_weight_grams'],
                    max_weight_grams=rate_data['max_weight_grams'],
                    defaults={
                        'rate': rate_data['rate'],
                        'free_above_order_value': rate_data.get('free_above_order_value'),
                    }
                )
                if not rate_created:
                    rate.rate = rate_data['rate']
                    rate.free_above_order_value = rate_data.get('free_above_order_value')
                    rate.save()

        # ── 2. Products per seller ───────────────────────────────────────────
        self.stdout.write('\nSeeding products...')
        sellers = User.objects.filter(role='grower', is_deleted=False)

        if not sellers.exists():
            self.stdout.write(self.style.WARNING('No grower sellers found. Skipping products.'))
            return

        total_created = 0
        for seller in sellers:
            seller_label = seller.email.split('@')[0]
            created_count = 0

            for tmpl in PRODUCT_TEMPLATES:
                product_name = f"{tmpl['name']} — {seller_label}"

                if options['clear_products']:
                    Product.objects.filter(name=product_name, seller=seller).delete()

                if Product.objects.filter(name=product_name, seller=seller).exists():
                    self.stdout.write(f'  [SKIP] {product_name}')
                    continue

                # Resolve category + subcategory
                cat_obj = cat_objs.get(tmpl['category'])
                sub_obj = subcat_objs.get(f"{tmpl['category']}|{tmpl['subcategory']}")

                # Build unique slug
                base_slug = slugify(product_name)
                slug = base_slug
                counter = 1
                while Product.objects.filter(slug=slug).exists():
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                product = Product.objects.create(
                    name=product_name,
                    slug=slug,
                    seller=seller,
                    tagline=tmpl['tagline'],
                    description=(
                        f"{tmpl['tagline']}. {tmpl['name']} is a popular choice among aquarists "
                        f"originating from {tmpl.get('origin', 'various regions')}. "
                        f"Suitable for {tmpl['care_level'].lower()}-level keepers."
                    ),
                    scientific_name=tmpl.get('scientific_name') or '',
                    care_level=tmpl['care_level'],
                    light_requirements=tmpl['light_requirements'],
                    growth_rate=tmpl['growth_rate'],
                    co2_requirement=tmpl['co2_requirement'],
                    water_temperature=tmpl.get('water_temperature') or '',
                    ph_range=tmpl.get('ph_range') or '',
                    origin=tmpl.get('origin') or '',
                    is_rare=tmpl.get('is_rare', False),
                    is_active=True,
                )

                if sub_obj:
                    product.sub_categories.set([sub_obj])

                if cat_obj:
                    product.categories.add(cat_obj)
                if sub_obj and sub_obj.category not in product.categories.all():
                    product.categories.add(sub_obj.category)

                # Variants
                for v_tmpl in tmpl['variants']:
                    ProductVariant.objects.create(
                        product=product,
                        name=v_tmpl['name'],
                        base_price=v_tmpl['base_price'],
                        gst_rate=v_tmpl['gst_rate'],
                        commission_rate=v_tmpl['commission_rate'],
                        stock=v_tmpl['stock'],
                        packed_weight_grams=v_tmpl.get('packed_weight_grams', 200),
                        item_category=v_tmpl.get('item_category', 'light'),
                        length=v_tmpl.get('length', 15),
                        width=v_tmpl.get('width', 10),
                        height=v_tmpl.get('height', 8),
                        weight=Decimal(str(v_tmpl.get('packed_weight_grams', 200))) / Decimal('1000'),
                    )

                # Image
                if tmpl.get('image_url'):
                    ProductImage.objects.create(
                        product=product,
                        image_url=tmpl['image_url'],
                        is_primary=True,
                        order=0,
                    )

                created_count += 1
                self.stdout.write(f'  [OK] {product_name}')

            total_created += created_count
            self.stdout.write(
                self.style.SUCCESS(f'  → {created_count} products created for {seller.email}')
            )

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {total_created} products created across {sellers.count()} sellers.'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'Categories: {len(cat_objs)} | Subcategories: {len(subcat_objs)}'
        ))
