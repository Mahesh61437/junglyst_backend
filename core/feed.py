"""
Shop product feed — seller-fair round-robin, cached in Redis.

Cache layers
------------
Layer 1 — master feed   shop:feed:ids:v1
    All active product IDs in seller-fair round-robin order.
    Rebuilt once per day (TTL until midnight) or after product/stock changes.

Layer 2 — filtered feed   shop:feed:filtered:{version}:{hash}
    Master list narrowed to a specific filter combination (category, care level,
    price range, search, etc.).  Keyed by a hash of the active filter params AND
    the current feed version, so it auto-expires whenever the master feed changes
    — no explicit per-filter invalidation needed.

Per-request cost
----------------
Browse (no filters, warm cache):
    O(1)  Redis GET master list
    O(1)  Python page slice
    O(p)  DB PK fetch for page_size p  ← only DB work

Filtered (warm cache, filter seen before):
    O(1)  Redis GET filtered list      ← no DB at all
    O(1)  Python page slice
    O(p)  DB PK fetch

Filtered (warm cache, filter seen for first time today):
    O(1)  Redis GET master list
    O(k)  DB  SELECT id WHERE filters  ← lightweight, no sort, no JOINs
    O(n)  Python set intersection      ← microseconds
    O(1)  Redis SET filtered list
    O(p)  DB PK fetch

Cache miss (first request of the day or after invalidation):
    O(n)  single DB query (id, seller_id, has_stock only)
    O(n)  Python shuffle
    →  stored in Redis until midnight

Order rules
-----------
1. In-stock products always before out-of-stock.
2. Within each block, sellers are round-robin interleaved — no single seller
   dominates any page regardless of catalogue size.
3. Seller order and per-seller product order use a daily seed — identical all
   day (pagination is stable), reshuffles at midnight.
"""

import datetime
import hashlib
import json
import random
import time
from collections import defaultdict

from django.core.cache import cache

FEED_CACHE_KEY    = 'shop:feed:ids:v1'
FEED_VERSION_KEY  = 'shop:feed:version'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ttl_until_midnight() -> int:
    """Seconds from now until 00:05 tomorrow."""
    now = datetime.datetime.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    return max(int((tomorrow - now).total_seconds()), 60)


def _feed_version() -> int:
    """
    Monotonic version stamp for the feed.
    Bumped by invalidate_feed_cache() so all filter caches become stale
    automatically — no need to track or delete individual filter keys.
    Uses milliseconds so rapid invalidations still produce distinct versions.
    """
    v = cache.get(FEED_VERSION_KEY)
    if v is None:
        v = int(time.time() * 1000)
        cache.set(FEED_VERSION_KEY, v, _ttl_until_midnight())
    return v


def _filtered_cache_key(filter_params: dict) -> str:
    """Stable Redis key for a filter-param dict, scoped to the current feed version."""
    canonical = json.dumps(sorted(filter_params.items()), sort_keys=True)
    h = hashlib.md5(canonical.encode()).hexdigest()[:10]
    return f'shop:feed:filtered:{_feed_version()}:{h}'


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_ordered_ids() -> list:
    """
    Single DB query → pure Python shuffle → ordered list of all active product IDs.
    """
    from django.db.models import Exists, OuterRef
    from .models import Product, ProductVariant

    daily_seed = int(datetime.date.today().strftime('%Y%m%d'))

    rows = list(
        Product.objects
        .filter(is_active=True, is_draft=False)
        .annotate(has_stock=Exists(
            ProductVariant.objects.filter(product=OuterRef('pk'), stock__gt=0)
        ))
        .values('id', 'seller_id', 'has_stock')
    )

    seller_in: dict  = defaultdict(list)
    seller_out: dict = defaultdict(list)
    for row in rows:
        (seller_in if row['has_stock'] else seller_out)[row['seller_id']].append(row['id'])

    all_sellers = sorted(set(list(seller_in.keys()) + list(seller_out.keys())))

    for sid in all_sellers:
        seller_seed = daily_seed ^ (hash(str(sid)) & 0x7FFF_FFFF)
        rng = random.Random(seller_seed)
        rng.shuffle(seller_in[sid])
        rng.shuffle(seller_out[sid])

    seller_order = all_sellers[:]
    random.Random(daily_seed).shuffle(seller_order)

    result: list = []

    if seller_in:
        max_rounds = max((len(seller_in[s]) for s in seller_order if seller_in[s]), default=0)
        for i in range(max_rounds):
            for sid in seller_order:
                if i < len(seller_in[sid]):
                    result.append(seller_in[sid][i])

    if seller_out:
        max_rounds = max((len(seller_out[s]) for s in seller_order if seller_out[s]), default=0)
        for i in range(max_rounds):
            for sid in seller_order:
                if i < len(seller_out[sid]):
                    result.append(seller_out[sid][i])

    return result


