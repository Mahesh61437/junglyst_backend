"""
Management command: import_himadri

Reads scraped aquatic plant data from himadri_aquatic_plants.json (produced by
scrape_himadri.py) and upserts Products into Junglyst. Images are downloaded
from himadriaquatics.com and re-uploaded to Junglyst Firebase Storage.

Category mapping (JungLyst canonical tree):
  All aquatic plants → Aquascaping / Starter Packs
  (mirrors the existing import_aquaticexotica convention)

Usage:
    python manage.py import_himadri
    python manage.py import_himadri --seller-email admin@junglyst.com
    python manage.py import_himadri --json-file /path/to/file.json
    python manage.py import_himadri --dry-run
    python manage.py import_himadri --update
    python manage.py import_himadri --skip-images
    python manage.py import_himadri --limit 5
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import re
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
        os.path.dirname(__file__),           # .../core/management/commands/
        "..", "..", "..",                     # → junglyst_backend/
        "..",                                # → E:\JungLyst
        "himadri_aquatic_plants.json",
    )
)

REQUEST_TIMEOUT = 30
RETRY_DELAY = 2

DEFAULT_GST_RATE = Decimal("12.00")        # Aquascaping GST
DEFAULT_COMMISSION_RATE = Decimal("15.00") # Aquascaping commission
DEFAULT_WEIGHT_KG = Decimal("0.3")
DEFAULT_LENGTH_CM = Decimal("20.0")
DEFAULT_WIDTH_CM = Decimal("15.0")
DEFAULT_HEIGHT_CM = Decimal("10.0")
DEFAULT_PACKED_WEIGHT_GRAMS = 200

JUNGLYST_CATEGORY = "Aquascaping"
JUNGLYST_SUBCATEGORY = "Starter Packs"

# Himadri subcategory → variant type hint
SUBCAT_VARIANT_MAP: dict[str, str] = {
    "Tissue Culture Cups": "Tissue Culture",
    "Aquatic Ferns & Mosses": "Plant",
    "Aquatic Rhizome Plants, Ferns & Mosses": "Rhizome",
    "Carpet/ Foreground Plants": "Plant",
    "Carpet/Foreground Plants": "Plant",
    "Floating & pond plants": "Plant",
    "Aquatic stem plant varieties": "Bunch",
    "Low tech-aquarium plants": "Plant",
    "Anubias, Bucephalandras & Lagenandras": "Rhizome",
    "Cryptocorynes": "Plant",
    "Echinodorous/ Sword Varieties": "Plant",
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

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


# ── Data helpers ───────────────────────────────────────────────────────────────

def _parse_stock(status: str) -> int:
    """'87 in stock' → 87, '' / 'out of stock' → 0."""
    if not status:
        return 0
    m = re.search(r"(\d+)", status)
    return int(m.group(1)) if m else 0


def _infer_variant_type(name: str, categories: list[str]) -> str:
    """Infer VariantType from the product name and its Himadri categories."""
    n = name.lower()
    if "tissue culture" in n:
        return "Tissue Culture"
    if "pot" in n:
        return "Pot"
    if "rhizome" in n:
        return "Rhizome"
    if "clump" in n:
        return "Clump"
    if ("moss" in n or "pellia" in n or "susswasser" in n or "subwasser" in n) and (
        "pouch" in n or "mat" in n
    ):
        return "Mat"
    if "pouch" in n:
        return "Mat"
    if re.search(r"\d+\s*stems?", n):
        return "Bunch"
    for cat in categories:
        vt = SUBCAT_VARIANT_MAP.get(cat)
        if vt:
            return vt
    return "Plant"


def _infer_variant_name(product_name: str) -> str:
    """Extract a short variant label from the product name (the part in parentheses)."""
    m = re.search(r"\(([^)]+)\)", product_name)
    if m:
        label = m.group(1).strip()
        if len(label) <= 50:
            return label
    return "Standard"


def _normalize_light(val: str) -> str:
    """Map raw Himadri light string to Product.light_requirements choices."""
    v = val.lower()
    if "high" in v:
        return "High"
    if "low" in v and "medium" in v:
        return "Low"          # "Low to Medium" / "Low & Medium"
    if "low" in v:
        return "Low"
    if "medium" in v or "moderate" in v:
        return "Medium"
    return "Medium"           # safe default


def _normalize_co2(val: str) -> str:
    """Map raw Himadri CO2 string to Product.co2_requirement choices (Low/Medium/High)."""
    v = val.lower()
    if any(p in v for p in ("high", "required", "injection")):
        return "High"
    if any(p in v for p in ("medium", "moderate", "recommended")):
        return "Medium"
    return "Low"              # "Will survive under lower CO2 levels" etc.


def _normalize_growth(val: str) -> str:
    """Map raw growth description to Slow / Moderate / Fast."""
    v = val.lower()
    if any(p in v for p in ("fast", "rapid")):
        return "Fast"
    if any(p in v for p in ("slow", "sluggish")):
        return "Slow"
    return "Moderate"


def _parse_himadri_fields(raw_description: str) -> dict:
    """
    Extract structured key-value fields from Himadri's description block.

    Himadri description format:
      "Description Quantity: 1 Large pot Origin: Cameroon Plant positioning:
       foreground & midground Light requirement: low & Medium CO2 requirement:
       Will survive under lower CO2 levels Plant difficulty level: Easy ..."
    """
    # Boundary words used to terminate each capture group
    _BOUNDARY = r"(?=\s+(?:Plant|Light|CO2|Quantity|Pls\s+Note|In\s+India|$))"

    def _get(pattern: str) -> str:
        m = re.search(pattern, raw_description, re.IGNORECASE)
        return m.group(1).strip().rstrip(".,") if m else ""

    raw_light = _get(
        r"Light\s+requirement:\s*([^\n]+?)"
        r"(?=\s+(?:CO2|Plant\s+diff|Plant\s+prop|Pls|In\s+India)|$)"
    )
    raw_co2 = _get(
        r"CO2\s+requirement:\s*([^\n]+?)"
        r"(?=\s+(?:Plant\s+diff|Plant\s+prop|Pls|In\s+India)|$)"
    )
    raw_care = _get(
        r"Plant\s+difficulty\s+level:\s*([^\n]+?)"
        r"(?=\s+(?:Plant\s+prop|Pls|In\s+India)|$)"
    )
    raw_growth = _get(
        r"(?:Growth\s+rate|growth):\s*([^\n]+?)"
        r"(?=\s+(?:Plant|Light|CO2|Pls|In\s+India)|$)"
    )

    # Infer growth from short_desc keywords when not explicit
    growth = _normalize_growth(raw_growth) if raw_growth else None

    return {
        "light_requirements": _normalize_light(raw_light) if raw_light else None,
        "co2_requirement":    _normalize_co2(raw_co2)   if raw_co2  else None,
        "care_level":         raw_care.capitalize()      if raw_care else None,
        "growth_rate":        growth,
    }


def _extract_origin(raw_description: str, short_desc: str) -> str:
    """
    Extract clean origin value from Himadri's description block or short_description.
    'Origin: Cameroon' → 'Cameroon'
    'Native to West Africa' → 'West Africa'
    """
    # Structured field: "Origin: Cameroon" (stops before next keyword)
    m = re.search(
        r"Origin:\s*([A-Za-z ,\-]+?)(?=\s+(?:Plant|Light|CO2|Pls|In\s+India)|$)",
        raw_description, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().rstrip(".,")
    # Fallback: "native to the Amazon basin" in short description
    m = re.search(r"[Nn]ative to (?:the\s+)?([A-Za-z ,\-]+?)[\.,]", short_desc)
    if m:
        return m.group(1).strip()
    return ""


def _infer_growth_from_text(short_desc: str) -> str:
    """Fallback growth rate inference from free-text description."""
    text = short_desc.lower()
    if any(p in text for p in ("fast growth", "fast-growing", "fast grower", "rapid growth")):
        return "Fast"
    if any(p in text for p in ("slow growth", "slow-growing", "slow grower", "extremely slow")):
        return "Slow"
    return "Moderate"


def _extract_ph(description: str) -> str:
    m = re.search(r"ph[:\s]*([\d.]+\s*[-–to]+\s*[\d.]+)", description, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_temp(description: str) -> str:
    m = re.search(r"(\d{2})\s*[-–]\s*(\d{2})\s*[°]?[Cc]", description)
    if m:
        return f"{m.group(1)}–{m.group(2)}°C"
    return ""


def _calc_base_price(retail_price, gst: Decimal, commission: Decimal) -> Decimal:
    retail = Decimal(str(retail_price))
    divisor = Decimal("1") + (gst / Decimal("100")) + (commission / Decimal("100"))
    return (retail / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _himadri_key(product_url: str) -> str:
    """Stable idempotency key derived from the Himadri product URL slug."""
    slug = urlparse(product_url).path.strip("/").rsplit("/", 1)[-1]
    return f"himadri-{slug}"


def _name_slug(name: str, him_key: str, existing_slugs: set) -> str:
    base = slugify(name) or him_key
    base = base[:110]
    slug = base
    counter = 1
    while slug in existing_slugs:
        slug = f"{base}-{counter}"
        counter += 1
    existing_slugs.add(slug)
    return slug


# ── Management command ─────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Import aquatic plants scraped from himadriaquatics.com into Junglyst, "
        "uploading images to Firebase Storage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--seller-id", default="",
            help="UUID of the Junglyst seller to assign products to.",
        )
        parser.add_argument(
            "--seller-email", default="",
            help="Email of the Junglyst seller (alternative to --seller-id).",
        )
        parser.add_argument(
            "--json-file", default=DEFAULT_JSON,
            help=f"Path to the scraped JSON file. Default: {DEFAULT_JSON}",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse and validate without writing to the database.",
        )
        parser.add_argument(
            "--update", action="store_true",
            help="Overwrite existing products on re-run.",
        )
        parser.add_argument(
            "--skip-images", action="store_true",
            help="Skip image download/upload (keeps Himadri URLs).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after this many products (0 = no limit).",
        )

    def handle(self, *args, **options):
        from core.models import (
            Category, Product, ProductImage, ProductVariant, SubCategory, Tag, User,
        )
        from core.storage import upload_to_firebase

        dry_run: bool = options["dry_run"]
        do_update: bool = options["update"]
        skip_images: bool = options["skip_images"]
        limit: int = options["limit"]
        json_path: str = options["json_file"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        # ── Load JSON ─────────────────────────────────────────────────────────
        if not os.path.exists(json_path):
            raise CommandError(f"JSON file not found: {json_path}")

        with open(json_path, encoding="utf-8") as f:
            raw_products: list[dict] = json.load(f)

        self.stdout.write(f"Loaded {len(raw_products)} products from {json_path}")

        if limit:
            raw_products = raw_products[:limit]
            self.stdout.write(self.style.WARNING(f"  (limited to first {limit})"))

        # ── Seller ────────────────────────────────────────────────────────────
        seller = self._resolve_seller(User, options["seller_id"], options["seller_email"])
        self.stdout.write(f"Seller  : {seller.email}  (id={seller.id})")

        # ── Category lookup ───────────────────────────────────────────────────
        try:
            cat = Category.objects.get(name=JUNGLYST_CATEGORY)
        except Category.DoesNotExist:
            raise CommandError(
                f"Category '{JUNGLYST_CATEGORY}' not found in the database. "
                "Run: python manage.py seed_categories"
            )
        try:
            sub = SubCategory.objects.get(category=cat, name=JUNGLYST_SUBCATEGORY)
        except SubCategory.DoesNotExist:
            sub = None
            self.stdout.write(
                self.style.WARNING(f"  SubCategory '{JUNGLYST_SUBCATEGORY}' not found — sub_category will be null.")
            )

        self.stdout.write(f"Category: {cat.name} / {sub.name if sub else '(none)'}\n")

        # ── Pre-build slug set for collision avoidance ─────────────────────────
        from core.models import Product as _Product
        existing_slugs: set = set(_Product.all_objects.values_list("slug", flat=True))

        created = updated = skipped = img_ok = img_fail = errors = 0

        for idx, raw in enumerate(raw_products, start=1):
            him_key = _himadri_key(raw.get("url", ""))
            # base_lookup_slug: plain name slug without collision suffix — used to
            # find existing products from a previous import run.
            base_lookup_slug = (slugify(raw.get("name", "")) or him_key)[:110]
            # collision-safe slug: only needed when actually creating a new product
            slug = _name_slug(raw.get("name", ""), him_key, existing_slugs)

            try:
                action, n_ok, n_fail = self._import_product(
                    raw=raw,
                    slug=slug,
                    him_key=him_key,
                    base_lookup_slug=base_lookup_slug,
                    seller=seller,
                    cat=cat,
                    sub=sub,
                    do_update=do_update,
                    skip_images=skip_images,
                    dry_run=dry_run,
                    upload_fn=upload_to_firebase,
                    models={
                        "Product": Product,
                        "ProductVariant": ProductVariant,
                        "ProductImage": ProductImage,
                        "Tag": Tag,
                    },
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(
                    self.style.ERROR(f"  [{idx}/{len(raw_products)}] ERROR  {slug} — {exc}")
                )
                import traceback
                self.stderr.write(traceback.format_exc())
                continue

            img_ok += n_ok
            img_fail += n_fail
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            else:
                skipped += 1

            label = {"created": "CREATED", "updated": "UPDATED", "skipped": "SKIPPED"}[action]
            img_note = f"  [{n_ok} imgs]" if n_ok else ""
            self.stdout.write(
                f"  [{idx}/{len(raw_products)}] {label}  {raw.get('name', '')[:55]}{img_note}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("-" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"Done.  created={created}  updated={updated}  skipped={skipped}  "
            f"errors={errors}  images_uploaded={img_ok}  images_failed={img_fail}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was saved."))

    # ── helpers ────────────────────────────────────────────────────────────────

    def _resolve_seller(self, User, seller_id: str, seller_email: str):
        if seller_id:
            try:
                return User.objects.get(id=seller_id)
            except User.DoesNotExist:
                raise CommandError(f"No user with id: {seller_id}")
        if seller_email:
            try:
                return User.objects.get(email=seller_email)
            except User.DoesNotExist:
                raise CommandError(f"No user with email: {seller_email}")
        seller = (
            User.objects.filter(role="admin").first()
            or User.objects.filter(is_staff=True).first()
            or User.objects.first()
        )
        if not seller:
            raise CommandError("No users in DB. Pass --seller-id or --seller-email.")
        self.stdout.write(self.style.WARNING(f"No seller specified — defaulting to {seller.email}"))
        return seller

    def _upload_image(self, url: str, seller_id, upload_fn) -> Optional[str]:
        if not url:
            return None
        result = _download_image(url)
        if not result:
            return None
        data, content_type = result
        ext = _ext_from_url(url, content_type)
        img_file = _ImageFile(data, f"product.{ext}", content_type)
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
        him_key: str,
        base_lookup_slug: str,
        seller,
        cat,
        sub,
        do_update: bool,
        skip_images: bool,
        dry_run: bool,
        upload_fn,
        models: dict,
    ) -> tuple[str, int, int]:
        Product = models["Product"]
        ProductVariant = models["ProductVariant"]
        ProductImage = models["ProductImage"]

        name: str = raw.get("name") or ""
        # raw_desc = Himadri's structured block ("Origin: X Light requirement: Y ...")
        # short_desc = clean botanical description ("Anubias nana is a dwarf variety...")
        raw_desc: str = (raw.get("description") or "").strip()
        short_desc: str = (raw.get("short_description") or "").strip()
        scientific_name: str = raw.get("scientific_name") or ""
        sku: str = raw.get("sku") or ""
        sale_price = raw.get("sale_price")
        regular_price = raw.get("regular_price")
        stock_status: str = raw.get("stock_status") or ""
        categories: list[str] = raw.get("categories") or []
        images: list[str] = raw.get("images") or []

        # ── FIX 2: Extract individual structured fields from Himadri block ──
        parsed = _parse_himadri_fields(raw_desc)
        origin = _extract_origin(raw_desc, short_desc)

        # ── FIX 4: Use short_desc as clean description; fall back to raw_desc ─
        clean_description = short_desc or raw_desc

        # pH and temperature from either source
        ph = _extract_ph(raw_desc + " " + short_desc)
        temp = _extract_temp(raw_desc + " " + short_desc)

        # Resolve individual care fields (structured block wins; fallback to text inference)
        light_req  = parsed["light_requirements"] or "Medium"
        co2_req    = parsed["co2_requirement"]    or "Low"
        care_level = parsed["care_level"]         or "Easy"
        growth     = parsed["growth_rate"]        or _infer_growth_from_text(short_desc)

        # ── FIX 3: Stock fallback — if count unknown but product is listed as in_stock ─
        stock = _parse_stock(stock_status)
        if stock == 0 and raw.get("in_stock"):
            stock = 10   # default quantity when count is undetectable from page

        variant_type = _infer_variant_type(name, categories)
        variant_name = _infer_variant_name(name)
        tagline = short_desc[:499] if short_desc else ""

        # Idempotency: him_key (if ever stored) → base name slug (most common)
        # → collision-suffixed slug (edge case). Also match by name for robustness.
        existing = (
            Product.all_objects.filter(slug=him_key).first()
            or Product.all_objects.filter(slug=base_lookup_slug).first()
            or Product.all_objects.filter(slug=slug).first()
            or Product.all_objects.filter(name=name, seller=seller).first()
        )
        if existing and not do_update:
            return "skipped", 0, 0

        if dry_run:
            return ("created" if not existing else "updated"), 0, 0

        # ── Product ──────────────────────────────────────────────────────────
        product_fields = dict(
            name=name,
            tagline=tagline,
            description=clean_description,
            seller=seller,
            sub_category=sub,
            scientific_name=scientific_name,
            care_level=care_level,
            light_requirements=light_req,
            growth_rate=growth,
            co2_requirement=co2_req,
            water_temperature=temp,
            ph_range=ph,
            origin=origin,
            is_rare=False,
            is_active=True,
            is_draft=False,
        )

        if existing:
            for field, value in product_fields.items():
                setattr(existing, field, value)
            if existing.slug == him_key:
                existing.slug = slug
            existing.save()
            product = existing
            action = "updated"
        else:
            product = Product(slug=slug, **product_fields)
            product.save()
            action = "created"

        product.categories.set([cat])

        # ── Variant ───────────────────────────────────────────────────────────
        base_price = (
            _calc_base_price(sale_price, DEFAULT_GST_RATE, DEFAULT_COMMISSION_RATE)
            if sale_price
            else Decimal("0.00")
        )
        compare_at = (
            Decimal(str(regular_price)).quantize(Decimal("0.01"))
            if regular_price and regular_price != sale_price
            else None
        )

        variant_qs = ProductVariant.all_objects.filter(product=product, name=variant_name)
        if variant_qs.exists():
            variant = variant_qs.first()
            variant.base_price = base_price
            variant.gst_rate = DEFAULT_GST_RATE
            variant.commission_rate = DEFAULT_COMMISSION_RATE
            variant.compare_at_price = compare_at
            variant.stock = stock
            variant.sku = sku or variant.sku
            variant.variant_type = variant_type
            variant.weight = DEFAULT_WEIGHT_KG
            variant.length = DEFAULT_LENGTH_CM
            variant.width = DEFAULT_WIDTH_CM
            variant.height = DEFAULT_HEIGHT_CM
            variant.item_category = "light"
            variant.packed_weight_grams = DEFAULT_PACKED_WEIGHT_GRAMS
            variant.is_active = True
            variant.save()
        else:
            variant = ProductVariant(
                product=product,
                name=variant_name,
                variant_type=variant_type,
                sku=sku or None,
                base_price=base_price,
                gst_rate=DEFAULT_GST_RATE,
                commission_rate=DEFAULT_COMMISSION_RATE,
                price=Decimal("0.00"),  # recalculated on save()
                compare_at_price=compare_at,
                stock=stock,
                weight=DEFAULT_WEIGHT_KG,
                length=DEFAULT_LENGTH_CM,
                width=DEFAULT_WIDTH_CM,
                height=DEFAULT_HEIGHT_CM,
                item_category="light",
                packed_weight_grams=DEFAULT_PACKED_WEIGHT_GRAMS,
                is_active=True,
            )
            variant.save()

        # ── Images ────────────────────────────────────────────────────────────
        img_ok = img_fail = 0

        if not skip_images and images:
            if action == "updated":
                ProductImage.all_objects.filter(product=product).delete()

            seen: set[str] = set()
            unique_images = [u for u in images if u and u not in seen and not seen.add(u)]

            for order, src_url in enumerate(unique_images):
                firebase_url = self._upload_image(src_url, seller.id, upload_fn)
                if firebase_url:
                    ProductImage.objects.create(
                        product=product,
                        variant=variant,
                        image_url=firebase_url,
                        is_primary=(order == 0),
                        order=order,
                    )
                    img_ok += 1
                else:
                    img_fail += 1

        return action, img_ok, img_fail
