"""
Management command: import_greenwater

Upserts Green Water Aquascapes products into the Junglyst database from the
sheet produced by scrape_greenwater.py (greenwater_products.json).

Pricing
-------
Buyer price = GWA's actual selling price + 10%.

The ProductVariant model computes `price = base_price * (1 + seller_rate/100)`
on save (see ProductVariant.save / User._resolve_commission), and the resolved
rate is ALWAYS the seller's `seller_commission_rate`. So we:
  * set base_price = GWA retail price, and
  * pin the seller's commission to 10 % (price_is_buyer_final = False).
The model then produces price = retail * 1.10 by itself. We assert the result
per row and warn on any mismatch.

Idempotency
-----------
Keyed by variant SKU (the GWA SKU, e.g. "#GWAT097", which is unique). Re-running
skips existing products unless --update is passed. The tissue-culture and pot
forms of the same plant are distinct SKUs and import as separate products.

Usage
-----
    python manage.py import_greenwater --seller-email seller@example.com
    python manage.py import_greenwater --seller-id <uuid> --input path/to.json
    python manage.py import_greenwater --dry-run
    python manage.py import_greenwater --update
    python manage.py import_greenwater --skip-images --limit 5
"""
from __future__ import annotations

import io
import json
import mimetypes
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

REQUEST_TIMEOUT = 30
RETRY_DELAY = 2

MARKUP_PERCENT = Decimal("10")          # buyer pays GWA price + 10 %
DEFAULT_INPUT = "greenwater_products.json"

DEFAULT_GST_RATE = Decimal("0.00")      # live plants are GST-exempt in IN
DEFAULT_WEIGHT_KG = Decimal("0.5")
DEFAULT_LENGTH_CM = Decimal("10.0")
DEFAULT_WIDTH_CM = Decimal("10.0")
DEFAULT_HEIGHT_CM = Decimal("10.0")
DEFAULT_PACKED_WEIGHT_GRAMS = 200

# Every GWA product is an aquatic plant. Land it in the known-good base bucket,
# then attach a more specific subcategory when one already exists in the DB.
BASE_CATEGORY = "Plants"
BASE_SUBCATEGORY = "Aquatic Plants"

# scraper junglyst_subcategory  ->  (Category, SubCategory) to also attach if present
SUBCATEGORY_MAP: dict[str, tuple[str, str]] = {
    "Carpet Plants":    ("Plants", "Carpet Plants"),
    "Stem Plants":      ("Plants", "Stem Plants"),
    "Rhizome Plants":   ("Plants", "Aquatic Plants"),
    "Mosses":           ("Plants", "Mosses"),
    "Floating Plants":  ("Plants", "Floating Plants"),
    "Bulb Plants":      ("Plants", "Bulb Plants"),
    "Rare & Exotic":    ("Plants", "Rare & Exotic"),
    "Background Plants": ("Plants", "Stem Plants"),
    "Midground Plants": ("Plants", "Aquatic Plants"),
    "Tissue Culture":   ("Plants", "Aquatic Plants"),
}

# name / packing keyword  ->  ProductVariant.VariantType value
VARIANT_TYPE_RULES = [
    ("tissue culture", "Tissue Culture"),
    ("[tc]", "Tissue Culture"),
    ("emersed", "Emersed"),
    ("submerged", "Submerged"),
    ("mat", "Mat"),
    ("cup", "Cup"),
    ("bulb", "Bulb"),
    ("corm", "Corm"),
    ("pot", "Pot"),
    ("bunch", "Bunch"),
    ("clump", "Clump"),
]


# ── image download / upload (mirrors import_aquaticexotica) ───────────────────

def _download_image(url: str, retries: int = 3) -> Optional[tuple[bytes, str]]:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Junglyst-Importer/1.0"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read(), (resp.headers.get_content_type() or "image/jpeg")
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

    def read(self, *a):
        return self._buf.read(*a)

    def seek(self, *a):
        return self._buf.seek(*a)

    def tell(self):
        return self._buf.tell()


def _ext_from_url(url: str, content_type: str) -> str:
    path = urlparse(url).path
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        return last.rsplit(".", 1)[-1].lower().split("?")[0]
    return (mimetypes.guess_extension(content_type) or ".jpg").lstrip(".")


# ── value helpers ─────────────────────────────────────────────────────────────

def _dec(value, default: Optional[str] = None) -> Optional[Decimal]:
    if value in ("", None):
        return Decimal(default) if default is not None else None
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default) if default is not None else None


