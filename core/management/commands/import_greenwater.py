"""
Management command: import_greenwater

Upserts the Green Water Aquascapes WooCommerce product export into Junglyst
under the seller whose store slug is "green-water-aquascapes".

Inputs (both live in imports/seller_onboarding/):
    green_water_aquascapes_wc_export.xlsx     WooCommerce "Export products" file
    green_water_aquascapes_enrichment.json    curated species data, category
                                              extras, clean names and copy for
                                              rows the seller left blank

Usage:
    python manage.py import_greenwater --dry-run            # validate + preview, no writes
    python manage.py import_greenwater                      # upsert everything
    python manage.py import_greenwater --report out.csv     # also write a mapping report

Idempotency:
    Products are matched by variant SKU (e.g. "#GWAE025"). Existing products are
    updated in place (slug and id are kept); unknown SKUs are created. Images are
    replaced only when the export row has images and they differ from the current ones.
"""
from __future__ import annotations

import csv
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

ONBOARDING_DIR = Path(settings.BASE_DIR) / "imports" / "seller_onboarding"
DEFAULT_EXPORT_PATH = ONBOARDING_DIR / "green_water_aquascapes_wc_export.xlsx"
DEFAULT_ENRICHMENT_PATH = ONBOARDING_DIR / "green_water_aquascapes_enrichment.json"

TISSUE_CULTURE_SKU_PREFIX = "#GWAT"
TISSUE_CULTURE_NAME_SUFFIX = " (Tissue Culture)"
REDUCED_GST_TAX_CLASS = "5tax"
REDUCED_GST_RATE = Decimal("5.00")
ZERO = Decimal("0.00")

DEFAULT_PLANT_PACKED_WEIGHT_GRAMS = 150
DEFAULT_PLANT_WEIGHT_KG = Decimal("0.05")
DEFAULT_PLANT_DIMENSIONS_CM = (Decimal("7.5"), Decimal("7.5"), Decimal("5"))
SUPPLY_BOX_DIMENSIONS_CM = (Decimal("8"), Decimal("8"), Decimal("16"))

# A seller description shorter than this (as plain text) is treated as missing.
MIN_DESCRIPTION_CHARS = 60

PLANTS_CATEGORY = "Plants"
AQUATIC_PLANTS = "Aquatic Plants"
TISSUE_CULTURE_SUB = "Tissue Culture"
POT_PLANTS_SUB = "Pot Plants"
RARE_SUB = "Rare and Exotic"
BEGINNER_SUB = "Beginner Friendly"
TERRARIUM_CATEGORY = "Terrarium & Paludarium"

SELLER_LIGHT_TO_JUNGLYST = {
    "low": "Low",
    "medium": "Medium",
    "bright": "Medium to High",
    "very bright": "High",
}
SELLER_DIFFICULTY_TO_CARE_LEVEL = {
    "easy": "Easy",
    "medium": "Moderate",
    "difficult": "Advanced",
}
BEGINNER_LIGHT_LEVELS = {"Low", "Medium", "Low to Medium"}
SELLER_PLACEMENT_TO_TAG = {
    "fore ground plants": "Foreground",
    "mid ground plants": "Midground",
    "background plants": "Background",
}
PLANT_TYPE_TAG_ALIASES = {"Runner": "Runners"}

STEM_BUNCH_PATTERN = re.compile(r"(\d+)\s*stems?", re.IGNORECASE)
WP_SHORTCODE_PATTERN = re.compile(r"\[[a-z]+_[a-z_]+[^\]]*\]")
WIDE_GAP_PATTERN = re.compile(r"[ \t]{3,}")
WHITESPACE_PATTERN = re.compile(r"\s+")
BULLET_LINE_PATTERN = re.compile(r"^\s*[-•]\s+")

XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# ── Spreadsheet reading ───────────────────────────────────────────────────────

