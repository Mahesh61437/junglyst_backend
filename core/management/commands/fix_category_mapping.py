"""
fix_category_mapping.py

One-time command to correct category mis-assignments introduced during scraping.

Problems fixed:
  1. CO2 products sitting in "Aquascaping" moved to "Planted Aquarium CO2"
  2. Cooling fan products sitting in "Aquascaping"/"Aquarium Filters" moved to "Cooling Systems"
  3. Brand products (Seachem, API, Tetra, JBL, Chihiros, ADA…) get "Brand & Specialized" added
  4. Furniture/setup products get "Furniture & Setup" added

Run with --dry-run first to preview changes before committing.

Usage:
    python manage.py fix_category_mapping --dry-run
    python manage.py fix_category_mapping
"""

from django.core.management.base import BaseCommand
from core.models import Product, Category


# Keywords that identify CO2 products regardless of their current category
CO2_KEYWORDS = [
    'co2', 'cylinder', 'diffuser', 'regulator', 'solenoid',
    'bubble counter', 'drop checker', 'diy co2', 'co2 generator',
    'co2 splitter', 'co2 connector', 'co2 indicator', 'co2 tablet',
    'bubble counter', 'co2 checker',
]

# Keywords that identify cooling equipment
COOLING_KEYWORDS = [
    'cooling fan', 'aquarium fan', 'chiller', 'temperature controller',
    'tank fan', 'water chiller',
]

# Keywords that identify furniture/setup items
FURNITURE_KEYWORDS = [
    'aquarium stand', 'aquarium cabinet', 'fish tank stand',
    'tank stand', 'tank cabinet', 'aquarium rack',
]

# Brands matched only at the START of the product name (safe — unambiguous prefixes)
BRAND_PREFIXES = [
    'seachem', 'api ', 'tetra ', 'jbl ', 'chihiros', 'ada ',
    'fluval', 'eheim', 'juwel', 'boyu', 'sunsun', 'aquael',
    'oase', 'dennerle', 'sera ', 'aqua medic', 'hikari',
]

# Brands matched anywhere in the name (clearly distinctive, not substrings of common words)
BRAND_ANYWHERE = [
    'sunsun', 'chihiros', 'seachem', 'eheim', 'fluval',
]


def _matches(name_lower: str, keywords: list[str]) -> bool:
    return any(kw in name_lower for kw in keywords)


def _is_brand(name_lower: str) -> bool:
    if any(name_lower.startswith(p) for p in BRAND_PREFIXES):
        return True
    if any(f' {b}' in name_lower for b in BRAND_ANYWHERE):
        return True
    return False


class Command(BaseCommand):
    help = 'Fix mis-mapped product categories from the initial scrape'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without saving anything to the database',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no changes will be saved\n'))

        cats = {c.name: c for c in Category.objects.all()}
        co2_cat       = cats['Planted Aquarium CO2']
        cooling_cat   = cats['Cooling Systems']
        furniture_cat = cats['Furniture & Setup']
        brand_cat     = cats['Brand & Specialized']
        aquascaping   = cats['Aquascaping']

        all_products = list(
            Product.objects
            .prefetch_related('categories')
            .filter(is_active=True)
        )

        co2_moved      = []
        cooling_moved  = []
        furniture_added = []
        brand_added    = []

        for p in all_products:
            current_cats = {c.id for c in p.categories.all()}
            name = p.name.lower()

            # ── Rule 1: CO2 products ──────────────────────────────────────────
            # Move from Aquascaping to Planted Aquarium CO2
            if _matches(name, CO2_KEYWORDS):
                if aquascaping.id in current_cats and co2_cat.id not in current_cats:
                    co2_moved.append(p)
                    if not dry_run:
                        p.categories.remove(aquascaping)
                        p.categories.add(co2_cat)

            # ── Rule 2: Cooling products ──────────────────────────────────────
            # Replace whatever category they're in with Cooling Systems
            elif _matches(name, COOLING_KEYWORDS):
                if cooling_cat.id not in current_cats:
                    cooling_moved.append(p)
                    if not dry_run:
                        p.categories.clear()
                        p.categories.add(cooling_cat)

            # ── Rule 3: Furniture products ────────────────────────────────────
            elif _matches(name, FURNITURE_KEYWORDS):
                if furniture_cat.id not in current_cats:
                    furniture_added.append(p)
                    if not dry_run:
                        p.categories.add(furniture_cat)

            # ── Rule 4: Brand products ────────────────────────────────────────
            if _is_brand(name):
                if brand_cat.id not in current_cats:
                    brand_added.append(p)
                    if not dry_run:
                        p.categories.add(brand_cat)

        # ── Report ────────────────────────────────────────────────────────────
        self._report('CO2 products: Aquascaping -> Planted Aquarium CO2', co2_moved)
        self._report('Cooling products: moved -> Cooling Systems', cooling_moved)
        self._report('Furniture products: added Furniture & Setup', furniture_added)
        self._report('Brand products: added Brand & Specialized', brand_added)

        total = len(co2_moved) + len(cooling_moved) + len(furniture_added) + len(brand_added)
        action = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'\n{action} {total} product(category) records.'))

        if not dry_run:
            # Bust the Redis feed cache so the shop reflects the new mappings
            try:
                from core.feed import invalidate_feed_cache
                invalidate_feed_cache()
                self.stdout.write('Feed cache invalidated.')
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Could not invalidate feed cache: {e}'))

    def _report(self, title: str, products: list):
        self.stdout.write(f'\n{title} ({len(products)}):')
        for p in products:
            self.stdout.write(f'  {p.name[:70]}')
