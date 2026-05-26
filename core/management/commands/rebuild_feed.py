"""
Management command: rebuild the shop product feed cache.

Run after deploy or to force a fresh shuffle:
    python manage.py rebuild_feed
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Rebuild the seller-fair product feed + pre-warm all category/sub_category filter caches'

    def handle(self, *args, **options):
        from core.feed import invalidate_feed_cache, get_ordered_product_ids, prewarm_filter_caches
        from collections import Counter
        from core.models import Product

        self.stdout.write('Rebuilding feed...')
        invalidate_feed_cache()
        ids = get_ordered_product_ids()

        self.stdout.write('Pre-warming category and sub_category filter caches...')
        summary = prewarm_filter_caches(ids)

        # Seller distribution check (first 60 slots)
        id_to_seller = dict(
            Product.objects.filter(id__in=ids[:60]).values_list('id', 'seller__username')
        )
        dist = Counter(id_to_seller.get(i, '?') for i in ids[:60])

        self.stdout.write(self.style.SUCCESS(
            f'\nDone — {len(ids)} products | '
            f'{summary["categories"]} categories | '
            f'{summary["subcategories"]} sub_categories pre-warmed\n'
        ))
        self.stdout.write('Seller distribution in first 60 slots:')
        for seller, count in sorted(dist.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {seller:35s}  {count}')