def _column_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref).group()
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def read_xlsx_rows(path: Path) -> list[dict]:
    """Read the first worksheet of an .xlsx file into a list of header-keyed dicts.

    WooCommerce names its export ".csv" even when it is an Excel workbook, so the
    file is opened as a zip regardless of extension. Stdlib only — no openpyxl.
    """
    try:
        workbook = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError) as exc:
        raise CommandError(f"Cannot open export workbook {path}: {exc}") from exc

    shared_strings: list[str] = []
    if "xl/sharedStrings.xml" in workbook.namelist():
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        for item in root.findall("m:si", XLSX_NS):
            shared_strings.append("".join(t.text or "" for t in item.iter(f"{{{XLSX_NS['m']}}}t")))

    sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    table: list[list[str]] = []
    for row in sheet.iter(f"{{{XLSX_NS['m']}}}row"):
        cells: dict[int, str] = {}
        for cell in row.findall("m:c", XLSX_NS):
            cell_type = cell.get("t")
            value_node = cell.find("m:v", XLSX_NS)
            if cell_type == "s":
                value = shared_strings[int(value_node.text)]
            elif cell_type == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{{{XLSX_NS['m']}}}t"))
            else:
                value = value_node.text if value_node is not None else ""
            cells[_column_index(cell.get("r"))] = value or ""
        width = max(cells) + 1 if cells else 0
        table.append([cells.get(i, "") for i in range(width)])

    if not table:
        return []
    header = table[0]
    return [
        dict(zip(header, values + [""] * (len(header) - len(values))))
        for values in table[1:]
        if any(v.strip() for v in values)
    ]


# ── Description cleaning ──────────────────────────────────────────────────────

class _DescriptionSanitizer(HTMLParser):
    """Turns seller WordPress HTML into a whitelist of <p>, <ul>/<ol>, <li> blocks
    with <strong>/<em>/<sub>/<sup> inline. All attributes, scripts, styles and
    unknown tags are dropped; text is re-escaped on output."""

    INLINE_TAGS = {"strong": "strong", "b": "strong", "em": "em", "i": "em", "sub": "sub", "sup": "sup"}
    BLOCK_TAGS = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "table"}
    LIST_TAGS = {"ul", "ol"}
    DROP_CONTENT_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []   # (kind, inline_html); kind: p | ul | ol
        self._fragments: list[str] = []
        self._open_inline: list[str] = []
        self._list_stack: list[str] = []
        self._in_list_item = False
        self._dropping = 0

    # block helpers
    def _flush(self):
        for tag in reversed(self._open_inline):
            self._fragments.append(f"</{tag}>")
        html = WHITESPACE_PATTERN.sub(" ", "".join(self._fragments)).strip()
        if _plain_text(html):
            if self._in_list_item and self._list_stack:
                kind = self._list_stack[-1]
            elif BULLET_LINE_PATTERN.match(html):
                kind, html = "ul", BULLET_LINE_PATTERN.sub("", html, count=1)
            else:
                kind = "p"
            self.blocks.append((kind, html))
        self._fragments = []
        self._open_inline = []

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_CONTENT_TAGS:
            self._dropping += 1
        elif tag in self.INLINE_TAGS:
            mapped = self.INLINE_TAGS[tag]
            self._fragments.append(f"<{mapped}>")
            self._open_inline.append(mapped)
        elif tag in self.LIST_TAGS:
            self._flush()
            self._list_stack.append(tag)
        elif tag == "li":
            self._flush()
            self._in_list_item = True
        elif tag in self.BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.DROP_CONTENT_TAGS:
            self._dropping = max(0, self._dropping - 1)
        elif tag in self.INLINE_TAGS:
            mapped = self.INLINE_TAGS[tag]
            if mapped in self._open_inline:
                # close everything opened after it, then the tag itself
                while self._open_inline:
                    closing = self._open_inline.pop()
                    self._fragments.append(f"</{closing}>")
                    if closing == mapped:
                        break
        elif tag == "li":
            self._flush()
            self._in_list_item = False
        elif tag in self.LIST_TAGS:
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
        elif tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._dropping:
            return
        text = data.replace("\xa0", " ")
        text = WIDE_GAP_PATTERN.sub(" · ", text)
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if index:
                reopen = list(self._open_inline)
                self._flush()
                for tag in reopen:
                    self._fragments.append(f"<{tag}>")
                self._open_inline = reopen
            self._fragments.append(escape(line, quote=False))

    def close(self):
        super().close()
        self._flush()