def _as_bool(value) -> bool:
    return str(value).strip().upper() in ("TRUE", "1", "YES")


def _infer_care(text: str) -> dict[str, str]:
    """Fill blanks from description text; fall back to model defaults."""
    t = (text or "").lower()

    care = "Easy"
    if "advanced" in t or "difficult" in t:
        care = "Advanced"
    elif "medium" in t and "care" in t:
        care = "Medium"

    light = "Medium"
    if any(p in t for p in ("low light", "low to medium", "low lighting")):
        light = "Low"
    elif any(p in t for p in ("high light", "high lighting", "strong light", "very bright")):
        light = "High"

    growth = "Moderate"
    if any(p in t for p in ("slow growth", "slow-growing", "extremely slow", "slow to moderate")):
        growth = "Slow"
    elif any(p in t for p in ("fast growth", "fast-growing")):
        growth = "Fast"

    co2 = "Low"
    if any(p in t for p in ("co2 required", "co2 injection", "additional co2", "co₂ injection")):
        co2 = "High"
    elif any(p in t for p in ("co2 recommended", "medium co2")):
        co2 = "Medium"

    return {"care_level": care, "light_requirements": light,
            "growth_rate": growth, "co2_requirement": co2}


def _infer_variant_type(name: str, notes: str) -> str:
    blob = f"{name} {notes}".lower()
    for needle, vtype in VARIANT_TYPE_RULES:
        if needle in blob:
            return vtype
    return "Plant"


def _name_slug(name: str, fallback: str, existing: set) -> str:
    base = slugify(name) or slugify(fallback) or "product"
    slug, counter = base, 1
    while slug in existing:
        slug = f"{base}-{counter}"
        counter += 1
    existing.add(slug)
    return slug


