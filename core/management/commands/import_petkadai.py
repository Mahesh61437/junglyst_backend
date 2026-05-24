"""
Management command: import_petkadai
====================================
Reads petkadai_aquarium_plants.json (produced by scrape_petkadai.py) and
upserts Products, ProductVariants, and ProductImages into JungLyst.

Images are downloaded from petkadai.com's CDN and re-uploaded to Firebase
Storage so JungLyst is never dependent on the source CDN.

Category mapping
----------------
  Parent  : Aquascaping
  Sub     : mapped per product from sub_category_name field
            (e.g. "TISSUE CULTURE" → "Tissue Culture", "POT" → "Pot Plants")
            SubCategories are auto-created under Aquascaping if absent.

Usage
-----
  python manage.py import_petkadai
  python manage.py import_petkadai --seller-id a617d9c8-df70-45eb-b93a-bfa47433489c
  python manage.py import_petkadai --json-file /path/to/petkadai_aquarium_plants.json
  python manage.py import_petkadai --dry-run
  python manage.py import_petkadai --update
  python manage.py import_petkadai --skip-images
  python manage.py import_petkadai --limit 5
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_JSON = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),   # .../core/management/commands/
        "..", "..", "..",            # → junglyst_backend/
        "..",                        # → E:\JungLyst
        "petkadai_aquarium_plants.json",
    )
)

REQUEST_TIMEOUT   = 30
RETRY_DELAY       = 2

DEFAULT_GST_RATE        = Decimal("12.00")   # standard GST for live plants
DEFAULT_COMMISSION_RATE = Decimal("10.00")   # platform commission
DEFAULT_WEIGHT_KG       = Decimal("0.10")    # TC / bunch plants are very light
DEFAULT_LENGTH_CM       = Decimal("15.0")
DEFAULT_WIDTH_CM        = Decimal("10.0")
DEFAULT_HEIGHT_CM       = Decimal("5.0")
DEFAULT_PACKED_WEIGHT_G = 100               # grams, packed for shipping

JUNGLYST_CATEGORY = "Aquascaping"           # parent category (must exist in DB)

# petkadai sub_category_name → JungLyst SubCategory display name
# Keys are lowercased for case-insensitive matching
SUBCAT_DISPLAY: dict[str, str] = {
    "tissue culture": "Tissue Culture",
    "pot":            "Pot Plants",
    "bunch":          "Bunch Plants",
    "rhizome":        "Rhizome Plants",
    "clump":          "Clump Plants",
    "mat":            "Mat Plants",
    "cup":            "Cup Plants",
    "emersed":        "Emersed Plants",
    "submerged":      "Submerged Plants",
    "cutting":        "Cuttings",
    "moss":           "Mosses",
    "floating":       "Floating Plants",
}


# ── HTTP / image helpers ───────────────────────────────────────────────────────

def _download_image(url: str, retries: int = 3) -> Optional[tuple[bytes, str]]:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Junglyst-Importer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = resp.read()
                ct = resp.headers.get_content_type() or "image/jpeg"
                return data, ct
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(RETRY_DELAY)
    return None


class _ImageFile:
    """Minimal file-like object compatible with Firebase upload helper."""
    def __init__(self, data: bytes, filename: str, content_type: str):
        self._buf = io.BytesIO(data)
        self.name = filename
        self.content_type = content_type

    def read(self, *args):  return self._buf.read(*args)
    def seek(self, *args):  return self._buf.seek(*args)
    def tell(self):         return self._buf.tell()


def _ext_from_url(url: str, content_type: str) -> str:
    path = urlparse(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower().split("?")[0]
    return (mimetypes.guess_extension(content_type) or ".jpg").lstrip(".")


# ── Price helpers ──────────────────────────────────────────────────────────────

def _to_decimal(value) -> Decimal:
    """Convert a raw price value to Decimal, rounded to 2dp."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Slug helpers ───────────────────────────────────────────────────────────────

def _safe_slug(name: str, existing: set[str], max_len: int = 110) -> str:
    """Generate a collision-free slug; updates `existing` in place."""
    base  = (slugify(name) or "product")[:max_len]
    slug  = base
    n     = 1
    while slug in existing:
        suffix = f"-{n}"
        slug   = base[: max_len - len(suffix)] + suffix
        n     += 1
    existing.add(slug)
    return slug


# ── SubCategory resolution ─────────────────────────────────────────────────────

