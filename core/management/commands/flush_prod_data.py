"""
Management command: flush_prod_data
Wipes all transactional and dummy data from the database, keeping:
  - Superusers (is_superuser=True)
  - Admin-role users (role='admin')
  - Categories and subcategories (configuration, not dummy data)
  - Category shipping rates

Use this to clean up a staging/production database that has dummy data
before going live.

Usage:
    python manage.py flush_prod_data --confirm
    python manage.py flush_prod_data --dry-run
    python manage.py flush_prod_data --confirm --keep-categories
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = 'Wipe all dummy/transactional data, preserving admin accounts and category config'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Required flag to actually execute the wipe (safety gate)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be deleted without touching the database',
        )
        parser.add_argument(
            '--keep-categories',
            action='store_true',
            help='Preserve categories, subcategories, and shipping rates (skip wiping them)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        wipe_categories = not options['keep_categories']

        if not dry_run and not confirm:
            self.stdout.write(self.style.ERROR(
                'Safety gate: pass --confirm to actually run, or --dry-run to preview.'
            ))
            return

        mode = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.WARNING(
            f'{mode}flush_prod_data — wiping dummy data…'
        ))

        with transaction.atomic():
            self._flush(dry_run, wipe_categories)

            if dry_run:
                # Roll back the transaction so nothing is actually deleted
                transaction.set_rollback(True)
                self.stdout.write(self.style.SUCCESS('\n[DRY RUN] No changes committed.'))
            else:
                self.stdout.write(self.style.SUCCESS('\nDone. Database is clean.'))

    def _flush(self, dry_run, wipe_categories):
        # Import inside to avoid early AppRegistry errors
        from core.models import (
            User, Product, ProductVariant, ProductImage, ProductReview,
            WishlistItem, Tag,
        )
        from sellers.models import SellerProfile, AllowedSeller
        from orders.models import Order, SubOrder, OrderItem
        from payments.models import Payment
        from cart.models import Cart, CartItem
        from shipping.models import ShippingAddress, Shipment
        from notifications.models import AppNotification
        from analytics.models import EventLog

        # ── Analytics & notifications ────────────────────────────────────────
        self._wipe_all(EventLog, 'EventLog', dry_run)
        self._wipe_all(AppNotification, 'AppNotification', dry_run)

        # ── Cart ─────────────────────────────────────────────────────────────
        self._wipe_all(CartItem, 'CartItem', dry_run)
        self._wipe_all(Cart, 'Cart', dry_run)

        # ── Wishlist ─────────────────────────────────────────────────────────
        self._wipe_all(WishlistItem, 'WishlistItem', dry_run)

        # ── Shipping ─────────────────────────────────────────────────────────
        self._wipe_all(Shipment, 'Shipment', dry_run)
        self._wipe_all(ShippingAddress, 'ShippingAddress', dry_run)

        # ── Payments ─────────────────────────────────────────────────────────
        self._wipe_all(Payment, 'Payment', dry_run)

        # ── Orders ───────────────────────────────────────────────────────────
        self._wipe_all(OrderItem, 'OrderItem', dry_run)
        self._wipe_all(SubOrder, 'SubOrder', dry_run)
        self._wipe_all(Order, 'Order', dry_run)

        # ── Products ─────────────────────────────────────────────────────────
        self._wipe_all(ProductReview, 'ProductReview', dry_run)
        self._wipe_all(ProductImage, 'ProductImage', dry_run)
        self._wipe_all(ProductVariant, 'ProductVariant', dry_run)
        # Clear M2M through tables before deleting products
        if not dry_run:
            for p in Product.all_objects.all():
                p.categories.clear()
                p.tags.clear()
        self._wipe_all(Product, 'Product', dry_run)
        self._wipe_all(Tag, 'Tag', dry_run)

        # ── Sellers ──────────────────────────────────────────────────────────
        self._wipe_all(AllowedSeller, 'AllowedSeller', dry_run)
        self._wipe_all(SellerProfile, 'SellerProfile', dry_run)

        # ── Non-admin users ──────────────────────────────────────────────────
        kept_users = User.objects.filter(is_superuser=True) | User.objects.filter(role='admin')
        kept_ids = list(kept_users.values_list('id', flat=True))
        non_admin_qs = User.all_objects.exclude(id__in=kept_ids)
        count = non_admin_qs.count()
        self.stdout.write(f'  Deleting {count} non-admin users (keeping {len(kept_ids)} admin accounts)')
        if not dry_run:
            non_admin_qs.delete()

        # ── Categories (optional) ────────────────────────────────────────────
        if wipe_categories:
            from core.models import Category, SubCategory, CategoryShippingRate
            self._wipe_all(CategoryShippingRate, 'CategoryShippingRate', dry_run)
            self._wipe_all(SubCategory, 'SubCategory', dry_run)
            self._wipe_all(Category, 'Category', dry_run)
        else:
            self.stdout.write('  Keeping categories, subcategories, and shipping rates (--keep-categories)')

    def _wipe(self, model, label, dry_run):
        """Wipe records via the default manager (respects soft-delete where present)."""
        try:
            qs = model.objects.all()
        except AttributeError:
            qs = model._default_manager.all()
        count = qs.count()
        self.stdout.write(f'  Deleting {count} {label} records')
        if not dry_run:
            qs.delete()

    def _wipe_all(self, model, label, dry_run):
        """Wipe ALL records including soft-deleted ones via all_objects manager."""
        try:
            qs = model.all_objects.all()
        except AttributeError:
            qs = model._default_manager.all()
        count = qs.count()
        self.stdout.write(f'  Deleting {count} {label} records (incl. soft-deleted)')
        if not dry_run:
            qs.delete()
