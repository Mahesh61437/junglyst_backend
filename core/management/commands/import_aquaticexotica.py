"""
Management command: import_aquaticexotica

Fetches all products from the Aquatic Exotica public API and bulk-upserts them
into the Junglyst database under a designated seller account.  Product images
are downloaded from the AE Firebase URLs and re-uploaded to the Junglyst
Firebase Storage bucket so Junglyst owns the assets.

Usage:
    python manage.py import_aquaticexotica --seller-id <uuid>
    python manage.py import_aquaticexotica --seller-email <email>
    python manage.py import_aquaticexotica --dry-run
    python manage.py import_aquaticexotica --update          # overwrite existing
    python manage.py import_aquaticexotica --skip-images     # skip image upload
    python manage.py import_aquaticexotica --limit 5         # test batch

Idempotency:
    Products are keyed by slug "ae-<ae_product_id>".  Re-running skips already-
    imported products unless --update is passed.
"""
from __future__ import annotations

import io
import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from typing import Optional
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

AE_API_BASE = "https://api.aquaticexotica.com/api/products/"
AE_API_DETAIL = "https://api.aquaticexotica.com/api/products/{id}/"
REQUEST_TIMEOUT = 30
RETRY_DELAY = 2

DEFAULT_GST_RATE = Decimal("12.00")
DEFAULT_COMMISSION_RATE = Decimal("0.00")
DEFAULT_WEIGHT_KG = Decimal("0.5")
DEFAULT_LENGTH_CM = Decimal("10.0")
DEFAULT_WIDTH_CM = Decimal("10.0")
DEFAULT_HEIGHT_CM = Decimal("10.0")
DEFAULT_PACKED_WEIGHT_GRAMS = 200

CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "Aquatic Plants":   ("Plants",                   "Aquatic Plants"),
    "Rhizome plants":   ("Plants",                   "Aquatic Plants"),
    "Moss":             ("Terrarium & Paludarium",   "Terrarium Moss"),
    "Terrarium Plants": ("Terrarium & Paludarium",   "Terrarium Plants"),
    "Indoor Plants":    ("Terrarium & Paludarium",   "Terrarium Plants"),
    "Exotic Plants":    ("Terrarium & Paludarium",   "Terrarium Plants"),
    "Premium":          ("Plants",                   "Rare & Exotic"),
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get_json(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            if attempt == retries - 1:
                raise CommandError(f"Failed to fetch {url}: {exc}") from exc
            time.sleep(RETRY_DELAY)
    return {}


def _download_image(url: str, retries: int = 3) -> Optional[tuple[bytes, str]]:
    """Download image bytes from a URL.  Returns (bytes, content_type) or None."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Junglyst-Importer/1.0"})
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
    """Wraps raw bytes so upload_to_firebase() sees a file-like object."""

    def __init__(self, data: bytes, filename: str, content_type: str):
        self._buf = io.BytesIO(data)
        self.name = filename
        self.content_type = content_type

    def read(self, *args):
        return self._buf.read(*args)

    def seek(self, *args):
        return self._buf.seek(*args)

    def tell(self):
        return self._buf.tell()


def _ext_from_url(url: str, content_type: str) -> str:
    path = urlparse(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower().split("?")[0]
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    return ext.lstrip(".")


# ── API helpers ───────────────────────────────────────────────────────────────

def _fetch_all_products() -> list[dict]:
    page, results = 1, []
    while True:
        data = _get_json(f"{AE_API_BASE}?page={page}")
        results.extend(data.get("results") or [])
        if not data.get("next"):
            break
        page += 1
    return results


def _fetch_detail(product_id: int) -> dict:
    return _get_json(AE_API_DETAIL.format(id=product_id))


# ── Text / price helpers ──────────────────────────────────────────────────────

def _strip_html(html: str, max_len: Optional[int] = None) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    if max_len and len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _calc_base_price(retail_price, gst: Decimal, commission: Decimal) -> Decimal:
    retail = Decimal(str(retail_price))
    divisor = Decimal("1") + (gst / Decimal("100")) + (commission / Decimal("100"))
    return (retail / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _infer_care(description_html: str) -> dict[str, str]:
    text = _strip_html(description_html).lower()

    care = "Easy"
    if "advanced" in text:
        care = "Advanced"
    elif "medium" in text and "care" in text:
        care = "Medium"

    light = "Medium"
    if any(p in text for p in ("low light", "low to medium", "low lighting")):
        light = "Low"
    elif any(p in text for p in ("high light", "high lighting")):
        light = "High"

    growth = "Moderate"
    if any(p in text for p in ("slow growth", "extremely slow", "slow-growing")):
        growth = "Slow"
    elif any(p in text for p in ("fast growth", "fast-growing")):
        growth = "Fast"

    co2 = "Low"
    if any(p in text for p in ("co2 required", "co2 injection")):
        co2 = "High"
    elif any(p in text for p in ("co2 recommended", "medium co2")):
        co2 = "Medium"

    return {"care_level": care, "light_requirements": light, "growth_rate": growth, "co2_requirement": co2}


def _resolve_category(product: dict):
    from core.models import Category, SubCategory
    cat_names = [c.get("name", "") for c in (product.get("categories") or []) if c.get("name")]
    names_set = set(cat_names)
    
    mapped_subs = []
    
    # 1. Aquatic Mosses (e.g. Christmas Moss)
    # Aquatic Mosses (e.g. Christmas Moss) → Plants > Aquatic Moss + Terrarium moss + Aquatic plants +  Terrarium & Paludarium
    if "Moss" in names_set and "Aquatic Plants" in names_set:
        mapped_subs.append(("Plants", "Aquatic Moss"))
        mapped_subs.append(("Terrarium & Paludarium", "Terrarium Moss"))
        mapped_subs.append(("Plants", "Aquatic Plants"))

    # 2. Terrarium Mosses (e.g. Sheet/Mood Moss)
    # Terrarium Mosses (e.g. Sheet/Mood Moss) → Terrarium & Paludarium > Terrarium moss + Plants
    elif "Moss" in names_set:
        mapped_subs.append(("Terrarium & Paludarium", "Terrarium Moss"))
        mapped_subs.append(("Plants", "Mosses"))

    # 3. Rare/Exotic Rhizomes & Aquatic Plants (e.g. Bucephalandra)
    # Rare/Exotic Rhizomes & Aquatic Plants (e.g. Bucephalandra) → Plants > Rare & Exotic + Aquatic plants + Terrarium & Paludarium
    elif ("Rhizome plants" in names_set or "Aquatic Plants" in names_set) and ("Exotic Plants" in names_set or "Premium" in names_set):
        mapped_subs.append(("Plants", "Rare & Exotic"))
        mapped_subs.append(("Plants", "Aquatic Plants"))
        mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))

    # 4. Standard Rhizomes & Aquatic Plants (e.g. Anubias)
    # Standard Rhizomes & Aquatic Plants (e.g. Anubias) → Plants > Aquatic Plants + Terrarium & Paludarium
    elif "Rhizome plants" in names_set or "Aquatic Plants" in names_set:
        mapped_subs.append(("Plants", "Aquatic Plants"))
        mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))

    # 5. Exotic/Indoor/Terrarium Plants (Non-Aquatic) (e.g. Peperomia, Fittonia, Ferns)
    # Exotic/Indoor/Terrarium Plants (Non-Aquatic) (e.g. Peperomia, Fittonia, Ferns) → Terrarium & Paludarium > Terrarium Plants
    elif "Exotic Plants" in names_set or "Indoor Plants" in names_set or "Terrarium Plants" in names_set:
        mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))

    # Deduplicate mapped_subs
    mapped_subs = list(dict.fromkeys(mapped_subs))

    # Fallback to single category map
    if not mapped_subs:
        for name in cat_names:
            if name in CATEGORY_MAP:
                mapped_subs.append(CATEGORY_MAP[name])
                break

    cats = []
    subs = []
    for cat_name, sub_name in mapped_subs:
        try:
            cat_obj = Category.objects.get(name=cat_name)
            if cat_obj not in cats:
                cats.append(cat_obj)
            try:
                sub_obj = SubCategory.objects.get(category=cat_obj, name=sub_name)
                if sub_obj not in subs:
                    subs.append(sub_obj)
            except SubCategory.DoesNotExist:
                pass
        except Category.DoesNotExist:
            pass

    return cats, subs, cat_names


def _ae_key(ae_id: int) -> str:
    """Internal idempotency key — used only for DB lookup, never stored as the slug."""
    return f"ae-{ae_id}"


def _name_slug(name: str, ae_id: int, existing_slugs: set) -> str:
    """Generate an SEO-friendly slug from the product name with collision handling."""
    base = slugify(name) or f"ae-{ae_id}"
    slug = base
    counter = 1
    while slug in existing_slugs:
        slug = f"{base}-{counter}"
        counter += 1
    existing_slugs.add(slug)
    return slug


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Import / update products from the Aquatic Exotica public API into Junglyst, uploading images to Junglyst Firebase Storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--seller-id",
            default="",
            help="UUID of the Junglyst seller user to assign products to.",
        )
        parser.add_argument(
            "--seller-email",
            default="",
            help="Email of the Junglyst seller user (alternative to --seller-id).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and parse products but do not write to the database.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Overwrite fields on products that already exist in the DB.",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Do not download or upload images (keeps AE URLs as-is).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after importing this many products (0 = no limit).",
        )

    def handle(self, *args, **options):
        from core.models import (
            Category, Product, ProductImage, ProductVariant, SubCategory, Tag, User
        )
        from core.storage import upload_to_firebase

        dry_run: bool = options["dry_run"]
        do_update: bool = options["update"]
        skip_images: bool = options["skip_images"]
        limit: int = options["limit"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        seller = self._resolve_seller(User, options["seller_id"], options["seller_email"])
        self.stdout.write(f"Seller  : {seller.email}  (id={seller.id})")

        self.stdout.write("Fetching product list from Aquatic Exotica API…")
        products = _fetch_all_products()
        self.stdout.write(f"  → {len(products)} products found")

        if limit:
            products = products[:limit]
            self.stdout.write(self.style.WARNING(f"  (limited to first {limit})"))

        created = updated = skipped = img_ok = img_fail = errors = 0

        # Pre-build set of slugs already in the DB so _name_slug can avoid collisions
        from core.models import Product as _Product
        existing_slugs: set = set(_Product.all_objects.values_list("slug", flat=True))

        for idx, raw in enumerate(products, start=1):
            ae_id: int = raw["id"]
            ae_key = _ae_key(ae_id)
            slug = _name_slug(raw.get("name", ""), ae_id, existing_slugs)

            try:
                result, n_img_ok, n_img_fail = self._import_product(
                    raw=raw,
                    slug=slug,
                    ae_key=ae_key,
                    seller=seller,
                    do_update=do_update,
                    skip_images=skip_images,
                    dry_run=dry_run,
                    upload_fn=upload_to_firebase,
                    models={
                        "Product": Product,
                        "ProductVariant": ProductVariant,
                        "ProductImage": ProductImage,
                        "Category": Category,
                        "SubCategory": SubCategory,
                        "Tag": Tag,
                    },
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(
                    self.style.ERROR(f"  [{idx}/{len(products)}] ERROR  {slug} — {exc}")
                )
                continue

            img_ok += n_img_ok
            img_fail += n_img_fail

            status_label = {"created": "CREATED", "updated": "UPDATED", "skipped": "SKIPPED"}[result]
            img_note = f"  [{n_img_ok} imgs]" if n_img_ok else ""
            self.stdout.write(
                f"  [{idx}/{len(products)}] {status_label}  {slug}  (ae_key={ae_key})  "
                f"{raw['name'][:55]}{img_note}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("─" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"Done.  created={created}  updated={updated}  skipped={skipped}  "
            f"errors={errors}  images_uploaded={img_ok}  images_failed={img_fail}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was saved."))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _resolve_seller(self, User, seller_id: str, seller_email: str):
        if seller_id:
            try:
                return User.objects.get(id=seller_id)
            except User.DoesNotExist:
                raise CommandError(f"No user found with id: {seller_id}")
        if seller_email:
            try:
                return User.objects.get(email=seller_email)
            except User.DoesNotExist:
                raise CommandError(f"No user found with email: {seller_email}")

        seller = (
            User.objects.filter(role="admin").first()
            or User.objects.filter(is_staff=True).first()
            or User.objects.first()
        )
        if not seller:
            raise CommandError(
                "No users in the database. Pass --seller-id or --seller-email."
            )
        self.stdout.write(self.style.WARNING(f"No seller specified — defaulting to {seller.email}"))
        return seller

    def _upload_image(self, url: str, seller_id, upload_fn) -> Optional[str]:
        """Download from AE and re-upload to Junglyst Firebase. Returns public URL or None."""
        if not url:
            return None
        result = _download_image(url)
        if not result:
            return None
        data, content_type = result
        ext = _ext_from_url(url, content_type)
        filename = f"product.{ext}"
        img_file = _ImageFile(data, filename, content_type)
        try:
            return upload_fn(img_file, str(seller_id), "product")
        except Exception:
            return None

    @transaction.atomic
    def _import_product(
        self,
        *,
        raw: dict,
        slug: str,
        ae_key: str,
        seller,
        do_update: bool,
        skip_images: bool,
        dry_run: bool,
        upload_fn,
        models: dict,
    ) -> tuple[str, int, int]:
        """Returns (action, images_uploaded, images_failed)."""
        Product = models["Product"]
        ProductVariant = models["ProductVariant"]
        ProductImage = models["ProductImage"]
        Tag = models["Tag"]

        ae_id: int = raw["id"]
        name: str = raw.get("name") or ""
        description_html: str = raw.get("description") or ""
        retail_price = raw.get("price")
        compare_at_price = raw.get("compareAtPrice")
        stock: int = int(raw.get("stock") or 0)
        is_active: bool = bool(raw.get("isActive", True))
        image_url: str = raw.get("imageUrl") or ""
        thumbnail_url: str = raw.get("thumbnailUrl") or ""
        rating = Decimal(str(raw.get("rating") or "5.0"))

        care = _infer_care(description_html)
        cat, subs, _ = _resolve_category(raw)
        description = _strip_html(description_html, max_len=8000)

        # Look up by the old ae-{id} key first (backward compat), then by new name slug
        existing = (
            Product.all_objects.filter(slug=ae_key).first()
            or Product.all_objects.filter(slug=slug).first()
        )
        if existing and not do_update:
            return "skipped", 0, 0

        if dry_run:
            return ("created" if not existing else "updated"), 0, 0

        # ── Product ──────────────────────────────────────────────────────────
        product_fields = dict(
            name=name,
            tagline="",
            description=description,
            seller=seller,
            scientific_name="",
            care_level=care["care_level"],
            light_requirements=care["light_requirements"],
            growth_rate=care["growth_rate"],
            co2_requirement=care["co2_requirement"],
            water_temperature="",
            ph_range="",
            is_rare=False,
            is_active=is_active,
            is_draft=False,
            rating=rating,
        )

        if existing:
            for field, value in product_fields.items():
                setattr(existing, field, value)
            # Migrate old ae-{id} slug to SEO-friendly name slug
            if existing.slug == ae_key:
                existing.slug = slug
            existing.save()
            product = existing
            action = "updated"
        else:
            product = Product(slug=slug, **product_fields)
            product.save()
            action = "created"

        if cat:
            product.categories.set(cat)

        if subs:
            product.sub_categories.set(subs)

        tag_names = [t.get("name") for t in (raw.get("tagDetails") or []) if t.get("name")]
        tags = []
        for tag_name in tag_names:
            tag, _ = Tag.objects.get_or_create(name=tag_name)
            tags.append(tag)
        product.tags.set(tags)

        # ── Variant ──────────────────────────────────────────────────────────
        if retail_price:
            base_price = _calc_base_price(retail_price, DEFAULT_GST_RATE, DEFAULT_COMMISSION_RATE)
        else:
            base_price = Decimal("0.00")

        compare_at_dec: Optional[Decimal] = (
            Decimal(str(compare_at_price)).quantize(Decimal("0.01"))
            if compare_at_price
            else None
        )

        variant_qs = ProductVariant.all_objects.filter(product=product, name="Standard")
        if variant_qs.exists():
            variant = variant_qs.first()
            variant.base_price = base_price
            variant.gst_rate = DEFAULT_GST_RATE
            variant.commission_rate = DEFAULT_COMMISSION_RATE
            variant.compare_at_price = compare_at_dec
            variant.stock = stock
            variant.weight = DEFAULT_WEIGHT_KG
            variant.length = DEFAULT_LENGTH_CM
            variant.width = DEFAULT_WIDTH_CM
            variant.height = DEFAULT_HEIGHT_CM
            variant.item_category = "light"
            variant.packed_weight_grams = DEFAULT_PACKED_WEIGHT_GRAMS
            variant.is_active = is_active
            variant.save()
        else:
            variant = ProductVariant(
                product=product,
                name="Standard",
                variant_type="Plant",
                base_price=base_price,
                gst_rate=DEFAULT_GST_RATE,
                commission_rate=DEFAULT_COMMISSION_RATE,
                price=Decimal("0.00"),
                compare_at_price=compare_at_dec,
                stock=stock,
                weight=DEFAULT_WEIGHT_KG,
                length=DEFAULT_LENGTH_CM,
                width=DEFAULT_WIDTH_CM,
                height=DEFAULT_HEIGHT_CM,
                item_category="light",
                packed_weight_grams=DEFAULT_PACKED_WEIGHT_GRAMS,
                is_active=is_active,
            )
            variant.save()

        # ── Images ───────────────────────────────────────────────────────────
        img_ok = img_fail = 0

        if not skip_images:
            # Fetch detail for extra images array
            detail = _fetch_detail(ae_id)
            extra_images = detail.get("images") or []

            # Collect all candidate URLs (deduplicated, order: primary → thumbnail → extras)
            seen: set[str] = set()
            candidate_urls: list[str] = []
            for u in [image_url, thumbnail_url] + [
                (img.get("image_url") or img.get("imageUrl") or "") for img in extra_images
            ]:
                if u and u not in seen:
                    seen.add(u)
                    candidate_urls.append(u)

            # On update, clear existing images
            if action == "updated":
                ProductImage.all_objects.filter(product=product).delete()

            for order, src_url in enumerate(candidate_urls):
                junglyst_url = self._upload_image(src_url, seller.id, upload_fn)
                if junglyst_url:
                    ProductImage.objects.create(
                        product=product,
                        variant=variant,
                        image_url=junglyst_url,
                        is_primary=(order == 0),
                        order=order,
                    )
                    img_ok += 1
                else:
                    img_fail += 1

        return action, img_ok, img_fail
