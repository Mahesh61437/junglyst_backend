"""
Management command: fix_product_slugs

Re-generates slugs for products whose slug is an opaque ae-{id} key
rather than a readable name-based slug.

Uses the same logic as Product.save() — seller-based disambiguation first,
numeric fallback second.

Usage:
    python manage.py fix_product_slugs            # preview only (dry run)
    python manage.py fix_product_slugs --apply    # write changes
"""
from django.core.management.base import BaseCommand
from django.utils.text import slugify


class Command(BaseCommand):
    help = "Re-generate SEO-friendly slugs for products that still use ae-{id} slugs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes to the database (omit for a dry-run preview).",
        )

    def handle(self, *args, **options):
        from core.models import Product

        apply = options["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN — pass --apply to save changes."))

        targets = (
            Product.all_objects
            .filter(slug__regex=r'^ae-\d+$')
            .select_related('seller__seller_profile')
            .order_by("created_at")
        )
        self.stdout.write(f"Found {targets.count()} product(s) with ae-* slugs.")

        fixed = skipped = 0
        for product in targets:
            if not slugify(product.name):
                self.stdout.write(self.style.WARNING(
                    f"  SKIP  {product.slug!r}  — name {product.name!r} produces empty slug"
                ))
                skipped += 1
                continue

            old_slug = product.slug

            if apply:
                # Clear slug so Product.save() auto-generates it using the same
                # seller-disambiguation logic as all new products
                product.slug = ""
                product.save(update_fields=["slug"])
                self.stdout.write(f"  FIXED  {old_slug!r}  ->  {product.slug!r}  ({product.name})")
            else:
                # Simulate what save() would produce without writing
                base = slugify(product.name)
                qs = Product.all_objects.exclude(pk=product.pk)
                preview = base
                if qs.filter(slug=preview).exists():
                    try:
                        store_slug = product.seller.seller_profile.slug
                        preview = f"{base}-{store_slug}"
                    except Exception:
                        pass
                counter = 1
                candidate = preview
                while qs.filter(slug=candidate).exists():
                    candidate = f"{preview}-{counter}"
                    counter += 1
                self.stdout.write(f"  WOULD FIX  {old_slug!r}  ->  {candidate!r}  ({product.name})")

            fixed += 1

        self.stdout.write("")
        if apply:
            self.stdout.write(self.style.SUCCESS(f"Done. {fixed} slug(s) updated, {skipped} skipped."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Dry run complete. {fixed} would be updated, {skipped} would be skipped."
            ))
            self.stdout.write("Run with --apply to apply changes.")
