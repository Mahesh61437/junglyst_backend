"""
Himadri's public WooCommerce Store API (https://himadriaquatics.com/wp-json/wc/store/v1/products).

The stock & price sync reads the whole catalogue from here — about 18 requests of
100 products — instead of loading every product page (~1,500 requests). Records are
converted to the same shape the page scraper (analytics/himadri_scraper.py) produces,
so the rest of the sync does not care which source filled them.
"""
from __future__ import annotations

import re
import time
from html import unescape
from typing import Callable, Optional

import requests

STORE_API_URL = 'https://himadriaquatics.com/wp-json/wc/store/v1/products'
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; JunglystSync/1.0)'}

# WooCommerce reports a placeholder "maximum" (9999 or a countdown from 9,999,999)
# for products whose stock is not tracked. No real plant listing holds this many.
UNMANAGED_STOCK_THRESHOLD = 5000

_TAGS = re.compile(r'<[^>]+>')
_WHITESPACE = re.compile(r'\s+')


class HimadriApiError(Exception):
    """The catalogue could not be read completely; callers fall back to the page crawl."""


def _plain_text(html: str) -> str:
    return _WHITESPACE.sub(' ', unescape(_TAGS.sub(' ', html or ''))).strip()


def _get_page(page: int, session: requests.Session):
    last_error: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                STORE_API_URL, params={'per_page': PAGE_SIZE, 'page': page},
                headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json(), response.headers
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_SECONDS)
    raise HimadriApiError(f'page {page}: {type(last_error).__name__}')


def fetch_all_products(progress_cb: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Every product in the store. Raises HimadriApiError unless the full catalogue arrived."""
    products: list[dict] = []
    session = requests.Session()
    page, total_pages, expected_total = 1, 1, None
    while page <= total_pages:
        if progress_cb:
            progress_cb(f'Reading Himadri catalogue (page {page}/{total_pages})…')
        data, headers = _get_page(page, session)
        if not isinstance(data, list):
            raise HimadriApiError(f'page {page}: unexpected response')
        products.extend(data)
        try:
            total_pages = int(headers.get('X-WP-TotalPages', total_pages))
            expected_total = int(headers.get('X-WP-Total', len(products)))
        except ValueError as exc:
            raise HimadriApiError('unreadable pagination headers') from exc
        page += 1
    if expected_total is not None and len(products) != expected_total:
        raise HimadriApiError(f'received {len(products)} of {expected_total} products')
    if not products:
        raise HimadriApiError('catalogue is empty')
    return products


def _price(prices: dict, field: str) -> Optional[float]:
    raw = (prices or {}).get(field)
    if raw in (None, ''):
        return None
    try:
        return int(raw) / (10 ** int((prices or {}).get('currency_minor_unit', 2)))
    except (ValueError, TypeError):
        return None


def _stock_status(product: dict) -> tuple[Optional[int], str]:
    """(count or None when untracked, text in the page scraper's wording)."""
    if not product.get('is_in_stock'):
        return 0, 'Out of stock'
    maximum = (product.get('add_to_cart') or {}).get('maximum')
    if isinstance(maximum, int) and 0 < maximum < UNMANAGED_STOCK_THRESHOLD:
        return maximum, f'{maximum} in stock'
    return None, ''


def to_record(product: dict) -> dict:
    """A Store API product in the shape analytics.himadri_scraper.scrape_product returns."""
    try:
        from analytics.himadri_scraper import get_scientific_name
    except ImportError:  # bs4 missing: the scientific name is a nicety, not required
        def get_scientific_name(_name: str) -> str:
            return ''

    name = unescape(product.get('name') or '').strip()
    prices = product.get('prices') or {}
    regular, sale = _price(prices, 'regular_price'), _price(prices, 'sale_price')
    stock, stock_status = _stock_status(product)
    try:
        rating = float(product.get('average_rating')) or None
    except (TypeError, ValueError):
        rating = None
    return {
        'url': product.get('permalink') or '',
        'slug': product.get('slug') or '',
        'name': name,
        'scientific_name': get_scientific_name(name),
        'sku': (product.get('sku') or '').strip().lstrip('#'),
        'sale_price': sale if sale is not None and sale != regular else None,
        'regular_price': regular if regular is not None else sale,
        'currency': 'INR',
        'stock': stock,
        'stock_status': stock_status,
        'in_stock': bool(product.get('is_in_stock')),
        'short_description': _plain_text(product.get('short_description')),
        'description': _plain_text(product.get('description')),
        'categories': [unescape(c.get('name', '')).strip() for c in product.get('categories') or []],
        'tags': [unescape(t.get('name', '')).strip() for t in product.get('tags') or []],
        'rating': rating,
        'images': [i['src'] for i in product.get('images') or [] if i.get('src')],
    }
