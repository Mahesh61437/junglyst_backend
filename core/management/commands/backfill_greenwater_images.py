"""
Management command: backfill_greenwater_images

Attaches images to already-imported Green Water Aquascapes products. Reads the
scraped sheet (greenwater_products.json), matches each record to the existing
ProductVariant by SKU, downloads the image from GWA, uploads it to Junglyst
Firebase, and creates the ProductImage rows.

Touches IMAGES ONLY — never modifies product fields, prices, or stock.

Why this exists
---------------
The original import created 210 products with zero images because a malformed
FIREBASE_STORAGE_BUCKET value made every upload fail silently. With the bucket
fixed, this backfills the missing images without re-importing anything.

Usage
-----
    # preview — writes nothing
    python manage.py backfill_greenwater_images --input ../scratch/greenwater_products.json --dry-run

    # real run (set the env prefix so uploads land under prod/ not dev/)
    RAILWAY_ENVIRONMENT_NAME=production \\
        python manage.py backfill_greenwater_images --input ../scratch/greenwater_products.json

    # re-do images for products that already have some
    ... --force
"""
from __future__ import annotations

import io
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

REQUEST_TIMEOUT = 30
RETRY_DELAY = 2
DEFAULT_SELLER_SLUG = "green-water-aquascapes"


def _download_image(url: str, retries: int = 3) -> Optional[tuple[bytes, str]]:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Junglyst-Importer/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read(), (resp.headers.get_content_type() or "image/jpeg")
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(RETRY_DELAY)
    return None


class _ImageFile:
    def __init__(self, data: bytes, filename: str, content_type: str):
        self._buf = io.BytesIO(data)
        self.name = filename
        self.content_type = content_type

    def read(self, *a):
        return self._buf.read(*a)

    def seek(self, *a):
        return self._buf.seek(*a)

    def tell(self):
        return self._buf.tell()


def _ext_from_url(url: str, content_type: str) -> str:
    last = urlparse(url).path.rsplit("/", 1)[-1]
    if "." in last:
        return last.rsplit(".", 1)[-1].lower().split("?")[0]
    return (mimetypes.guess_extension(content_type) or ".jpg").lstrip(".")


class Command(BaseCommand):
    help = "Backfill images for imported Green Water Aquascapes products (images only)."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True,
                            help="Path to greenwater_products.json.")
        parser.add_argument("--seller-slug", default=DEFAULT_SELLER_SLUG,
                            help=f"Seller store slug (default: {DEFAULT_SELLER_SLUG}).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen; write nothing.")
        parser.add_argument("--force", action="store_true",
                            help="Re-upload even if the product already has images.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Process at most N records (0 = all).")

    def handle(self, *args, **opts):
        from core.models import Product, ProductImage, ProductVariant
        from sellers.models import SellerProfile

        dry_run = opts["dry_run"]
        force = opts["force"]
        limit = opts["limit"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing will be written."))

        path = Path(opts["input"])
        if not path.exists():
            raise CommandError(f"Input file not found: {path.resolve()}")
        records = json.loads(path.read_text(encoding="utf-8"))
        if limit:
            records = records[:limit]

        sp = SellerProfile.objects.filter(slug=opts["seller_slug"]).first()
        if not sp:
            raise CommandError(f"No seller with slug '{opts['seller_slug']}'.")
        seller = sp.user
        self.stdout.write(f"Seller  : {seller.email}  (slug={opts['seller_slug']})")
        self.stdout.write(f"Records : {len(records)} from {path}")

        # confirm the bucket resolves before doing any work
        if not dry_run:
            from decouple import config
            self.stdout.write(f"Bucket  : {config('FIREBASE_STORAGE_BUCKET')}")

        done = skipped_have = no_variant = no_url = img_ok = img_fail = 0

        for idx, rec in enumerate(records, 1):
            sku = (rec.get("external_product_id") or "").strip()
            urls = [rec.get(f"image_url_{i}") for i in (1, 2, 3)]
            urls = [u for u in urls if u]

            variant = ProductVariant.all_objects.filter(sku=sku, product__seller=seller).first()
            if not variant:
                no_variant += 1
                continue
            product = variant.product

            if not urls:
                no_url += 1
                continue

            has_images = ProductImage.all_objects.filter(product=product).exists()
            if has_images and not force:
                skipped_have += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"  [{idx}/{len(records)}] WOULD ADD {len(urls)} img  {sku:12} {product.name[:44]}")
                done += 1
                continue

            uploaded = self._attach_images(product, variant, urls, seller.id,
                                            ProductImage, force)
            img_ok += uploaded["ok"]
            img_fail += uploaded["fail"]
            if uploaded["ok"]:
                done += 1
                self.stdout.write(
                    f"  [{idx}/{len(records)}] +{uploaded['ok']} img  {sku:12} {product.name[:44]}")
            else:
                self.stderr.write(self.style.WARNING(
                    f"  [{idx}/{len(records)}] 0 img (all failed)  {sku}  {product.name[:40]}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("─" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"Done. products_updated={done} images_uploaded={img_ok} img_failed={img_fail}"))
        self.stdout.write(
            f"Skipped: already_had_images={skipped_have} no_source_image={no_url} "
            f"variant_not_found={no_variant}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was saved."))

    def _attach_images(self, product, variant, urls, seller_id, ProductImage, force):
        from django.db import transaction
        from core.storage import upload_to_firebase

        ok = fail = 0
        with transaction.atomic():
            if force:
                ProductImage.all_objects.filter(product=product).delete()
            for order, src in enumerate(urls):
                dl = _download_image(src)
                if not dl:
                    fail += 1
                    continue
                data, content_type = dl
                ext = _ext_from_url(src, content_type)
                try:
                    public_url = upload_to_firebase(
                        _ImageFile(data, f"product.{ext}", content_type),
                        str(seller_id), "product")
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f"      upload failed: {exc}"))
                    fail += 1
                    continue
                ProductImage.objects.create(
                    product=product, variant=variant, image_url=public_url,
                    is_primary=(order == 0), order=order)
                ok += 1
        return {"ok": ok, "fail": fail}