# ── command ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = ("Import Green Water Aquascapes products (from scrape_greenwater.py "
            "JSON) with buyer price = GWA price + 10 %.")

    def add_arguments(self, parser):
        parser.add_argument("--input", default=DEFAULT_INPUT,
                            help=f"Path to the scraped JSON (default: {DEFAULT_INPUT}).")
        parser.add_argument("--seller-id", default="",
                            help="UUID of the Junglyst seller user.")
        parser.add_argument("--seller-email", default="",
                            help="Email of the Junglyst seller user (alt to --seller-id).")
        parser.add_argument("--markup", type=str, default=str(MARKUP_PERCENT),
                            help="Percent added to GWA price (default: 10).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse and report but write nothing.")
        parser.add_argument("--update", action="store_true",
                            help="Overwrite products that already exist (by SKU).")
        parser.add_argument("--skip-images", action="store_true",
                            help="Keep source image URLs; do not re-upload to Firebase.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N products (0 = all).")

    def handle(self, *args, **opts):
        from core.models import (
            Category, Product, ProductImage, ProductVariant, SubCategory, Tag, User
        )

        dry_run = opts["dry_run"]
        do_update = opts["update"]
        skip_images = opts["skip_images"]
        limit = opts["limit"]
        markup = Decimal(str(opts["markup"]))

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written."))

        # ── load sheet ────────────────────────────────────────────────────────
        path = Path(opts["input"])
        if not path.exists():
            raise CommandError(f"Input file not found: {path.resolve()}")
        records = json.loads(path.read_text(encoding="utf-8"))
        if limit:
            records = records[:limit]
        self.stdout.write(f"Loaded  : {len(records)} products from {path}")

        # ── seller + commission pin ───────────────────────────────────────────
        seller = self._resolve_seller(User, opts["seller_id"], opts["seller_email"])
        self.stdout.write(f"Seller  : {seller.email}  (id={seller.id})")
        self._pin_commission(seller, markup, dry_run)

        # ── resolve base taxonomy once ────────────────────────────────────────
        base_cat = Category.objects.filter(name=BASE_CATEGORY).first()
        base_sub = (SubCategory.objects.filter(category=base_cat, name=BASE_SUBCATEGORY).first()
                    if base_cat else None)
        if not base_cat:
            self.stdout.write(self.style.WARNING(
                f"  ! Base category '{BASE_CATEGORY}' not found — products will "
                f"import without a category. Run seed_categories first."))

        existing_slugs: set = set(Product.all_objects.values_list("slug", flat=True))

        created = updated = skipped = img_ok = img_fail = errors = price_warn = 0

        for idx, rec in enumerate(records, 1):
            sku = (rec.get("external_product_id") or "").strip()
            name = (rec.get("name") or "").strip()
            try:
                action, n_ok, n_fail, warned = self._import_one(
                    rec=rec, seller=seller, base_cat=base_cat, base_sub=base_sub,
                    markup=markup, do_update=do_update, skip_images=skip_images,
                    dry_run=dry_run, existing_slugs=existing_slugs,
                    models=dict(Category=Category, Product=Product,
                                ProductImage=ProductImage, ProductVariant=ProductVariant,
                                SubCategory=SubCategory, Tag=Tag),
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(self.style.ERROR(
                    f"  [{idx}/{len(records)}] ERROR {sku} — {exc}"))
                continue

            created += action == "created"
            updated += action == "updated"
            skipped += action == "skipped"
            img_ok += n_ok
            img_fail += n_fail
            price_warn += warned
            label = {"created": "CREATED", "updated": "UPDATED", "skipped": "SKIPPED"}[action]
            note = f"  [{n_ok} imgs]" if n_ok else ""
            self.stdout.write(
                f"  [{idx}/{len(records)}] {label}  {sku:12} {name[:48]}{note}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("─" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created} updated={updated} skipped={skipped} "
            f"errors={errors} images={img_ok} img_fail={img_fail} "
            f"price_warnings={price_warn}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was saved."))

    # ── helpers ───────────────────────────────────────────────────────────────

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
        raise CommandError("Pass --seller-id or --seller-email to assign products.")

    def _pin_commission(self, seller, markup: Decimal, dry_run: bool):
        """Force buyer price = base_price * (1 + markup/100) via the model."""
        current = Decimal(str(seller.seller_commission_rate))
        if seller.price_is_buyer_final or current != markup:
            self.stdout.write(self.style.WARNING(
                f"  Pinning seller commission {current} -> {markup} % "
                f"(price_is_buyer_final -> False) so buyer price = GWA price + {markup} %."))
            if not dry_run:
                seller.seller_commission_rate = markup
                seller.price_is_buyer_final = False
                seller.save(update_fields=["seller_commission_rate", "price_is_buyer_final"])
        else:
            self.stdout.write(f"  Seller commission already {markup} % — good.")

    def _upload_image(self, url: str, seller_id) -> Optional[str]:
        from core.storage import upload_to_firebase
        result = _download_image(url)
        if not result:
            return None
        data, content_type = result
        ext = _ext_from_url(url, content_type)
        try:
            return upload_to_firebase(
                _ImageFile(data, f"product.{ext}", content_type),
                str(seller_id), "product")
        except Exception:
            return None

    @transaction.atomic
    def _import_one(self, *, rec, seller, base_cat, base_sub, markup, do_update,
                    skip_images, dry_run, existing_slugs, models):
        Product = models["Product"]
        ProductVariant = models["ProductVariant"]
        ProductImage = models["ProductImage"]
        SubCategory = models["SubCategory"]
        Category = models["Category"]
        Tag = models["Tag"]

        sku = (rec.get("external_product_id") or "").strip()
        name = (rec.get("name") or "").strip()
        if not sku or not name:
            raise ValueError("record missing SKU or name")

        # Idempotency: locate by unique variant SKU
        existing_variant = ProductVariant.all_objects.filter(sku=sku).first()
        existing = existing_variant.product if existing_variant else None
        if existing and not do_update:
            return "skipped", 0, 0, 0

        # care fields: use sheet value, else infer from description, else default
        inferred = _infer_care(rec.get("description", ""))
        care_level = rec.get("care_level") or inferred["care_level"]
        light = rec.get("light_requirements") or inferred["light_requirements"]
        growth = rec.get("growth_rate") or inferred["growth_rate"]
        co2 = rec.get("co2_requirement") or inferred["co2_requirement"]

        if dry_run:
            # still surface a price preview
            base_price = _dec(rec.get("source_retail_price"), "0")
            self._check_price(base_price, base_price * (1 + markup / 100), sku, preview=True)
            return ("updated" if existing else "created"), 0, 0, 0

        product_fields = dict(
            name=name,
            tagline=(rec.get("tagline") or "")[:500],
            description=rec.get("description") or name,
            seller=seller,
            scientific_name=(rec.get("scientific_name") or "")[:255],
            care_level=care_level,
            light_requirements=light,
            growth_rate=growth,
            co2_requirement=co2,
            water_temperature=(rec.get("water_temperature") or "") or None,
            ph_range=(rec.get("ph_range") or "") or None,
            is_rare=_as_bool(rec.get("is_rare")),
            origin=(rec.get("origin") or "") or None,
            is_active=_as_bool(rec.get("is_active")),
            is_draft=False,
            rating=_dec(rec.get("source_rating"), "5.0"),
        )

        if existing:
            for f, v in product_fields.items():
                setattr(existing, f, v)
            existing.save()
            product = existing
            action = "updated"
        else:
            slug = _name_slug(name, sku, existing_slugs)
            product = Product(slug=slug, **product_fields)
            product.save()
            action = "created"

        # ── taxonomy ──────────────────────────────────────────────────────────
        cats, subs = [], []
        if base_cat:
            cats.append(base_cat)
        if base_sub:
            subs.append(base_sub)
        mapping = SUBCATEGORY_MAP.get(rec.get("junglyst_subcategory", ""))
        if mapping:
            c = Category.objects.filter(name=mapping[0]).first()
            if c and c not in cats:
                cats.append(c)
            if c:
                s = SubCategory.objects.filter(category=c, name=mapping[1]).first()
                if s and s not in subs:
                    subs.append(s)
        if cats:
            product.categories.set(cats)
        if subs:
            product.sub_categories.set(subs)

        # ── tags ──────────────────────────────────────────────────────────────
        tag_names = [t for t in (rec.get("source_tags") or "").split("|") if t.strip()]
        tags = []
        for tn in tag_names:
            tag, _ = Tag.objects.get_or_create(name=tn.strip())
            tags.append(tag)
        if tags:
            product.tags.set(tags)

        # ── variant (pricing) ─────────────────────────────────────────────────
        base_price = _dec(rec.get("source_retail_price"), "0")          # GWA price
        compare_at = _dec(rec.get("source_compare_at_price"))           # struck-through
        if compare_at is not None and base_price is not None and compare_at <= base_price:
            compare_at = None
        stock = int(rec.get("stock") or 0)
        vtype = _infer_variant_type(name, rec.get("import_notes", ""))

        vfields = dict(
            name=rec.get("variant_name") or "Standard",
            variant_type=vtype,
            sku=sku,
            base_price=base_price,
            gst_rate=DEFAULT_GST_RATE,
            compare_at_price=compare_at,
            stock=stock,
            weight=DEFAULT_WEIGHT_KG,
            length=DEFAULT_LENGTH_CM,
            width=DEFAULT_WIDTH_CM,
            height=DEFAULT_HEIGHT_CM,
            item_category=rec.get("item_category") or "light",
            packed_weight_grams=int(rec.get("packed_weight_grams") or DEFAULT_PACKED_WEIGHT_GRAMS),
            is_active=_as_bool(rec.get("is_active")),
        )

        if existing_variant:
            for f, v in vfields.items():
                setattr(existing_variant, f, v)
            variant = existing_variant
        else:
            variant = ProductVariant(product=product, price=Decimal("0.00"), **vfields)
        variant.save()   # model computes price = base_price * (1 + seller_rate/100)

        # verify the 10 % markup actually landed
        expected = (base_price * (1 + markup / 100)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        warned = self._check_price(base_price, expected, sku, actual=variant.price)

        # ── images ────────────────────────────────────────────────────────────
        img_ok = img_fail = 0
        urls = [rec.get(f"image_url_{i}") for i in (1, 2, 3)]
        urls = [u for u in urls if u]
        if urls:
            if action == "updated":
                ProductImage.all_objects.filter(product=product).delete()
            for order, src in enumerate(urls):
                final_url = src if skip_images else self._upload_image(src, seller.id)
                if final_url:
                    ProductImage.objects.create(
                        product=product, variant=variant, image_url=final_url,
                        is_primary=(order == 0), order=order)
                    img_ok += 1
                else:
                    img_fail += 1

        return action, img_ok, img_fail, warned

    def _check_price(self, base_price, expected, sku, actual=None, preview=False) -> int:
        if base_price is None:
            return 0
        expected = Decimal(expected).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if preview:
            self.stdout.write(
                f"    {sku}: GWA ₹{base_price} -> buyer ₹{expected}")
            return 0
        if actual is not None and Decimal(actual).quantize(Decimal("0.01")) != expected:
            self.stderr.write(self.style.WARNING(
                f"    ! {sku}: price ₹{actual} != expected ₹{expected} "
                f"(check seller commission)"))
            return 1
        return 0