def _plain_text(html: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", re.sub(r"<[^>]+>", " ", html)).strip()


def _normalise_for_compare(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def apply_text_fixes(text: str, text_fixes: list[list[str]]) -> str:
    for wrong, right in text_fixes:
        text = text.replace(wrong, right)
    return text


def clean_description_html(raw: str, text_fixes: list[list[str]] = ()) -> str:
    """Normalise one WooCommerce description cell into safe, minimal HTML."""
    if not raw:
        return ""
    text = raw.replace("_x000D_", "").replace("\\n", "\n").replace("\r", "")
    text = WP_SHORTCODE_PATTERN.sub("", text)
    text = apply_text_fixes(text, text_fixes)

    sanitizer = _DescriptionSanitizer()
    sanitizer.feed(text)
    sanitizer.close()

    rendered: list[str] = []
    previous_plain = None
    open_list: Optional[str] = None
    for kind, inline_html in sanitizer.blocks:
        plain = _normalise_for_compare(_plain_text(inline_html))
        if plain == previous_plain:
            continue  # drop repeated title lines
        previous_plain = plain
        if kind in ("ul", "ol"):
            if open_list != kind:
                if open_list:
                    rendered.append(f"</{open_list}>")
                rendered.append(f"<{kind}>")
                open_list = kind
            rendered.append(f"<li>{inline_html}</li>")
        else:
            if open_list:
                rendered.append(f"</{open_list}>")
                open_list = None
            rendered.append(f"<p>{inline_html}</p>")
    if open_list:
        rendered.append(f"</{open_list}>")
    return "\n".join(rendered)


def plain_text_to_html(text: str) -> str:
    """Curated plain-text copy (paragraphs separated by blank lines) → <p> blocks."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n".join(f"<p>{escape(p, quote=False)}</p>" for p in paragraphs)


def merge_seller_descriptions(description_html: str, short_html: str) -> str:
    """Combine the long and short description, skipping whichever duplicates the other."""
    long_plain = _normalise_for_compare(_plain_text(description_html))
    short_plain = _normalise_for_compare(_plain_text(short_html))
    if len(_plain_text(description_html)) < MIN_DESCRIPTION_CHARS:
        description_html, long_plain = "", ""
    if len(_plain_text(short_html)) < MIN_DESCRIPTION_CHARS:
        short_html, short_plain = "", ""
    if not long_plain:
        return short_html
    if not short_plain or short_plain in long_plain:
        return description_html
    if long_plain in short_plain:
        return short_html
    return f"{description_html}\n{short_html}"


# ── Row → product spec ────────────────────────────────────────────────────────

@dataclass
class VariantSpec:
    sku: str
    name: str
    variant_type: str
    base_price: Decimal
    regular_price: Optional[Decimal]
    gst_rate: Decimal
    stock: int
    weight_kg: Decimal
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal
    packed_weight_grams: int


@dataclass
class ProductSpec:
    sku: str
    name: str
    tagline: str
    description: str
    scientific_name: str
    origin: str
    care_level: str
    light_requirements: str
    growth_rate: str
    co2_requirement: str
    water_temperature: str
    ph_range: str
    is_rare: bool
    categories: list[tuple[str, Optional[str]]]
    tags: list[str]
    image_urls: list[str]
    variant: VariantSpec
    warnings: list[str] = field(default_factory=list)
    species_key: str = ""
    # Photos of the same plant sold in another form (pot ↔ tissue culture); used
    # only when this row has no images and the product has none stored either.
    fallback_image_urls: list[str] = field(default_factory=list)


def parse_decimal(value: str) -> Optional[Decimal]:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number > 0 else None


def parse_attributes(row: dict) -> dict[str, str]:
    attributes = {}
    for index in range(1, 10):
        name = (row.get(f"Attribute {index} name") or "").strip()
        value = (row.get(f"Attribute {index} value(s)") or "").strip()
        if name and value:
            attributes[name] = value
    return attributes


def parse_image_urls(cell: str) -> list[str]:
    urls = []
    for candidate in (cell or "").split(","):
        url = candidate.strip()
        if url.startswith("https://") and url not in urls:
            urls.append(url)
    return urls


def seller_categories(row: dict) -> list[str]:
    return [c.strip() for c in (row.get("Categories") or "").split(",") if c.strip()]


def map_light(seller_value: Optional[str]) -> Optional[str]:
    return SELLER_LIGHT_TO_JUNGLYST.get((seller_value or "").strip().lower())


def map_care_level(seller_value: Optional[str]) -> Optional[str]:
    return SELLER_DIFFICULTY_TO_CARE_LEVEL.get((seller_value or "").strip().lower())


def is_tissue_culture(sku: str) -> bool:
    return sku.upper().startswith(TISSUE_CULTURE_SKU_PREFIX)


def placement_tags(row: dict) -> list[str]:
    tags = []
    for category in seller_categories(row):
        leaf = category.split(">")[-1].strip().lower()
        tag = SELLER_PLACEMENT_TO_TAG.get(leaf)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def plant_variant(sku: str, attributes: dict, overrides: dict) -> tuple[str, str]:
    if overrides.get("variant_name"):
        return overrides["variant_name"], overrides.get("variant_type", "Other")
    if is_tissue_culture(sku):
        return "Tissue Culture Cup", "Tissue Culture"
    bunch = STEM_BUNCH_PATTERN.search(attributes.get("Packing", ""))
    if bunch:
        return f"{bunch.group(1)} Stems", "Bunch"
    return "Pot", "Pot"


def is_beginner_friendly(species: dict) -> bool:
    """Junglyst merchandising decision, so it uses the curated species data rather
    than the seller's difficulty labels (which rate every Anubias 'Medium')."""
    return (
        not species.get("is_terrestrial")
        and species["care_level"] == "Easy"
        and species["light_requirements"] in BEGINNER_LIGHT_LEVELS
    )


def plant_categories(species: dict, sku: str, variant_type: str) -> list[tuple[str, Optional[str]]]:
    if species.get("is_terrestrial"):
        categories: list[tuple[str, Optional[str]]] = [(TERRARIUM_CATEGORY, None)]
    else:
        categories = [(PLANTS_CATEGORY, AQUATIC_PLANTS)]
        if is_tissue_culture(sku):
            categories.append((PLANTS_CATEGORY, TISSUE_CULTURE_SUB))
        elif variant_type == "Pot":
            categories.append((PLANTS_CATEGORY, POT_PLANTS_SUB))
    for sub_name in species.get("subcategories", []):
        categories.append((PLANTS_CATEGORY, sub_name))
    if species.get("is_rare"):
        categories.append((PLANTS_CATEGORY, RARE_SUB))
    if is_beginner_friendly(species):
        categories.append((PLANTS_CATEGORY, BEGINNER_SUB))
    return list(dict.fromkeys(categories))


def seller_description(row: dict, text_fixes) -> str:
    return merge_seller_descriptions(
        clean_description_html(row.get("Description", ""), text_fixes),
        clean_description_html(row.get("Short description", ""), text_fixes),
    )


def _base_variant(row: dict, sku: str, name: str, variant_type: str, warnings: list[str]) -> VariantSpec:
    sale_price = parse_decimal(row.get("Sale price"))
    regular_price = parse_decimal(row.get("Regular price"))
    base_price = sale_price or regular_price
    if base_price is None:
        warnings.append("no valid price")
        base_price = ZERO
    try:
        stock = max(0, int(float(row.get("Stock") or 0)))
    except ValueError:
        stock = 0
    gst_rate = REDUCED_GST_RATE if (row.get("Tax class") or "").strip() == REDUCED_GST_TAX_CLASS else ZERO
    return VariantSpec(
        sku=sku, name=name, variant_type=variant_type,
        base_price=base_price, regular_price=regular_price, gst_rate=gst_rate, stock=stock,
        weight_kg=parse_decimal(row.get("Weight (kg)")) or DEFAULT_PLANT_WEIGHT_KG,
        length_cm=parse_decimal(row.get("Length (cm)")) or DEFAULT_PLANT_DIMENSIONS_CM[0],
        width_cm=parse_decimal(row.get("Width (cm)")) or DEFAULT_PLANT_DIMENSIONS_CM[1],
        height_cm=parse_decimal(row.get("Height (cm)")) or DEFAULT_PLANT_DIMENSIONS_CM[2],
        packed_weight_grams=DEFAULT_PLANT_PACKED_WEIGHT_GRAMS,
    )


def build_supply_spec(row: dict, supply: dict, text_fixes) -> ProductSpec:
    sku = row["SKU"].strip()
    warnings: list[str] = []
    variant = _base_variant(row, sku, supply["variant_name"], "Other", warnings)
    variant.packed_weight_grams = supply["packed_weight_grams"]
    variant.weight_kg = Decimal(supply["packed_weight_grams"]) / Decimal(1000)
    variant.length_cm, variant.width_cm, variant.height_cm = SUPPLY_BOX_DIMENSIONS_CM
    description = seller_description(row, text_fixes)
    if not description:
        warnings.append("no description")
    return ProductSpec(
        sku=sku, name=supply["name"], tagline=supply["tagline"], description=description,
        scientific_name="", origin="India", care_level="Easy", light_requirements="",
        growth_rate="", co2_requirement="Low", water_temperature="", ph_range="",
        is_rare=False, categories=[tuple(c) for c in supply["categories"]], tags=[],
        image_urls=parse_image_urls(row.get("Images")), variant=variant, warnings=warnings,
    )


def build_plant_spec(row: dict, product_meta: dict, species: dict,
                     rows_by_sku: dict, text_fixes) -> ProductSpec:
    sku = row["SKU"].strip()
    warnings: list[str] = []
    attributes = parse_attributes(row)

    light = map_light(attributes.get("Light Requirement")) or species["light_requirements"]
    care_level = map_care_level(attributes.get("Plant Difficulty")) or species["care_level"]
    variant_name, variant_type = plant_variant(sku, attributes, product_meta)

    name = product_meta.get("name") or species["display_name"]
    if is_tissue_culture(sku) and not product_meta.get("name"):
        name += TISSUE_CULTURE_NAME_SUFFIX

    if product_meta.get("use_species_description"):
        description = ""
    elif product_meta.get("description_from"):
        description = seller_description(rows_by_sku[product_meta["description_from"]], text_fixes)
    else:
        description = seller_description(row, text_fixes)
    if not description and species.get("description"):
        description = plain_text_to_html(species["description"])
    if not description:
        warnings.append("no description")

    tags = []
    plant_type = attributes.get("Plant Type")
    if plant_type:
        tags.append(PLANT_TYPE_TAG_ALIASES.get(plant_type, plant_type))
    tags.extend(placement_tags(row))

    variant = _base_variant(row, sku, variant_name, variant_type, warnings)
    return ProductSpec(
        sku=sku, name=name, tagline=species["tagline"], description=description,
        scientific_name=species["scientific_name"], origin=species["origin"],
        care_level=care_level, light_requirements=light,
        growth_rate=species["growth_rate"], co2_requirement=species["co2_requirement"],
        water_temperature=species["water_temperature"], ph_range=species["ph_range"],
        is_rare=bool(species.get("is_rare")),
        categories=plant_categories(species, sku, variant_type),
        tags=list(dict.fromkeys(tags)), image_urls=parse_image_urls(row.get("Images")),
        variant=variant, warnings=warnings,
    )


def build_product_specs(rows: list[dict], enrichment: dict) -> list[ProductSpec]:
    """Map every export row to a ProductSpec. Raises CommandError on unmapped SKUs."""
    text_fixes = enrichment.get("text_fixes", [])
    rows_by_sku = {row["SKU"].strip(): row for row in rows}
    unmapped = [
        sku for sku in rows_by_sku
        if sku not in enrichment["supplies"] and sku not in enrichment["products"]
    ]
    if unmapped:
        raise CommandError(f"SKUs missing from the enrichment file: {', '.join(unmapped)}")

    specs = []
    for sku, row in rows_by_sku.items():
        if sku in enrichment["supplies"]:
            specs.append(build_supply_spec(row, enrichment["supplies"][sku], text_fixes))
            continue
        product_meta = enrichment["products"][sku]
        species_key = product_meta["species"]
        if species_key not in enrichment["species"]:
            raise CommandError(f"{sku}: unknown species '{species_key}' in enrichment file")
        spec = build_plant_spec(row, product_meta, enrichment["species"][species_key], rows_by_sku, text_fixes)
        spec.species_key = species_key
        specs.append(spec)

    _assign_sibling_images(specs)
    return specs


def _assign_sibling_images(specs: list[ProductSpec]) -> None:
    images_by_species: dict[str, list[str]] = {}
    for spec in specs:
        if spec.species_key and spec.image_urls:
            images_by_species.setdefault(spec.species_key, spec.image_urls)
    for spec in specs:
        if not spec.image_urls:
            spec.fallback_image_urls = images_by_species.get(spec.species_key, [])


# ── Signal muting for bulk writes ─────────────────────────────────────────────

@contextmanager
def bulk_import_signals_muted():
    """Every Product/Variant save normally clears the whole cache and queues a feed
    rebuild. For a bulk import that is hundreds of flushes and Celery tasks, so
    mute those receivers and let the caller invalidate once at the end."""
    from django.db.models.signals import post_delete, post_save
    from core import signals as core_signals
    from core.models import Product, ProductVariant

    muted = [
        (post_save, core_signals.clear_api_cache, Product),
        (post_delete, core_signals.clear_api_cache, Product),
        (post_save, core_signals.product_feed_invalidate, Product),
        (post_delete, core_signals.product_feed_invalidate, Product),
        (post_save, core_signals.variant_stock_invalidate, ProductVariant),
    ]
    for signal, receiver, sender in muted:
        signal.disconnect(receiver, sender=sender)
    try:
        yield
    finally:
        for signal, receiver, sender in muted:
            signal.connect(receiver, sender=sender)


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Upsert the Green Water Aquascapes WooCommerce export into Junglyst (matched by SKU)."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_EXPORT_PATH), help="WooCommerce export (.xlsx).")
        parser.add_argument("--enrichment", default=str(DEFAULT_ENRICHMENT_PATH), help="Enrichment JSON.")
        parser.add_argument("--dry-run", action="store_true", help="Validate and preview; write nothing.")
        parser.add_argument("--report", default="", help="Optional CSV path for a per-product mapping report.")

    def handle(self, *args, **options):
        from sellers.models import SellerProfile

        try:
            enrichment = json.loads(Path(options["enrichment"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read enrichment file: {exc}") from exc

        rows = read_xlsx_rows(Path(options["file"]))
        if not rows:
            raise CommandError("The export has no product rows.")
        specs = build_product_specs(rows, enrichment)

        store_slug = enrichment["seller_store_slug"]
        profile = SellerProfile.objects.select_related("user").filter(slug=store_slug).first()
        if profile is None:
            raise CommandError(f"No seller profile with store slug '{store_slug}'.")
        seller = profile.user
        self.stdout.write(f"Seller : {profile.store_name} <{seller.email}>")
        self.stdout.write(f"Rows   : {len(specs)}")

        categories, subcategories = self._resolve_taxonomy(specs)
        existing_variants = self._existing_variants(specs)

        foreign = [
            spec.sku for spec in specs
            if spec.sku in existing_variants and existing_variants[spec.sku].product.seller_id != seller.id
        ]
        if foreign:
            raise CommandError(f"SKUs already belong to another seller: {', '.join(foreign)}")

        actions = {spec.sku: ("update" if spec.sku in existing_variants else "create") for spec in specs}
        if options["dry_run"]:
            self._print_preview(specs, actions)
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
        else:
            self._write_all(specs, seller, existing_variants, categories, subcategories)

        if options["report"]:
            self._write_report(Path(options["report"]), specs, actions)
            self.stdout.write(f"Report written to {options['report']}")

    # ── lookups (all batched; no per-row queries) ─────────────────────────────

    def _resolve_taxonomy(self, specs):
        from core.models import Category, SubCategory

        wanted_categories = {cat for spec in specs for cat, _ in spec.categories}
        categories = {c.name: c for c in Category.objects.filter(name__in=wanted_categories)}
        missing = wanted_categories - categories.keys()
        if missing:
            raise CommandError(f"Categories not found: {', '.join(sorted(missing))}")

        wanted_pairs = {(cat, sub) for spec in specs for cat, sub in spec.categories if sub}
        subcategories = {
            (s.category.name, s.name): s
            for s in SubCategory.objects.select_related("category").filter(
                category__name__in={cat for cat, _ in wanted_pairs},
                name__in={sub for _, sub in wanted_pairs},
            )
        }
        missing_pairs = wanted_pairs - subcategories.keys()
        if missing_pairs:
            readable = ", ".join(f"{c} > {s}" for c, s in sorted(missing_pairs))
            raise CommandError(f"Subcategories not found: {readable}")
        return categories, subcategories

    def _existing_variants(self, specs):
        from core.models import ProductVariant

        return {
            variant.sku: variant
            for variant in ProductVariant.all_objects.select_related("product").filter(
                sku__in=[spec.sku for spec in specs]
            )
        }

    # ── writes ────────────────────────────────────────────────────────────────

    def _write_all(self, specs, seller, existing_variants, categories, subcategories):
        from django.core.cache import cache
        from core.feed import invalidate_feed_cache
        from core.models import Tag

        tag_names = {name for spec in specs for name in spec.tags}
        tags = {t.name: t for t in Tag.objects.filter(name__in=tag_names)}
        for name in sorted(tag_names - tags.keys()):
            tags[name] = Tag.objects.create(name=name)

        created = updated = failed = 0
        with bulk_import_signals_muted():
            for index, spec in enumerate(specs, start=1):
                try:
                    action = self._upsert(spec, seller, existing_variants.get(spec.sku),
                                          categories, subcategories, tags)
                except Exception as exc:  # report the row and continue with the rest
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  [{index}/{len(specs)}] ERROR {spec.sku}: {exc}"))
                    continue
                created += action == "created"
                updated += action == "updated"
                note = f"  ⚠ {', '.join(spec.warnings)}" if spec.warnings else ""
                self.stdout.write(f"  [{index}/{len(specs)}] {action.upper():7} {spec.sku:<11} {spec.name}{note}")

        cache.clear()
        invalidate_feed_cache()
        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created} updated={updated} failed={failed}"
        ))

    @transaction.atomic
    def _upsert(self, spec: ProductSpec, seller, existing_variant, categories, subcategories, tags) -> str:
        from core.models import Product, ProductVariant

        product_fields = dict(
            name=spec.name, tagline=spec.tagline, description=spec.description,
            scientific_name=spec.scientific_name, origin=spec.origin,
            care_level=spec.care_level, light_requirements=spec.light_requirements,
            growth_rate=spec.growth_rate, co2_requirement=spec.co2_requirement,
            water_temperature=spec.water_temperature, ph_range=spec.ph_range,
            is_rare=spec.is_rare, is_active=True, is_draft=False,
        )
        if existing_variant:
            product = existing_variant.product
            for field_name, value in product_fields.items():
                setattr(product, field_name, value)
            product.is_deleted, product.deleted_at = False, None
            product.save()
            variant = existing_variant
            action = "updated"
        else:
            product = Product(seller=seller, **product_fields)
            product.save()
            variant = ProductVariant(product=product, sku=spec.sku, price=ZERO)
            action = "created"

        product.categories.set([categories[cat] for cat in dict.fromkeys(c for c, _ in spec.categories)])
        product.sub_categories.set([subcategories[(c, s)] for c, s in spec.categories if s])
        product.tags.set([tags[name] for name in spec.tags])

        self._save_variant(variant, spec.variant)
        self._sync_images(product, variant, spec.image_urls, spec.fallback_image_urls)
        return action

    @staticmethod
    def _save_variant(variant, spec: VariantSpec):
        variant.name = spec.name
        variant.variant_type = spec.variant_type
        variant.base_price = spec.base_price
        variant.gst_rate = spec.gst_rate
        variant.stock = spec.stock
        variant.weight = spec.weight_kg
        variant.length, variant.width, variant.height = spec.length_cm, spec.width_cm, spec.height_cm
        variant.item_category = "light"
        variant.packed_weight_grams = spec.packed_weight_grams
        variant.is_active = True
        variant.is_deleted, variant.deleted_at = False, None
        variant.compare_at_price = None
        variant.save()  # computes the buyer price from the seller's commission settings

        # Only show the seller's regular price as a strike-through when it is
        # actually above what the buyer pays after commission.
        if spec.regular_price and spec.regular_price > variant.price:
            variant.compare_at_price = spec.regular_price
            variant.save(update_fields=["compare_at_price"])

    @staticmethod
    def _sync_images(product, variant, image_urls: list[str], fallback_urls: list[str]):
        from core.models import ProductImage

        current = list(ProductImage.objects.filter(product=product).order_by("order"))
        if not image_urls:
            if current or not fallback_urls:
                return  # keep whatever the product already has
            image_urls = fallback_urls
        if [image.image_url for image in current] == image_urls:
            return
        ProductImage.objects.filter(product=product).update(is_deleted=True, deleted_at=timezone.now())
        ProductImage.objects.bulk_create([
            ProductImage(product=product, variant=variant, image_url=url, is_primary=(order == 0), order=order)
            for order, url in enumerate(image_urls)
        ])

    # ── output ────────────────────────────────────────────────────────────────

    def _print_preview(self, specs, actions):
        for spec in specs:
            categories = "; ".join(f"{c} > {s}" if s else c for c, s in spec.categories)
            note = f"  ⚠ {', '.join(spec.warnings)}" if spec.warnings else ""
            self.stdout.write(
                f"  {actions[spec.sku].upper():6} {spec.sku:<11} {spec.name[:55]:<55} "
                f"₹{spec.variant.base_price} x{spec.variant.stock} | {categories}{note}"
            )
        counts = {a: list(actions.values()).count(a) for a in ("create", "update")}
        self.stdout.write(f"Would create {counts['create']} and update {counts['update']} products.")

    @staticmethod
    def _write_report(path: Path, specs, actions):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "action", "sku", "name", "categories", "variant", "seller_price", "regular_price",
                "stock", "care_level", "light", "growth_rate", "co2", "scientific_name", "origin",
                "temperature", "ph", "is_rare", "tags", "images", "sibling_images", "description_chars", "warnings",
            ])
            for spec in specs:
                writer.writerow([
                    actions[spec.sku], spec.sku, spec.name,
                    "; ".join(f"{c} > {s}" if s else c for c, s in spec.categories),
                    f"{spec.variant.name} ({spec.variant.variant_type})",
                    spec.variant.base_price, spec.variant.regular_price or "", spec.variant.stock,
                    spec.care_level, spec.light_requirements, spec.growth_rate, spec.co2_requirement,
                    spec.scientific_name, spec.origin, spec.water_temperature, spec.ph_range,
                    spec.is_rare, ", ".join(spec.tags), len(spec.image_urls), len(spec.fallback_image_urls),
                    len(_plain_text(spec.description)), "; ".join(spec.warnings),
                ])
