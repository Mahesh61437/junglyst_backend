"""
sync_utils.py — live scraping + stock/price diff logic for the Stock & Price Sync feature.

Does NOT import scrape_petkadai.py or scrape_petkadai_category.py (which have a broken
dependency on the missing category_utils module). Instead it calls the PetKadai GraphQL
API directly and reuses only scrape_himadri.py (which has no problematic deps).
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
import re

import requests

# ── Constants ─────────────────────────────────────────────────────────────────

_PETKADAI_GQL = 'https://www.petkadai.com/graphql'
_PETKADAI_HEADERS = {
    'Content-Type': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
}

_PRODUCTS_QUERY = """
query SyncProducts($categoryUid: String!, $pageSize: Int!, $currentPage: Int!) {
  products(
    filter: { category_uid: { eq: $categoryUid } }
    pageSize: $pageSize
    currentPage: $currentPage
    sort: { position: ASC }
  ) {
    total_count
    items {
      sku
      name
      price_range {
        minimum_price {
          final_price   { value }
          regular_price { value }
        }
      }
      stock_status
      only_x_left_in_stock
      image { url }
      media_gallery { url }
    }
    page_info { current_page total_pages }
  }
}
"""

# Mirrors ALL_CATEGORIES from scrape_petkadai_category.py.
# 'Live Fishes' (and any shrimp/live-animal categories) are intentionally excluded —
# Junglyst no longer lists live fish/shrimp, only plants and aqua supplies.
_PETKADAI_CATEGORIES = [
    {'uid': 'NA==',  'name': 'Live Plants'},
    {'uid': 'OA==',  'name': 'Starters'},
    {'uid': 'MTA=',  'name': 'Aqua Essentials'},
    {'uid': 'MTI=',  'name': 'Fish Food'},
    {'uid': 'MTM=',  'name': 'Aqua Filters'},
    {'uid': 'MTQ=',  'name': 'Aquatic Remedies'},
    {'uid': 'MTU=',  'name': 'Planted Aquarium'},
    {'uid': 'MTk=',  'name': 'Aquarium Tanks'},
    {'uid': 'MjQ=',  'name': 'Aquarium Lights'},
    {'uid': 'MzE=',  'name': 'Driftwoods'},
    {'uid': 'MjE=',  'name': 'Toys & Decors'},
    # 'Birds' (uid 'MTg=') intentionally excluded — Junglyst has no bird category
    # and doesn't sell birds/bird supplies, same as Live Fishes above.
]

# Docker mounts E:\JungLyst → /project; outside Docker fall back to the relative path.
_ROOT = '/project' if os.path.isdir('/project') else os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..')
)

# Delisting (zero out stock for products not in scraped data) is only safe when
# we can trust the SKU is stable. Himadri's SKU scheme changed between the original
# import and the live site, so we disable delisting for it.
_DELIST_ENABLED = {
    'petkadai': True,
    'himadri':  False,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _q(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _parse_stock(val) -> int:
    if val is None:
        return 0
    s = str(val).lower().strip()
    if 'out of stock' in s or s in ('false', 'no', 'outofstock'):
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        m = re.search(r'(\d+)', s)
        return int(m.group(1)) if m else 0


def _petkadai_image_urls(item: dict) -> list[str]:
    """Collect image URLs from a raw PetKadai GraphQL item (main image + gallery)."""
    urls: list[str] = []
    main = (item.get('image') or {}).get('url')
    if main:
        urls.append(main)
    for entry in item.get('media_gallery') or []:
        url = entry.get('url')
        if url and url not in urls:
            urls.append(url)
    return urls


def _extract_petkadai(item: dict) -> dict | None:
    """Extract sync fields from a raw PetKadai GraphQL item."""
    sku = item.get('sku') or ''
    if not sku:
        return None

    price_info = item.get('price_range', {}).get('minimum_price', {})
    final_price = (price_info.get('final_price') or {}).get('value') or 0.0

    in_stock = item.get('stock_status') == 'IN_STOCK'
    raw_stock = item.get('only_x_left_in_stock')
    if raw_stock is not None:
        stock = _parse_stock(raw_stock)
    elif not in_stock:
        stock = 0
    else:
        stock = None  # IN_STOCK but PetKadai didn't report a count — don't overwrite DB value

    return {
        'sku': sku,
        'name': (item.get('name') or '')[:80],
        'new_base': _q(final_price),
        'new_stock': stock,
    }


def _extract_himadri(rec: dict, markup: Decimal) -> dict | None:
    """Extract sync fields from a raw Himadri scraped dict."""
    sku = rec.get('sku') or ''
    if not sku:
        return None
    # Use the minimum of sale_price and regular_price — in WooCommerce the current
    # displayed price is always the lower value (sale < regular when on sale).
    candidates = [p for p in [rec.get('sale_price'), rec.get('regular_price')] if p and p > 0]
    if not candidates:
        return None
    sale = min(candidates)
    new_base = (_q(sale) * markup).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return {
        'sku': sku,
        'name': (rec.get('name') or '')[:80],
        'new_base': new_base,
        'new_stock': _parse_stock(rec.get('stock')),
    }


# ── Live scrapers ─────────────────────────────────────────────────────────────

def _fetch_petkadai_category(uid: str, name: str) -> list[dict]:
    """Fetch all pages for one PetKadai category via GraphQL."""
    items: list[dict] = []
    page, page_size = 1, 50
    while True:
        payload = {
            'query': _PRODUCTS_QUERY,
            'variables': {'categoryUid': uid, 'pageSize': page_size, 'currentPage': page},
        }
        try:
            resp = requests.post(_PETKADAI_GQL, headers=_PETKADAI_HEADERS, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            break

        data = resp.json()
        if 'errors' in data:
            break

        products_data = data.get('data', {}).get('products', {})
        page_items = products_data.get('items', [])
        if not page_items:
            break
        items.extend(page_items)

        page_info = products_data.get('page_info', {})
        if page >= (page_info.get('total_pages') or 1):
            break
        page += 1

    return items


def scrape_live_petkadai(progress_cb=None) -> list[dict]:
    """Scrape all PetKadai categories via GraphQL. Returns raw item dicts."""
    records = []
    total = len(_PETKADAI_CATEGORIES)
    for i, cat in enumerate(_PETKADAI_CATEGORIES):
        if progress_cb:
            progress_cb(f'Scraping Pet Kadai: {cat["name"]} ({i + 1}/{total})…')
        items = _fetch_petkadai_category(cat['uid'], cat['name'])
        for item in items:
            item['images'] = _petkadai_image_urls(item)
            item['categories'] = [cat['name']]
        records.extend(items)
    return records


def scrape_live_himadri(progress_cb=None) -> list[dict]:
    """Scrape Himadri product pages from the live site.

    We use only the two parent category URLs (aquatic-plants, floating-plants) because
    the parent pages already include all sub-category products. Scraping parent + every
    sub-category would visit the same 500+ product URLs multiple times.
    """
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from scrape_himadri import get_all_product_urls, scrape_product

    # Parent categories only — sub-categories are a superset of their children
    _HIMADRI_PARENT_URLS = [
        'https://himadriaquatics.com/product-category/plants/aquatic-plants/',
        'https://himadriaquatics.com/product-category/plants/floating-plants/',
    ]

    seen: set[str] = set()
    records = []
    total = len(_HIMADRI_PARENT_URLS)
    for i, cat_url in enumerate(_HIMADRI_PARENT_URLS):
        if progress_cb:
            progress_cb(f'Scraping Himadri: {cat_url.split("/product-category/")[-1]} ({i + 1}/{total})…')
        try:
            urls = get_all_product_urls(cat_url)
        except Exception:
            continue
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            try:
                product = scrape_product(url)
                if product:
                    records.append(product)
            except Exception:
                pass
            time.sleep(0.3)
    return records


# ── Core diff + stock-apply logic ─────────────────────────────────────────────

def compute_diff(
    records: list[dict],
    source: str,
    seller_email: str,
    source_markup: Decimal | None,
    seller_commission: Decimal | None,
    buyer_commission: Decimal | None,
    progress_cb=None,
) -> dict:
    """
    Compare scraped records against the live DB.
    - Applies stock changes immediately (safe, no approval needed).
    - Returns price_changes list for admin approval.
    - Returns a session_key stored in cache for the apply step.
    """
    import uuid as _uuid
    from django.core.cache import cache
    from core.models import ProductVariant
    from core.feed import invalidate_feed_cache

    markup = (1 + (source_markup or Decimal('0')) / Decimal('100'))
    extractor = _extract_petkadai if source == 'petkadai' else lambda rec: _extract_himadri(rec, markup)

    stock_updated = stock_unchanged = not_found = delisted = 0
    price_changes = []
    new_products = []  # scraped products not found in DB — candidates for import

    if progress_cb:
        progress_cb(f'Comparing {len(records)} scraped products against DB…')

    # Build a set of all SKUs seen in this scrape so we can zero-out delisted items
    scraped_skus: set[str] = set()
    for rec in records:
        parsed_sku = extractor(rec)
        if parsed_sku:
            scraped_skus.add(parsed_sku['sku'])

    for rec in records:
        parsed = extractor(rec)
        if not parsed:
            continue

        try:
            variant = (
                ProductVariant.objects
                .select_related('product')
                .get(sku=parsed['sku'])
            )
        except ProductVariant.DoesNotExist:
            not_found += 1
            new_products.append({
                'sku':       parsed['sku'],
                'name':      parsed['name'],
                'new_base':  float(parsed['new_base']),
                'new_stock': parsed['new_stock'] or 0,
                'source':    source,
                '_raw': {
                    'description':     rec.get('description', ''),
                    'scientific_name': rec.get('scientific_name', ''),
                    'categories':      rec.get('categories', []),
                    'tags':            rec.get('tags', []),
                    'rating':          rec.get('rating', 5.0),
                    'images':          rec.get('images', []),
                    'url':             rec.get('url', ''),
                    'sale_price':      rec.get('sale_price'),
                    'regular_price':   rec.get('regular_price'),
                },
            })
            continue
        except ProductVariant.MultipleObjectsReturned:
            variant = (
                ProductVariant.objects
                .filter(sku=parsed['sku'])
                .select_related('product')
                .first()
            )

        # ── Stock: apply immediately ───────────────────────────────────────────
        new_stock = parsed['new_stock']
        if new_stock is None:
            # IN_STOCK but count unknown — leave DB value as-is
            stock_unchanged += 1
        elif variant.stock != new_stock:
            variant.stock = new_stock
            variant.save(update_fields=['stock'])
            stock_updated += 1
        else:
            stock_unchanged += 1

        # ── Price: queue for admin approval ───────────────────────────────────
        new_base = parsed['new_base']
        s_rate = (
            seller_commission
            if seller_commission is not None
            else (variant.commission_rate or Decimal('10'))
        )
        projected_price = (new_base * (1 + s_rate / Decimal('100'))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

        price_changed = abs(new_base - variant.base_price) >= Decimal('0.01')
        commission_changed = (
            seller_commission is not None
            and abs(s_rate - (variant.commission_rate or Decimal('10'))) >= Decimal('0.01')
        )

        if price_changed or commission_changed:
            change_pct = (
                float(((new_base - variant.base_price) / variant.base_price * 100)
                      .quantize(Decimal('0.1')))
                if variant.base_price else 0
            )
            price_changes.append({
                'id': str(variant.id),
                'product_id': str(variant.product.id),
                'name': parsed['name'],
                'sku': parsed['sku'],
                'old_base': float(variant.base_price),
                'new_base': float(new_base),
                'old_price': float(variant.price),
                'new_price': float(projected_price),
                'change_pct': change_pct,
                'seller_commission': float(s_rate),
                'buyer_commission': float(buyer_commission) if buyer_commission is not None else None,
                'source': source,
            })

    # ── Zero out stock for seller variants not seen in this scrape ────────────
    # Only for sources where SKU stability is trusted (see _DELIST_ENABLED).
    if seller_email and scraped_skus and _DELIST_ENABLED.get(source, False):
        delisted_qs = (
            ProductVariant.objects
            .filter(product__seller__email=seller_email, stock__gt=0)
            .exclude(sku__isnull=True)   # can't delist what we can't identify
            .exclude(sku__in=scraped_skus)
        )
        delisted = delisted_qs.count()
        if delisted:
            delisted_qs.update(stock=0)
            stock_updated += delisted

    if stock_updated > 0:
        invalidate_feed_cache()

    session_key = str(_uuid.uuid4())
    cache.set(
        f'stock_sync:pending:{session_key}',
        {item['id']: item for item in price_changes},
        3600,
    )
    cache.set(
        f'stock_sync:new_products:{session_key}',
        {p['sku']: p for p in new_products},
        3600,
    )

    return {
        'session_key': session_key,
        'source': source,
        'stock_updated': stock_updated,
        'stock_unchanged': stock_unchanged,
        'not_found': not_found,
        'delisted': delisted,
        'price_changes': sorted(price_changes, key=lambda x: abs(x['change_pct']), reverse=True),
        'new_products': sorted(new_products, key=lambda x: x['name']),
    }


# ── Category resolution (module-level so it's a single source of truth for ──
# both the live import path and any one-off recategorization scripts) ────────

# Himadri name fragment → (category_slug, subcategory_slug). Himadri's own
# WooCommerce category names are already fine-grained plant-type names.
CAT_MAP = [
    ('lillies',         ('plants', 'plants-lily')),
    ('lily',            ('plants', 'plants-lily')),
    ('lotus',           ('plants', 'plants-lily')),
    ('tissue culture',  ('plants', 'plants-tissue-culture')),
    ('tc cup',          ('plants', 'plants-tissue-culture')),
    ('carpet',          ('plants', 'plants-carpet-plants')),
    ('foreground',      ('plants', 'plants-carpet-plants')),
    ('moss',            ('plants', 'plants-mosses')),
    ('fern',            ('plants', 'plants-ferns')),
    ('anubias',         ('plants', 'plants-epiphytes')),
    ('lagenandra',      ('plants', 'plants-epiphytes')),
    ('bucephalandra',   ('plants', 'plants-epiphytes')),
    ('floating',        ('plants', 'plants-floating-plants')),
    ('pond',            ('plants', 'plants-floating-plants')),
    ('low tech',        ('plants', 'plants-beginner-friendly')),
    ('beginner',        ('plants', 'plants-beginner-friendly')),
    ('stem',            ('plants', 'aquatic-plants')),
    ('cryptocoryne',    ('plants', 'aquatic-plants')),
    ('echinodorus',     ('plants', 'aquatic-plants')),
    ('aquatic',         ('plants', 'aquatic-plants')),
]

# PetKadai source category → Junglyst (category, default subcategory). PetKadai's
# own category (attached to each scraped item as `categories: [name]` in
# scrape_live_petkadai) is a much more reliable signal than keyword-matching
# descriptions, and covers non-plant products the keyword map above never could.
# Keep in sync with _PETKADAI_CATEGORIES above — every non-excluded entry there
# must have a key here (see test_category_mapping.py).
PETKADAI_CATEGORY_MAP = {
    'live plants':      ('plants',               'aquatic-plants'),
    'starters':         ('aquascaping',           'aquascaping-starter-packs'),
    'aqua essentials':  ('aquarium-accessories',  'aquarium-accessories-tank-accessories'),
    'fish food':        ('fish-aquarium-care',    'fish-aquarium-care-fish-food'),
    'aqua filters':     ('aquarium-filters',      'aquarium-filters-accessories-spares'),
    'aquatic remedies': ('fish-aquarium-care',    'fish-aquarium-care-aquarium-medicines'),
    'planted aquarium': ('planted-aquarium-co2',  'planted-aquarium-co2-co2-accessories'),
    'aquarium tanks':   ('aquarium-tanks',        'aquarium-tanks-moulded-tanks'),
    'aquarium lights':  ('aquarium-lights',       'aquarium-lights-led-lights'),
    'driftwoods':       ('aquascaping',           'aquascaping-driftwood'),
    'toys & decors':    ('aquarium-accessories',  'aquarium-accessories-tank-accessories'),
}

# Keyword refinement applied within a PetKadai-mapped category, using the
# product name, to pick a more specific subcategory than the default above.
SUBCAT_REFINE = {
    'aquarium-filters': [
        ('canister',      'aquarium-filters-external-canister-filters'),
        ('hang on',       'aquarium-filters-hang-on-filters'),
        ('hob',           'aquarium-filters-hang-on-filters'),
        ('sponge',        'aquarium-filters-sponge-filters'),
        ('internal',      'aquarium-filters-internal-filters'),
        ('external',      'aquarium-filters-external-filters'),
    ],
    'aquarium-tanks': [
        ('rimless',       'aquarium-tanks-rimless-tanks'),
        ('ultra crystal', 'aquarium-tanks-ultra-crystal-clear-tanks'),
        ('crystal clear', 'aquarium-tanks-ultra-crystal-clear-tanks'),
    ],
    'aquarium-lights': [
        ('clip',          'aquarium-lights-clip-on-lights'),
    ],
    'planted-aquarium-co2': [
        ('diffuser',      'planted-aquarium-co2-co2-diffusers'),
        ('regulator',     'planted-aquarium-co2-co2-regulators'),
        ('kit',           'planted-aquarium-co2-complete-co2-kits'),
    ],
}


def resolve_category(scraped_cats: list[str], name: str = ''):
    """Return (Category, SubCategory | None) best matching scraped category list."""
    from core.models import Category, SubCategory

    combined = ' '.join(scraped_cats).lower()
    name_l = (name or '').lower()

    # 1) Direct match against the PetKadai source category (most reliable —
    #    covers non-plant products the keyword map below was never built for).
    for cat_name in scraped_cats:
        mapped = PETKADAI_CATEGORY_MAP.get((cat_name or '').strip().lower())
        if not mapped:
            continue
        cat_slug, default_sub_slug = mapped
        sub_slug = default_sub_slug
        for keyword, refined_sub_slug in SUBCAT_REFINE.get(cat_slug, []):
            if keyword in name_l:
                sub_slug = refined_sub_slug
                break
        try:
            cat = Category.objects.get(slug=cat_slug)
            sub = SubCategory.objects.get(slug=sub_slug)
            return cat, sub
        except (Category.DoesNotExist, SubCategory.DoesNotExist):
            pass

    # 2) Keyword match against scraped category text (Himadri path).
    for keyword, (cat_slug, sub_slug) in CAT_MAP:
        if keyword in combined:
            try:
                cat = Category.objects.get(slug=cat_slug)
                sub = SubCategory.objects.get(slug=sub_slug)
                return cat, sub
            except (Category.DoesNotExist, SubCategory.DoesNotExist):
                pass

    # fallback to Plants > Aquatic Plants
    try:
        return Category.objects.get(slug='plants'), SubCategory.objects.get(slug='aquatic-plants')
    except Exception:
        return None, None


def import_new_products(session_key: str, approved_skus: list[str], seller_email: str, seller_commission: Decimal | None) -> dict:
    """
    Create Product + ProductVariant + ProductImage for each approved SKU
    from the new_products session cache. Maps Himadri categories to Junglyst
    categories/subcategories and sets sensible defaults per plant type.
    """
    from django.core.cache import cache
    from core.models import Product, ProductVariant, ProductImage, User, VariantType, Category, SubCategory, Tag
    from core.feed import invalidate_feed_cache

    pending = cache.get(f'stock_sync:new_products:{session_key}', {})
    try:
        seller = User.objects.get(email=seller_email)
    except User.DoesNotExist:
        return {'imported': 0, 'errors': [f'Seller not found: {seller_email}']}

    s_rate = seller_commission if seller_commission is not None else Decimal(str(seller.seller_commission_rate))

    # ── Care defaults by subcategory slug ────────────────────────────────────
    # (care_level, light_requirements, growth_rate, co2_requirement)
    _CARE_DEFAULTS = {
        'plants-lily':              ('Easy',   'Low',    'Moderate', 'Low'),
        'plants-tissue-culture':    ('Easy',   'Medium', 'Moderate', 'Low'),
        'plants-carpet-plants':     ('Medium', 'High',   'Moderate', 'High'),
        'plants-mosses':            ('Easy',   'Low',    'Slow',     'Low'),
        'plants-ferns':             ('Easy',   'Low',    'Slow',     'Low'),
        'plants-epiphytes':         ('Easy',   'Low',    'Slow',     'Low'),
        'plants-floating-plants':   ('Easy',   'Low',    'Fast',     'Low'),
        'plants-beginner-friendly': ('Easy',   'Low',    'Moderate', 'Low'),
        'aquatic-plants':           ('Easy',   'Medium', 'Moderate', 'Low'),
    }
    _DEFAULT_CARE = ('Easy', 'Medium', 'Moderate', 'Low')

    # ── Variant type from SKU prefix / name ───────────────────────────────────
    def _infer_variant_type(sku: str, name: str) -> str:
        u = sku.upper()
        name_l = name.lower()
        if 'GTC' in u or u.startswith('TC') or 'tissue culture' in name_l or '[tc]' in name_l:
            return VariantType.TISSUE_CULTURE
        if u.startswith('POTA') or u.startswith('POT') or 'pot' in name_l:
            return VariantType.POT
        if u.startswith('POUCH') or 'pouch' in name_l:
            return VariantType.BUNCH
        if u.startswith('LILY') or 'lily' in name_l or 'lotus' in name_l or 'nymphaea' in name_l:
            return VariantType.BULB
        if u.startswith('RHIZ') or 'rhizome' in name_l:
            return VariantType.RHIZOME
        if u.startswith('MAT') or 'mat' in name_l:
            return VariantType.MAT
        if u.startswith('CUP') or 'cup' in name_l:
            return VariantType.CUP
        if u.startswith('CLUMP') or 'clump' in name_l:
            return VariantType.CLUMP
        if 'bunch' in name_l:
            return VariantType.BUNCH
        return VariantType.PLANT

    imported = 0
    errors = []

    for sku in approved_skus:
        item = pending.get(sku)
        if not item:
            errors.append(f'{sku}: not in pending session')
            continue

        raw = item.get('_raw', {})
        base_price = Decimal(str(item['new_base']))
        new_stock = item.get('new_stock', 0)

        try:
            if ProductVariant.objects.filter(sku=sku).exists():
                errors.append(f'{sku}: already exists')
                continue

            scraped_cats = raw.get('categories') or []
            category, sub_category = resolve_category(scraped_cats, item['name'])
            sub_slug = sub_category.slug if sub_category else None
            care_level, light_req, growth_rate, co2_req = _CARE_DEFAULTS.get(sub_slug, _DEFAULT_CARE)

            product = Product(
                name=item['name'],
                description=raw.get('description') or item['name'],
                seller=seller,
                scientific_name=raw.get('scientific_name') or '',
                rating=Decimal(str(raw.get('rating') or 5.0)),
                care_level=care_level,
                light_requirements=light_req,
                growth_rate=growth_rate,
                co2_requirement=co2_req,
                is_draft=False,
            )
            product.save()

            if category:
                product.categories.add(category)
            if sub_category:
                product.sub_categories.add(sub_category)

            # Tags
            for tag_name in (raw.get('tags') or [])[:10]:
                tag, _ = Tag.objects.get_or_create(name=tag_name.strip())
                product.tags.add(tag)

            # compare_at_price — use regular_price × markup if higher than sale
            sale_p = raw.get('sale_price')
            reg_p = raw.get('regular_price')
            compare_at_price = None
            if reg_p and sale_p and reg_p > sale_p:
                # reg_p was NOT multiplied by markup yet (raw scraper value)
                # new_base already has markup applied: new_base = sale_p × markup
                # So compare = reg_p × markup × (1 + s_rate/100)
                markup_factor = base_price / Decimal(str(sale_p)) if sale_p else Decimal('1')
                compare_base = (Decimal(str(reg_p)) * markup_factor).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
                compare_at_price = (compare_base * (1 + s_rate / Decimal('100'))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

            variant = ProductVariant(
                product=product,
                name='Standard',
                variant_type=_infer_variant_type(sku, item['name']),
                sku=sku,
                base_price=base_price,
                price=base_price,  # recomputed by save()
                compare_at_price=compare_at_price,
                stock=new_stock,
            )
            variant.save()  # recomputes price = base_price × (1 + seller_commission_rate/100)

            # Images
            for i, url in enumerate((raw.get('images') or [])[:5]):
                ProductImage.objects.create(
                    product=product,
                    variant=variant,
                    image_url=url,
                    is_primary=(i == 0),
                    order=i,
                )

            imported += 1
        except Exception as e:
            errors.append(f'{sku}: {e}')

    if imported > 0:
        invalidate_feed_cache()

    return {'imported': imported, 'errors': errors}