def _resolve_subcat_display(raw_name: str | None) -> str:
    """Map petkadai sub_category_name to a clean JungLyst display name."""
    if not raw_name:
        return "Live Plants"
    lower = raw_name.lower()
    for key, display in SUBCAT_DISPLAY.items():
        if key in lower:
            return display
    # Fallback: title-case the raw name
    return raw_name.strip().title()


# ── Management command ─────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Import aquarium plants scraped from petkadai.com into JungLyst, "
        "re-uploading images to Firebase Storage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seller-id",
            default="a617d9c8-df70-45eb-b93a-bfa47433489c",
            help="UUID of the JungLyst seller to assign products to. "
                 "Default: a617d9c8-df70-45eb-b93a-bfa47433489c (Pet Kadai).",
        )
        parser.add_argument(
            "--json-file",
            default=DEFAULT_JSON,
            help=f"Path to the scraped JSON file. Default: {DEFAULT_JSON}",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate without writing to the database.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Overwrite existing products on re-run (default: skip duplicates).",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Store the petkadai CDN URL directly instead of re-uploading to Firebase.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after this many products (0 = all).",
        )

    # ── Entry point ────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from core.models import (
            Category, Product, ProductImage, ProductVariant, SubCategory, Tag, User,
        )
        from core.storage import upload_to_firebase

        dry_run     : bool = options["dry_run"]
        do_update   : bool = options["update"]
        skip_images : bool = options["skip_images"]
        limit       : int  = options["limit"]
        json_path   : str  = options["json_file"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written.\n"))

        # ── Load JSON ──────────────────────────────────────────────────────────
        if not os.path.exists(json_path):
            raise CommandError(
                f"JSON file not found: {json_path}\n"
                "Run:  python scrape_petkadai.py  to generate it."
            )

        with open(json_path, encoding="utf-8") as fh:
            records: list[dict] = json.load(fh)

        self.stdout.write(f"Loaded {len(records)} records from {json_path}")

        if limit:
            records = records[:limit]
            self.stdout.write(self.style.WARNING(f"  (limited to first {limit})"))

        # ── Seller ─────────────────────────────────────────────────────────────
        seller = self._resolve_seller(User, options["seller_id"])
        self.stdout.write(f"Seller  : {seller.email}  (id={seller.id})")

        # ── Parent category (must exist — run seed_categories first) ───────────
        try:
            cat = Category.objects.get(name=JUNGLYST_CATEGORY)
        except Category.DoesNotExist:
            raise CommandError(
                f"Category '{JUNGLYST_CATEGORY}' not found.\n"
                "Run:  python manage.py seed_categories"
            )
        self.stdout.write(f"Category: {cat.name}\n")

        # ── Pre-fetch all existing slugs to avoid collision queries in loop ────
        existing_slugs: set[str] = set(
            Product.all_objects.values_list("slug", flat=True)
        )

        created = updated = skipped = img_ok = img_fail = errors = 0

        for idx, rec in enumerate(records, start=1):
            product_data : dict = rec.get("product", {})
            variant_data : dict = rec.get("variant", {})
            images_data  : list = rec.get("images", [])
            source_meta  : dict = rec.get("_source", {})

            name = product_data.get("name") or ""
            if not name:
                self.stderr.write(self.style.ERROR(f"  [{idx}] Skipping — no name in record."))
                errors += 1
                continue

            # Collision-safe slug
            slug = _safe_slug(name, existing_slugs)

            try:
                action, n_ok, n_fail = self._import_record(
                    product_data = product_data,
                    variant_data = variant_data,
                    images_data  = images_data,
                    source_meta  = source_meta,
                    slug         = slug,
                    seller       = seller,
                    cat          = cat,
                    do_update    = do_update,
                    skip_images  = skip_images,
                    dry_run      = dry_run,
                    upload_fn    = upload_to_firebase,
                    models       = {
                        "Product"       : Product,
                        "ProductVariant": ProductVariant,
                        "ProductImage"  : ProductImage,
                        "SubCategory"   : SubCategory,
                    },
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(
                    self.style.ERROR(f"  [{idx}/{len(records)}] ERROR  {name[:50]} — {exc}")
                )
                import traceback
                self.stderr.write(traceback.format_exc())
                continue

            img_ok   += n_ok
            img_fail += n_fail
            if   action == "created": created += 1
            elif action == "updated": updated += 1
            else:                     skipped += 1

            label    = {"created": "CREATED", "updated": "UPDATED", "skipped": "SKIPPED"}[action]
            img_note = f"  [{n_ok} img(s)]" if n_ok else ""
            vtype    = variant_data.get("variant_type", "Plant")
            price    = variant_data.get("price", 0)
            self.stdout.write(
                f"  [{idx:>2}/{len(records)}] {label}  "
                f"[{vtype:<16}]  {name[:50]:<50}  Rs.{price}{img_note}"
            )

        # ── Summary ────────────────────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS(
            f"Done.  created={created}  updated={updated}  skipped={skipped}  "
            f"errors={errors}  images_ok={img_ok}  images_failed={img_fail}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was saved."))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _resolve_seller(self, User, seller_id: str):
        if seller_id:
            try:
                return User.objects.get(id=seller_id)
            except User.DoesNotExist:
                raise CommandError(
                    f"No user found with id '{seller_id}'.\n"
                    "Check the seller UUID or pass --seller-id with a valid UUID."
                )
        # Auto-fallback (should rarely happen given default is set)
        seller = (
            User.objects.filter(role="admin").first()
            or User.objects.filter(is_staff=True).first()
            or User.objects.first()
        )
        if not seller:
            raise CommandError("No users in DB. Pass --seller-id.")
        self.stdout.write(self.style.WARNING(f"No seller specified — defaulting to {seller.email}"))
        return seller

    def _upload_image(self, url: str, seller_id, upload_fn) -> Optional[str]:
        if not url:
            return None
        result = _download_image(url)
        if not result:
            return None
        data, content_type = result
        ext      = _ext_from_url(url, content_type)
        img_file = _ImageFile(data, f"product.{ext}", content_type)
        try:
            return upload_fn(img_file, str(seller_id), "product")
        except Exception:
            return None

    def _get_or_create_subcat(self, SubCategory, cat, display_name: str):
        """Get or auto-create a SubCategory under `cat` for the given display name."""
        sub_slug = slugify(f"{cat.name}-{display_name}")
        sub, created = SubCategory.objects.get_or_create(
            slug=sub_slug,
            defaults={
                "category"       : cat,
                "name"           : display_name,
                "gst_percentage" : None,   # inherit from parent category
                "commission_rate": None,   # inherit from parent category
            },
        )
        if created:
            self.stdout.write(
                self.style.WARNING(f"    Auto-created SubCategory: {cat.name} > {display_name}")
            )
        return sub

    @transaction.atomic
    def _import_record(
        self,
        *,
        product_data : dict,
        variant_data : dict,
        images_data  : list,
        source_meta  : dict,
        slug         : str,
        seller,
        cat,
        do_update    : bool,
        skip_images  : bool,
        dry_run      : bool,
        upload_fn,
        models       : dict,
    ) -> tuple[str, int, int]:

        Product        = models["Product"]
        ProductVariant = models["ProductVariant"]
        ProductImage   = models["ProductImage"]
        SubCategory    = models["SubCategory"]

        name = product_data.get("name") or ""

        # ── Resolve SubCategory (auto-create if missing) ───────────────────────
        raw_sub_name  = product_data.get("sub_category_name")
        sub_display   = _resolve_subcat_display(raw_sub_name)
        sub           = self._get_or_create_subcat(SubCategory, cat, sub_display)

        # ── Idempotency check ──────────────────────────────────────────────────
        # Match by: source external_id stored in description sentinel → slug → name+seller
        base_slug = slugify(name)[:110]
        existing  = (
            Product.all_objects.filter(slug=base_slug).first()
            or Product.all_objects.filter(slug=slug).first()
            or Product.all_objects.filter(name=name, seller=seller).first()
        )

        if existing and not do_update:
            return "skipped", 0, 0

        if dry_run:
            return ("updated" if existing else "created"), 0, 0

        # ── Build Product fields ───────────────────────────────────────────────
        product_fields = dict(
            name               = name,
            tagline            = (product_data.get("tagline") or "")[:499] or None,
            description        = product_data.get("description") or name,
            seller             = seller,
            scientific_name    = product_data.get("scientific_name") or None,
            care_level         = product_data.get("care_level")         or "Easy",
            light_requirements = product_data.get("light_requirements") or "Medium",
            growth_rate        = product_data.get("growth_rate")        or "Moderate",
            co2_requirement    = product_data.get("co2_requirement")    or "Low",
            water_temperature  = product_data.get("water_temperature")  or None,
            ph_range           = product_data.get("ph_range")           or None,
            origin             = product_data.get("origin")             or None,
            is_rare            = bool(product_data.get("is_rare", False)),
            is_active          = bool(product_data.get("is_active", True)),
            is_draft           = bool(product_data.get("is_draft", True)),
            rating             = Decimal(str(product_data.get("rating") or "5.0")),
        )

        if existing:
            for field, value in product_fields.items():
                setattr(existing, field, value)
            existing.save()
            product = existing
            action  = "updated"
        else:
            product = Product(slug=slug, **product_fields)
            product.save()
            action  = "created"

        # Assign parent category (M2M) and subcategory
        product.categories.set([cat])
        if sub:
            product.sub_categories.set([sub])

        # ── ProductVariant (upsert by product + variant name) ──────────────────
        vname    = (variant_data.get("name") or "Standard")[:100]
        vtype    = variant_data.get("variant_type") or "Plant"
        sku      = variant_data.get("sku") or None

        raw_price        = variant_data.get("price") or 0
        raw_base         = variant_data.get("base_price")
        raw_compare      = variant_data.get("compare_at_price")
        stock            = int(variant_data.get("stock") or 0)
        v_is_active      = bool(variant_data.get("is_active", True))
        item_cat         = variant_data.get("item_category") or "light"

        # base_price = website price (set by scraper); fall back to raw_price if absent
        base_price = _to_decimal(raw_base if raw_base else raw_price)
        compare_at = (
            Decimal(str(raw_compare)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if raw_compare
            else None
        )

        variant_qs = ProductVariant.all_objects.filter(product=product, name=vname)
        if variant_qs.exists():
            variant                  = variant_qs.first()
            variant.variant_type     = vtype
            variant.sku              = sku or variant.sku
            variant.base_price       = base_price
            variant.gst_rate         = DEFAULT_GST_RATE
            variant.commission_rate  = DEFAULT_COMMISSION_RATE
            variant.compare_at_price = compare_at
            variant.stock            = stock
            variant.weight           = Decimal(str(variant_data.get("weight") or DEFAULT_WEIGHT_KG))
            variant.length           = DEFAULT_LENGTH_CM
            variant.width            = DEFAULT_WIDTH_CM
            variant.height           = DEFAULT_HEIGHT_CM
            variant.item_category    = item_cat
            variant.packed_weight_grams = DEFAULT_PACKED_WEIGHT_G
            variant.is_active        = v_is_active
            variant.save()   # triggers price auto-calculation
        else:
            variant = ProductVariant(
                product             = product,
                name                = vname,
                variant_type        = vtype,
                sku                 = sku,
                base_price          = base_price,
                gst_rate            = DEFAULT_GST_RATE,
                commission_rate     = DEFAULT_COMMISSION_RATE,
                price               = Decimal("0.00"),   # recalculated on save()
                compare_at_price    = compare_at,
                stock               = stock,
                weight              = Decimal(str(variant_data.get("weight") or DEFAULT_WEIGHT_KG)),
                length              = DEFAULT_LENGTH_CM,
                width               = DEFAULT_WIDTH_CM,
                height              = DEFAULT_HEIGHT_CM,
                item_category       = item_cat,
                packed_weight_grams = DEFAULT_PACKED_WEIGHT_G,
                is_active           = v_is_active,
            )
            variant.save()   # triggers price auto-calculation

        # ── ProductImages ──────────────────────────────────────────────────────
        img_ok = img_fail = 0

        if images_data:
            if action == "updated":
                # Clear old images before re-importing
                ProductImage.all_objects.filter(product=product).delete()

            seen: set[str] = set()
            for img_rec in images_data:
                src_url = img_rec.get("image_url") or ""
                if not src_url or src_url in seen:
                    continue
                seen.add(src_url)

                order      = int(img_rec.get("order", 0))
                is_primary = bool(img_rec.get("is_primary", order == 0))

                if skip_images:
                    # Store petkadai CDN URL directly (no Firebase re-upload)
                    final_url = src_url
                else:
                    final_url = self._upload_image(src_url, seller.id, upload_fn)

                if final_url:
                    ProductImage.objects.create(
                        product    = product,
                        variant    = variant,
                        image_url  = final_url,
                        is_primary = is_primary,
                        order      = order,
                    )
                    img_ok += 1
                else:
                    img_fail += 1

        return action, img_ok, img_fail
