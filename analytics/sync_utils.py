"""
sync_utils.py — live scraping + stock/price diff logic for the Stock & Price Sync feature.

Does NOT import scrape_petkadai.py or scrape_petkadai_category.py (which have a broken
dependency on the missing category_utils module). Instead it calls the PetKadai GraphQL
API directly and uses analytics/himadri_scraper.py for Himadri.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
import re

import requests


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts).strip()


def _strip_html(html: str) -> str:
    if not html:
        return ""
    s = _HTMLStripper()
    s.feed(html)
    return re.sub(r"\s+", " ", s.get_text()).strip()

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
      short_description { html }
      description { html }
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

# Delisting (zero out stock for products not in scraped data) is only safe when
# we can trust the SKU is stable. Himadri's SKU scheme changed between the original
# import and the live site, so we disable delisting for it.
_DELIST_ENABLED = {
    'petkadai': True,
    'himadri':  False,
}


def find_seller_by_email(seller_email: str):
    """The seller account for an email typed in the sync form, or None.

    Emails are stored with whatever case the user signed up with
    ("Support@petkadai.com"), so an exact match rejects the same address typed in
    lower case. Returns None when no account matches or the match is ambiguous."""
    from core.models import User
    matches = list(User.objects.filter(email__iexact=(seller_email or '').strip())[:2])
    return matches[0] if len(matches) == 1 else None


# Delisting only runs when at least this share of the seller's in-stock SKUs showed up
# in the scrape. Picking the wrong seller, or a scrape that came back mostly empty,
# matches almost nothing — and would otherwise zero the seller's whole catalogue.
_MIN_MATCH_RATIO_FOR_DELIST = 0.5


def _delist_block_reason(scrape_incomplete, in_stock_skus: set, scraped_skus: set) -> str | None:
    """Why zeroing missing products must not run, or None when it is safe."""
    if scrape_incomplete:
        return ('Scrape was incomplete (' + ', '.join(scrape_incomplete) +
                '); stock was not zeroed for products missing from it.')
    if in_stock_skus:
        matched = len(in_stock_skus & scraped_skus)
        if matched / len(in_stock_skus) < _MIN_MATCH_RATIO_FOR_DELIST:
            return (f'Only {matched} of {len(in_stock_skus)} in-stock products for this seller were '
                    'found in the scrape — wrong seller selected? Stock was not zeroed.')
    return None


# Preview sessions are kept for a day. Purging is opportunistic — it runs when a
# new preview is taken, which is also the only time the table grows. An older row
# that has not been purged yet still works; correctness does not rest on this
# window but on the old_base check in StockSyncApplyView, which refuses any write
# that would clobber a value changed since the preview.
_SESSION_RETENTION_DAYS = 1


def _load_session(session_key: str):
    """Fetch a sync session by key, tolerating a malformed or unknown key."""
    from analytics.models import StockSyncSession
    try:
        return StockSyncSession.objects.get(key=session_key)
    except (StockSyncSession.DoesNotExist, ValueError, TypeError):
        return None


def _purge_stale_sessions():
    """Drop sessions older than the retention window. Returns Django's delete() tuple."""
    from datetime import timedelta
    from django.utils import timezone
    from analytics.models import StockSyncSession
    cutoff = timezone.now() - timedelta(days=_SESSION_RETENTION_DAYS)
    return StockSyncSession.objects.filter(created_at__lt=cutoff).delete()


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


# WooCommerce shows a placeholder count (e.g. "99998347 in stock") for products whose
# stock is not tracked. At or above this it is not a real quantity.
_UNTRACKED_STOCK_THRESHOLD = 5000

# New products shown as in stock but without a usable count are offered with this
# stock. The preview marks it as an estimate so it can be reviewed.
_UNKNOWN_COUNT_IMPORT_STOCK = 5

_SKU_MAX_LENGTH = 100


def _himadri_stock(rec: dict) -> int | None:
    """0 when out of stock, the real count when shown, None when in stock without a usable count.

    None means "leave the stored stock alone" — an in-stock product with no count must
    not be written as 0, which is what reading a blank stock label as a number did."""
    status = (rec.get('stock_status') or '').lower()
    if 'out of stock' in status:
        return 0
    match = re.search(r'(\d+)', status)
    if not match:
        return None
    count = int(match.group(1))
    return count if 0 < count < _UNTRACKED_STOCK_THRESHOLD else None