# ---------------------------------------------------------------------------
# Public cache interface
# ---------------------------------------------------------------------------

def get_ordered_product_ids() -> list:
    """Return the master feed, computing it if the cache is cold."""
    ids = cache.get(FEED_CACHE_KEY)
    if ids is None:
        ids = compute_ordered_ids()
        cache.set(FEED_CACHE_KEY, ids, _ttl_until_midnight())
    return ids


def get_filtered_ordered_ids(filter_params: dict, qs) -> list:
    """
    Return the master feed narrowed to products matching the active filters.

    filter_params  — dict of active filter query params (used as cache key)
    qs             — the already-filtered Django queryset (used on cache miss
                     to cheaply fetch matching IDs via SELECT id)

    On cache hit:  pure Redis, zero DB queries.
    On cache miss: one lightweight  SELECT id  query, then cached for today.
    """
    cache_key = _filtered_cache_key(filter_params)
    ids = cache.get(cache_key)
    if ids is None:
        master   = get_ordered_product_ids()
        valid    = frozenset(qs.values_list('id', flat=True))   # SELECT id only
        ids      = [i for i in master if i in valid]
        cache.set(cache_key, ids, _ttl_until_midnight())
    return ids


def prewarm_filter_caches(master_ids: list) -> dict:
    """
    Pre-populate the filter cache for every category and sub_category so that
    the first user to browse any filtered page gets a cache hit, not a DB query.

    Covers all four filter paths used by the frontend:
      ?category=Aquatic+Plants     → {'category': 'Aquatic Plants'}
      ?categories=5                → {'categories': '5'}
      ?sub_categories=3            → {'sub_categories': '3'}
      ?sub_category_id=3           → {'sub_category_id': '3'}

    Two extra DB queries (SELECT id + category/subcategory FK only) then pure
    Python set intersections — fast even at 10k+ SKUs.
    """
    from .models import Product

    ttl        = _ttl_until_midnight()
    master_set = frozenset(master_ids)
    summary    = {'categories': 0, 'subcategories': 0}

    # ── Category mappings ────────────────────────────────────────────────────
    # One query returns (product_id, category_id, category_name) for every
    # product↔category relationship.
    cat_rows = (
        Product.objects
        .filter(is_active=True, is_draft=False, id__in=master_set)
        .values('id', 'categories__id', 'categories__name')
    )

    by_cat_name: dict = defaultdict(set)   # name  → {product_ids}
    by_cat_id:   dict = defaultdict(set)   # str(id) → {product_ids}

    for row in cat_rows:
        if row['categories__id']:
            by_cat_name[row['categories__name']].add(row['id'])
            by_cat_id[str(row['categories__id'])].add(row['id'])

    for name, valid in by_cat_name.items():
        filtered = [i for i in master_ids if i in valid]
        cache.set(_filtered_cache_key({'category': name}), filtered, ttl)
        summary['categories'] += 1

    for cid, valid in by_cat_id.items():
        filtered = [i for i in master_ids if i in valid]
        cache.set(_filtered_cache_key({'categories': cid}), filtered, ttl)

    # ── Sub-category mappings ─────────────────────────────────────────────────
    subcat_rows = (
        Product.objects
        .filter(is_active=True, is_draft=False, id__in=master_set)
        .values('id', 'sub_categories__id')
    )

    by_subcat_id: dict = defaultdict(set)   # str(id) → {product_ids}

    for row in subcat_rows:
        if row['sub_categories__id']:
            by_subcat_id[str(row['sub_categories__id'])].add(row['id'])

    for scid, valid in by_subcat_id.items():
        filtered = [i for i in master_ids if i in valid]
        # DjangoFilterBackend param
        cache.set(_filtered_cache_key({'sub_categories': scid}),   filtered, ttl)
        # Custom param (same IDs, same result — two keys, one list)
        cache.set(_filtered_cache_key({'sub_category_id': scid}),  filtered, ttl)
        summary['subcategories'] += 1

    return summary


def invalidate_feed_cache() -> None:
    """
    Bust the master feed and implicitly expire all filter caches by
    bumping the version stamp they're keyed on.
    """
    cache.delete(FEED_CACHE_KEY)
    cache.delete(FEED_VERSION_KEY)          # delete first so _feed_version() recomputes fresh
    cache.set(FEED_VERSION_KEY, int(time.time() * 1000), _ttl_until_midnight())