def _himadri_key_sku(rec: dict) -> str:
    """The product's SKU, or HIM-<url-slug> when Himadri lists it without one.

    About two thirds of Himadri's products have no SKU. The URL slug is stable and
    unique, so it stands in as the key; products imported with it match next time."""
    sku = (rec.get('sku') or '').strip()
    if sku:
        return sku
    slug = (rec.get('slug') or urlparse(rec.get('url') or '').path.strip('/').rsplit('/', 1)[-1]).strip()
    return f'HIM-{slug}'[:_SKU_MAX_LENGTH] if slug else ''


def _extract_himadri(rec: dict, markup: Decimal) -> dict | None:
    """Extract sync fields from a raw Himadri record (page scraper or Store API)."""
    sku = _himadri_key_sku(rec)
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
        'full_name': rec.get('name') or '',
        'new_base': new_base,
        'new_stock': _himadri_stock(rec),
    }


# ── Live scrapers ─────────────────────────────────────────────────────────────

@dataclass
class ScrapeResult:
    """Scraped records plus the parts of the scrape that did not complete.

    A scrape that silently lost a page looks identical to one where those
    products were delisted, so callers must be able to tell the difference."""
    records: list[dict]
    incomplete: list[str] = field(default_factory=list)


def _fetch_petkadai_category(uid: str, name: str) -> tuple[list[dict], bool]:
    """Fetch all pages for one PetKadai category via GraphQL.

    Returns (items, complete). complete is False when a request failed, the API
    reported errors, or fewer items arrived than the API's own total_count."""
    items: list[dict] = []
    total_count = None
    page, page_size = 1, 50
    while True:
        payload = {
            'query': _PRODUCTS_QUERY,
            'variables': {'categoryUid': uid, 'pageSize': page_size, 'currentPage': page},
        }
        try:
            resp = requests.post(_PETKADAI_GQL, headers=_PETKADAI_HEADERS, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            return items, False
        if 'errors' in data:
            return items, False

        products_data = (data.get('data') or {}).get('products') or {}
        if total_count is None:
            total_count = products_data.get('total_count')
        page_items = products_data.get('items') or []
        if not page_items:
            break
        items.extend(page_items)

        page_info = products_data.get('page_info') or {}
        if page >= (page_info.get('total_pages') or 1):
            break
        page += 1

    complete = total_count is not None and len(items) >= total_count
    return items, complete


def scrape_live_petkadai(progress_cb=None) -> ScrapeResult:
    """Scrape all PetKadai categories via GraphQL."""
    records = []
    incomplete = []
    total = len(_PETKADAI_CATEGORIES)
    for i, cat in enumerate(_PETKADAI_CATEGORIES):
        if progress_cb:
            progress_cb(f'Scraping Pet Kadai: {cat["name"]} ({i + 1}/{total})…')
        items, complete = _fetch_petkadai_category(cat['uid'], cat['name'])
        if not complete:
            incomplete.append(cat['name'])
        for item in items:
            item['images'] = _petkadai_image_urls(item)
            item['categories'] = [cat['name']]
            desc_text  = _strip_html((item.get('description') or {}).get('html', ''))
            short_text = _strip_html((item.get('short_description') or {}).get('html', ''))
            item['description'] = desc_text or short_text
            item['short_description'] = short_text
        records.extend(items)
    return ScrapeResult(records, incomplete)


# Himadri category pages to crawl. Parent pages already list their sub-category
# products, so "plants/" alone covers every plant type; the rest are the non-plant
# and cross-listed categories. Fish, other livestock and live food are not listed —
# and are also filtered per product below, because they turn up inside "combo" and
# "uncategorized" too.
_HIMADRI_CATEGORY_URLS = [
    'https://himadriaquatics.com/product-category/plants/',
    'https://himadriaquatics.com/product-category/landscape/',
    'https://himadriaquatics.com/product-category/combo/',
    'https://himadriaquatics.com/product-category/alocasia/',
    'https://himadriaquatics.com/product-category/pond-plants/',
    'https://himadriaquatics.com/product-category/aquarium-accessories/',
    'https://himadriaquatics.com/product-category/accessories/',
    'https://himadriaquatics.com/product-category/decor/',
    'https://himadriaquatics.com/product-category/fish-foods/dry-foods/',
    'https://himadriaquatics.com/product-category/fish-medicines-and-water-conditioners/',
    'https://himadriaquatics.com/product-category/uncategorized/',
]

# Product pages are fetched in parallel: a serial crawl of ~1,500 pages would not
# finish inside the Celery task time limit. Each worker pauses after a request so
# the site is not hammered.
_HIMADRI_WORKERS = 4
_HIMADRI_PAGE_DELAY_SECONDS = 0.3
_HIMADRI_PROGRESS_EVERY = 50


def _norm_category(name: str) -> str:
    return re.sub(r'\s+', ' ', unescape(name or '')).strip().lower()


# Himadri categories Junglyst does not list: fish (and every fish sub-category),
# snails / shrimps / crayfish, and live food. A product tagged with any of these is
# dropped, wherever the crawl found it.
HIMADRI_EXCLUDED_CATEGORIES = frozenset(_norm_category(name) for name in [
    'Fishes', 'Algae Eater', 'Angels', 'Cray fish', 'fishes in kerala', 'Gaurami', 'Guppies',
    'oscars', 'other aquarium fishes', 'platy', 'Siamese fighting fish (Betta splendens)',
    'sword tail', 'Tetras', 'planted tank fishes', 'Snails , shrimps and crayfish', 'live foods',
])


def is_excluded_himadri_product(record: dict) -> bool:
    return any(_norm_category(name) in HIMADRI_EXCLUDED_CATEGORIES for name in record.get('categories') or [])


def scrape_live_himadri(progress_cb=None) -> ScrapeResult:
    """Himadri's catalogue minus fish and live food.

    Read from the public Store API (about 18 requests). If that cannot deliver the whole
    catalogue, fall back to loading the category and product pages."""
    from analytics import himadri_api

    try:
        products = himadri_api.fetch_all_products(progress_cb=progress_cb)
        records = [himadri_api.to_record(product) for product in products]
    except himadri_api.HimadriApiError as exc:
        if progress_cb:
            progress_cb(f'Himadri API unavailable ({exc}); reading the site pages instead…')
        return _scrape_himadri_pages(progress_cb)
    kept = [record for record in records if not is_excluded_himadri_product(record)]
    if progress_cb:
        progress_cb(f'Himadri: {len(kept)} products kept, {len(records) - len(kept)} fish/live-food skipped.')
    return ScrapeResult(kept, [])


def _scrape_himadri_pages(progress_cb=None) -> ScrapeResult:
    """Fallback: crawl the Himadri category pages and every product page."""
    from analytics.himadri_scraper import get_all_product_urls, scrape_product

    urls: list[str] = []
    seen: set[str] = set()
    incomplete: list[str] = []
    total = len(_HIMADRI_CATEGORY_URLS)
    for i, cat_url in enumerate(_HIMADRI_CATEGORY_URLS):
        category = cat_url.split('/product-category/')[-1].strip('/')
        if progress_cb:
            progress_cb(f'Listing Himadri: {category} ({i + 1}/{total})…')
        try:
            found = get_all_product_urls(cat_url)
        except Exception:
            incomplete.append(category)
            continue
        for url in found:
            if url not in seen:
                seen.add(url)
                urls.append(url)

    def fetch(url: str):
        try:
            product = scrape_product(url)
        except Exception:
            return None, True
        time.sleep(_HIMADRI_PAGE_DELAY_SECONDS)
        return product, False

    records: list[dict] = []
    failed_pages = excluded = 0
    with ThreadPoolExecutor(max_workers=_HIMADRI_WORKERS) as pool:
        for done, (product, failed) in enumerate(pool.map(fetch, urls), start=1):
            if progress_cb and done % _HIMADRI_PROGRESS_EVERY == 0:
                progress_cb(f'Scraping Himadri products ({done}/{len(urls)})…')
            if failed:
                failed_pages += 1
            elif product and is_excluded_himadri_product(product):
                excluded += 1
            elif product:
                records.append(product)
    if progress_cb:
        progress_cb(f'Himadri: {len(records)} products kept, {excluded} fish/live-food skipped.')
    if failed_pages:
        incomplete.append(f'{failed_pages} product page(s)')
    return ScrapeResult(records, incomplete)


# Himadri lists most products without a SKU. Until a product has been imported
# under its generated HIM-<slug> key, its existing record is found by product name.
_NAME_FALLBACK_SOURCES = {'himadri'}


def _name_key(name: str | None) -> str:
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _skuless_variants_by_name(seller_email: str) -> dict:
    """The seller's SKU-less variants keyed by product name; names shared by two products are left out."""
    from django.db.models import Q
    from core.models import ProductVariant

    by_name: dict = {}
    ambiguous: set = set()
    for variant in (ProductVariant.objects
                    .filter(product__seller__email__iexact=seller_email)
                    .filter(Q(sku__isnull=True) | Q(sku=''))
                    .select_related('product')):
        key = _name_key(variant.product.name)
        if key in by_name:
            ambiguous.add(key)
        by_name[key] = variant
    for key in ambiguous:
        del by_name[key]
    return by_name


# ── Core diff + stock-apply logic ─────────────────────────────────────────────

def compute_diff(
    records: list[dict],
    source: str,
    seller_email: str,
    source_markup: Decimal | None,
    seller_commission: Decimal | None,
    buyer_commission: Decimal | None,
    progress_cb=None,
    scrape_incomplete: list[str] | None = None,
) -> dict:
    """
    Compare scraped records against the live DB.
    - Applies stock changes immediately (safe, no approval needed).
    - Returns price_changes list for admin approval.
    - Returns a session_key stored in cache for the apply step.
    """
    from core.models import ProductVariant
    from core.feed import invalidate_feed_cache
    from analytics.models import StockSyncSession

    markup = (1 + (source_markup or Decimal('0')) / Decimal('100'))
    extractor = _extract_petkadai if source == 'petkadai' else lambda rec: _extract_himadri(rec, markup)

    stock_updated = stock_unchanged = not_found = delisted = 0
    skipped_out_of_stock = matched_by_name = 0
    price_changes = []
    new_products = []  # scraped products not found in DB — candidates for import

    if progress_cb:
        progress_cb(f'Comparing {len(records)} scraped products against DB…')

    parsed_records = [(rec, parsed) for rec in records if (parsed := extractor(rec))]
    # Every SKU seen in this scrape, so delisted items can be zeroed afterwards
    scraped_skus: set[str] = {parsed['sku'] for _, parsed in parsed_records}

    # One query for all SKUs in the scrape (not one per record); the oldest wins a clash.
    variants_by_sku: dict[str, ProductVariant] = {}
    for existing in (ProductVariant.objects.filter(sku__in=scraped_skus)
                     .select_related('product').order_by('created_at')):
        variants_by_sku.setdefault(existing.sku, existing)
    skuless_by_name = (_skuless_variants_by_name(seller_email) if source in _NAME_FALLBACK_SOURCES else {})

    for rec, parsed in parsed_records:
        variant = variants_by_sku.get(parsed['sku'])
        if variant is None:
            variant = skuless_by_name.get(_name_key(parsed.get('full_name')))
            if variant is not None:
                matched_by_name += 1

        if variant is None:
            new_stock = parsed['new_stock']
            if new_stock == 0:
                skipped_out_of_stock += 1  # not in the DB and out of stock — not worth importing
                continue
            not_found += 1
            new_products.append({
                'sku':       parsed['sku'],
                'name':      parsed['name'],
                'new_base':  float(parsed['new_base']),
                'new_stock': new_stock if new_stock is not None else _UNKNOWN_COUNT_IMPORT_STOCK,
                'stock_known': new_stock is not None,
                'source':    source,
                '_raw': {
                    'description':       rec.get('description', ''),
                    'short_description': rec.get('short_description', ''),
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
    # Only for sources where SKU stability is trusted (see _DELIST_ENABLED), and
    # only when the scrape can be trusted to be complete and to belong to this seller.
    delist_skipped = None
    if seller_email and scraped_skus and _DELIST_ENABLED.get(source, False):
        in_stock_skus = set(
            ProductVariant.objects
            .filter(product__seller__email__iexact=seller_email, stock__gt=0)
            .exclude(sku__isnull=True)   # can't delist what we can't identify
            .values_list('sku', flat=True)
        )
        delist_skipped = _delist_block_reason(scrape_incomplete, in_stock_skus, scraped_skus)
        if delist_skipped is None:
            delisted_qs = (
                ProductVariant.objects
                .filter(product__seller__email__iexact=seller_email, stock__gt=0)
                .exclude(sku__isnull=True)
                .exclude(sku__in=scraped_skus)
            )
            delisted = delisted_qs.count()
            if delisted:
                delisted_qs.update(stock=0)
                stock_updated += delisted

    if stock_updated > 0:
        invalidate_feed_cache()

    # Persisted, not cached: an import saves Products, which invalidates
    # catalogue caches. Keeping the session in the DB is what lets every row the
    # admin can see stay importable for as long as the preview is on screen.
    session = StockSyncSession.objects.create(
        source=source,
        seller_email=seller_email or '',
        price_changes={item['id']: item for item in price_changes},
        new_products={p['sku']: p for p in new_products},
    )
    _purge_stale_sessions()

    return {
        'session_key': str(session.key),
        'source': source,
        'stock_updated': stock_updated,
        'stock_unchanged': stock_unchanged,
        'not_found': not_found,
        'skipped_out_of_stock': skipped_out_of_stock,
        'matched_by_name': matched_by_name,
        'delisted': delisted,
        'delist_skipped': delist_skipped,
        'scrape_incomplete': scrape_incomplete or [],
        'price_changes': sorted(price_changes, key=lambda x: abs(x['change_pct']), reverse=True),
        # The scraped detail (_raw) stays in the saved session for the import step;
        # sending it to the browser too would be megabytes once Himadri lists ~1,000 new products.
        'new_products': [
            {key: value for key, value in product.items() if key != '_raw'}
            for product in sorted(new_products, key=lambda x: x['name'])
        ],
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


# ── Himadri category mapping ──────────────────────────────────────────────────
# Himadri tags a product with many category names and a product can belong to several
# Junglyst categories. The plant keywords are the original Himadri import's rules
# (category-name substring → (category, subcategory)); everything else is new.
_PLANTS = 'Plants'
HIMADRI_PLANT_RULES = [
    ('tissue culture', [(_PLANTS, 'Tissue Culture')]), ('tc cups', [(_PLANTS, 'Tissue Culture')]),
    ('anubias', [(_PLANTS, 'Epiphytes')]), ('bucephalandra', [(_PLANTS, 'Epiphytes')]),
    ('lagenandra', [(_PLANTS, 'Epiphytes')]),
    ('aquatic fern', [(_PLANTS, 'Ferns'), (_PLANTS, 'Aquatic Moss')]),
    ('java fern', [(_PLANTS, 'Ferns'), (_PLANTS, 'Epiphytes')]),
    ('bolbitis', [(_PLANTS, 'Ferns'), (_PLANTS, 'Epiphytes')]),
    ('moss', [(_PLANTS, 'Aquatic Moss'), (_PLANTS, 'Mosses')]),
    ('cryptocoryn', [(_PLANTS, 'Aquatic Plants')]), ('echinodor', [(_PLANTS, 'Aquatic Plants')]),
    ('sword', [(_PLANTS, 'Aquatic Plants')]),
    ('aquatic rhizome', [(_PLANTS, 'Aquatic Plants'), (_PLANTS, 'Epiphytes')]),
    ('stem plant', [(_PLANTS, 'Aquatic Plants')]),
    ('carpet', [(_PLANTS, 'Carpet Plants')]), ('foreground', [(_PLANTS, 'Carpet Plants')]),
    ('lilli', [(_PLANTS, 'Lily')]), ('lotus', [(_PLANTS, 'Lily')]), ('waterlil', [(_PLANTS, 'Lily')]),
    ('floating', [(_PLANTS, 'Floating Plants')]), ('pond plant', [(_PLANTS, 'Floating Plants')]),
    ('low tech', [(_PLANTS, 'Beginner Friendly'), (_PLANTS, 'Aquatic Plants')]),
    ('aquatic plant', [(_PLANTS, 'Aquatic Plants')]),
]
# A category name containing one of these marks an aquatic / floating / pond plant.
HIMADRI_AQUATIC_MARKERS = (
    'aquatic', 'aquarium plants', 'tissue culture', 'tc cups', 'floating', 'lillies',
    'lotus', 'waterlilies', 'carpet', 'low tech', 'pond plants',
)
_PLANT_SPECIFIC_MARKERS = (
    'anubias', 'cryptocoryn', 'echinodor', 'stem plant', 'carpet', 'tissue culture', 'tc cups',
    'floating', 'lillies', 'lotus', 'waterlil', 'low tech', 'aquatic plants', 'pond plants',
)
_INDOOR_TAGS = frozenset(_norm_category(name) for name in [
    'Indoor Plants/ Air Purifying Plants', 'Foliage plants', 'Aglaonema', 'Air Plants',
    'Anthurium and Orchids', 'Caladiums', 'Calathea', 'Croton', 'Flowering Plants', 'Helicornias',
    'Hoya', 'Lucky bamboos (Dracenas) and Massangeanas', 'Money plants (Pothos)', 'Monstera varieties',
    'Philodendrons', 'Sansevieria', 'Syngoniums', 'Alocasia', 'Cactus and Succulents',
    'Hangable pot plant varieties', 'Herbs, Spices And Medicinal Plant', 'Fruit Plants', 'Garden Ferns',
    'Garden Plants (outdoor plant varieties)', 'Climbers & Creepers', 'landscape',
])
_SUPPLY_TAGS = frozenset({
    'aquarium accessories & rocks', 'accessories', 'decor', 'aquascaping rocks', 'aquascaping soils',
})


def _words(*words: str):
    return re.compile(r'\b(' + '|'.join(words) + ')', re.IGNORECASE)


_FERTILISER = _words('fertliser', 'fertilizer', 'root tab', 'plant magic', 'nutrient')
_CONDITIONER = _words('conditioner', 'chlor', 'benebacter', 'microlife', 'bacter', 'indicator', 'test')
_FILTER = _words('filter')
_SOIL = _words('soil', 'sand', 'substrate')
_ROCK = _words('rock', 'stone', 'lava', 'seiryu', 'gravel', 'chips', 'pebble')
_WOOD = _words('driftwood', 'drift wood', 'wood', 'cholla')
_TERRARIUM_GEAR = _words('terraplanter', 'wabi', 'kusa')


def _himadri_supply_pair(name: str) -> tuple[str, str]:
    if _FILTER.search(name):
        return 'Aquarium Filters', 'Hang On Filters' if 'hang' in name.lower() else 'Accessories & Spares'
    if _SOIL.search(name):
        return 'Aquascaping', 'Aqua Soil'
    if _ROCK.search(name):
        return 'Aquascaping', 'Rocks & Stones'
    if _WOOD.search(name):
        return 'Aquascaping', 'Driftwood'
    if _TERRARIUM_GEAR.search(name):
        return 'Terrarium & Paludarium', 'Terrarium Accessories'
    return 'Aquarium Accessories', 'Tank Accessories'


def himadri_category_pairs(categories: list[str], name: str) -> list[tuple[str, str | None]]:
    """(Junglyst category name, subcategory name or None) pairs for a Himadri product.

    The product's own Himadri categories decide; the name only separates what shares a
    category (rocks vs soil vs wood in "accessories", conditioners vs medicines).
    A product tagged only "All Plants" says nothing about its type and falls back to
    Aquatic Plants, which is what almost all of them are."""
    names = {_norm_category(c) for c in categories or []}
    # aquatic: a generic aquatic/floating/pond tag, or any tag the plant rules recognise
    # (a product tagged only "Anubias, Bucephalandras & Lagenandras" is still aquatic)
    aquatic = (any(marker in n for n in names for marker in HIMADRI_AQUATIC_MARKERS)
               or any(keyword in n for n in names for keyword, _ in HIMADRI_PLANT_RULES))
    pairs: list[tuple[str, str | None]] = []

    def add(pair):
        if pair not in pairs:
            pairs.append(pair)

    if aquatic:
        for category in categories:
            for keyword, mapped in HIMADRI_PLANT_RULES:
                if keyword in category.lower():
                    for pair in mapped:
                        add(pair)
        add((_PLANTS, 'Aquatic Plants'))
    is_supply = bool(names & _SUPPLY_TAGS) and 'all plants' not in names \
        and not any(marker in n for n in names for marker in _PLANT_SPECIFIC_MARKERS)
    if is_supply:
        pairs.clear()  # e.g. dried sphagnum moss is a supply, not a plant
        add(_himadri_supply_pair(name))
    if 'terrarium plants' in names:
        add(('Terrarium & Paludarium', None))
    if not aquatic and names & _INDOOR_TAGS:
        add(('Indoor Plants', None))
    if names & {'fish foods', 'dry foods (pellets and flakes)'}:
        add(('Fish & Aquarium Care', 'Fish Food'))
    if 'medicines, water conditioners and test kits' in names:
        if _FERTILISER.search(name):
            add(('Aquascaping', 'Fertilizers'))
        elif _CONDITIONER.search(name):
            add(('Fish & Aquarium Care', 'Water Conditioners'))
        elif 'accessories' in names:
            add(('Aquarium Accessories', 'Tank Accessories'))
        else:
            add(('Fish & Aquarium Care', 'Aquarium Medicines'))
    return pairs or [(_PLANTS, 'Aquatic Plants')]


def resolve_himadri_categories(categories: list[str], name: str) -> list:
    """himadri_category_pairs as (Category, SubCategory | None) objects; unknown names are skipped."""
    from core.models import Category, SubCategory

    pairs = himadri_category_pairs(categories, name)
    category_names = {category for category, _ in pairs}
    sub_names = {sub for _, sub in pairs if sub}
    found_categories = {c.name: c for c in Category.objects.filter(name__in=category_names)}
    found_subs = {
        (s.category.name, s.name): s
        for s in SubCategory.objects.select_related('category').filter(
            category__name__in=category_names, name__in=sub_names)
    }
    return [
        (found_categories[category], found_subs.get((category, sub)) if sub else None)
        for category, sub in pairs if category in found_categories
    ]


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
    from django.db import transaction
    from core.models import Product, ProductVariant, ProductImage, VariantType, Category, SubCategory, Tag
    from core.feed import invalidate_feed_cache
    from analytics.models import StockSyncSession

    session = _load_session(session_key)
    if session is None:
        return {'imported': 0, 'errors': ['Sync session not found. Run preview again.']}
    pending = session.new_products or {}

    seller = find_seller_by_email(seller_email)
    if seller is None:
        return {'imported': 0, 'errors': [f'Seller not found: {seller_email}']}

    # The rate the admin typed only reaches the buyer-facing price through the
    # seller profile — ProductVariant.save() reads seller_commission_rate, not
    # anything passed in here. Set it once up front (same as the price-apply
    # path) so price and compare_at_price can't be computed off two different
    # rates, which is how mismatched pricing used to get written.
    s_rate = seller_commission if seller_commission is not None else Decimal(str(seller.seller_commission_rate))
    if seller_commission is not None and seller.seller_commission_rate != seller_commission:
        seller.seller_commission_rate = seller_commission
        seller.save(update_fields=['seller_commission_rate'])

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

            # One transaction per SKU: without it a failure partway through (bad
            # category, bad image URL, unique-SKU clash) left the Product row
            # behind with no variant, no price and no images — a broken listing
            # the shop would happily render.
            with transaction.atomic():
                scraped_cats = raw.get('categories') or []
                if session.source == 'himadri':
                    placements = resolve_himadri_categories(scraped_cats, item['name'])
                else:
                    category, sub_category = resolve_category(scraped_cats, item['name'])
                    placements = [(category, sub_category)] if category else []
                sub_slug = next((sub.slug for _, sub in placements if sub), None)
                care_level, light_req, growth_rate, co2_req = _CARE_DEFAULTS.get(sub_slug, _DEFAULT_CARE)

                # Never fall back to the product name here — an empty description lets
                # the frontend's own placeholder copy render instead of a fake description
                # that's just the title repeated (see backfill_petkadai_descriptions).
                description = raw.get('description') or raw.get('short_description') or ''
                tagline = (raw.get('short_description') or '')[:499] or None

                product = Product(
                    name=item['name'],
                    description=description,
                    tagline=tagline,
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

                for category, sub_category in placements:
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
