"""
Scraper for aquatic plants from himadriaquatics.com.

The stock & price sync (analytics/sync_utils.py) imports get_all_product_urls and
scrape_product from here. Copied from the standalone scrape_himadri.py so the sync
works in deployments that only contain this repository.

Standalone use still works and writes <slug>.json + <slug>.csv:
  python -m analytics.himadri_scraper --url https://himadriaquatics.com/product-category/aquatic-plants/
  python -m analytics.himadri_scraper --url "url1,url2,url3" --markup 14
  python -m analytics.himadri_scraper --url <url> --limit 10 --output my_plants.json
"""

import argparse
import requests
import json
import csv
import time
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# DEFAULT_CATEGORY_URL = 'https://himadriaquatics.com/product-category/fish-medicines-and-water-conditioners/'
# 'https://himadriaquatics.com/product-category/fish-foods/live-foods/'
# 'https://himadriaquatics.com/product-category/fish-foods/dry-foods/'
# 'https://himadriaquatics.com/product-category/fish-foods/'
# 'https://himadriaquatics.com/product-category/aquarium-accessories/aquascaping-soils/'
# 'https://himadriaquatics.com/product-category/aquarium-accessories/aquascaping-rocks/'
# 'https://himadriaquatics.com/product-category/aquarium-accessories/'
# 'https://himadriaquatics.com/product-category/plants/floating-plants/lillies-and-lotus/'
# 'https://himadriaquatics.com/product-category/plants/floating-plants/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/low-tech-aquarium-plants/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/carpet-foreground-plants/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-stem-plant-varieties/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/aquatic-ferns-and-mosses/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/echinodorous-sword-varieties/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/cryptocorynes/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/anubias-lagenandras/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/'
# 'https://himadriaquatics.com/product-category/plants/aquatic-plants/tissue-culture-cups-tc-cups/'
# "https://himadriaquatics.com/product-category/plants/aquatic-plants/"

DEFAULT_CATEGORY_URL = [
    # Aquatic plants — all sub-categories
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/tissue-culture-cups-tc-cups/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/anubias-lagenandras/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/cryptocorynes/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/echinodorous-sword-varieties/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-rhizome-plants/aquatic-ferns-and-mosses/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/aquatic-stem-plant-varieties/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/carpet-foreground-plants/',
    'https://himadriaquatics.com/product-category/plants/aquatic-plants/low-tech-aquarium-plants/',
    # Floating plants & lilies
    'https://himadriaquatics.com/product-category/plants/floating-plants/',
    'https://himadriaquatics.com/product-category/plants/floating-plants/lillies-and-lotus/',
]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Known scientific names by common name keywords
SCIENTIFIC_NAMES = {
    "anubias nana": "Anubias barteri var. nana",
    "anubias barteri": "Anubias barteri",
    "anubias coffeefolia": "Anubias barteri var. coffeefolia",
    "anubias lanceolata": "Anubias lanceolata",
    "anubias golden": "Anubias barteri var. nana 'Golden'",
    "anubias petite": "Anubias barteri var. nana 'Petite'",
    "anubias congensis": "Anubias congensis",
    "anubias minima": "Anubias minima",
    "anubias hastifolia": "Anubias hastifolia",
    "bucephalandra": "Bucephalandra sp.",
    "lagenandra": "Lagenandra sp.",
    "java fern": "Microsorum pteropus",
    "java fern windelov": "Microsorum pteropus 'Windelov'",
    "java fern narrow": "Microsorum pteropus 'Narrow'",
    "java fern trident": "Microsorum pteropus 'Trident'",
    "java moss": "Vesicularia dubyana",
    "christmas moss": "Vesicularia montagnei",
    "mini christmas moss": "Vesicularia sp. 'Mini Christmas'",
    "weeping moss": "Vesicularia ferriei",
    "flame moss": "Taxiphyllum sp. 'Flame'",
    "taiwan moss": "Taxiphyllum alternans",
    "singapore moss": "Vesicularia dubyana",
    "willow moss": "Fontinalis antipyretica",
    "peacock moss": "Taxiphyllum sp. 'Peacock'",
    "riccia": "Riccia fluitans",
    "hornwort": "Ceratophyllum demersum",
    "ludwigia repens": "Ludwigia repens",
    "ludwigia palustris": "Ludwigia palustris",
    "ludwigia arcuata": "Ludwigia arcuata",
    "ludwigia glandulosa": "Ludwigia glandulosa",
    "ludwigia brevipes": "Ludwigia brevipes",
    "ludwigia ovalis": "Ludwigia ovalis",
    "rotala rotundifolia": "Rotala rotundifolia",
    "rotala indica": "Rotala indica",
    "rotala macrandra": "Rotala macrandra",
    "rotala wallichii": "Rotala wallichii",
    "rotala blood red": "Rotala sp. 'Blood Red'",
    "rotala green": "Rotala rotundifolia 'Green'",
    "bacopa caroliniana": "Bacopa caroliniana",
    "bacopa monnieri": "Bacopa monnieri",
    "bacopa compact": "Bacopa sp. 'Compact'",
    "cryptocoryne wendtii": "Cryptocoryne wendtii",
    "cryptocoryne beckettii": "Cryptocoryne beckettii",
    "cryptocoryne parva": "Cryptocoryne parva",
    "cryptocoryne lutea": "Cryptocoryne lutea",
    "cryptocoryne spiralis": "Cryptocoryne spiralis",
    "cryptocoryne crispatula": "Cryptocoryne crispatula",
    "cryptocoryne usteriana": "Cryptocoryne usteriana",
    "cryptocoryne petchii": "Cryptocoryne petchii",
    "cryptocoryne balansae": "Cryptocoryne balansae",
    "echinodorus amazonicus": "Echinodorus grisebachii",
    "amazon sword": "Echinodorus amazonicus",
    "echinodorus bleheri": "Echinodorus bleheri",
    "echinodorus tenellus": "Echinodorus tenellus",
    "dwarf sagittaria": "Sagittaria subulata",
    "sagittaria": "Sagittaria sp.",
    "vallisneria": "Vallisneria sp.",
    "vallisneria nana": "Vallisneria nana",
    "vallisneria spiralis": "Vallisneria spiralis",
    "vallisneria americana": "Vallisneria americana",
    "hygrophila polysperma": "Hygrophila polysperma",
    "hygrophila difformis": "Hygrophila difformis",
    "hygrophila corymbosa": "Hygrophila corymbosa",
    "hygrophila pinnatifida": "Hygrophila pinnatifida",
    "hygrophila balsamica": "Hygrophila balsamica",
    "limnophila sessiliflora": "Limnophila sessiliflora",
    "limnophila aquatica": "Limnophila aquatica",
    "ambulia": "Limnophila sessiliflora",
    "hemianthus callitrichoides": "Hemianthus callitrichoides",
    "hc cuba": "Hemianthus callitrichoides 'Cuba'",
    "monte carlo": "Micranthemum tweediei",
    "micranthemum monte carlo": "Micranthemum tweediei",
    "hemianthus micranthemoides": "Hemianthus micranthemoides",
    "pearl weed": "Hemianthus micranthemoides",
    "eleocharis": "Eleocharis sp.",
    "eleocharis parvula": "Eleocharis parvula",
    "dwarf hairgrass": "Eleocharis parvula",
    "hair grass": "Eleocharis sp.",
    "marsilea": "Marsilea sp.",
    "marsilea crenata": "Marsilea crenata",
    "marsilea hirsuta": "Marsilea hirsuta",
    "glossostigma elatinoides": "Glossostigma elatinoides",
    "glossostigma": "Glossostigma elatinoides",
    "staurogyne repens": "Staurogyne repens",
    "blyxa japonica": "Blyxa japonica",
    "blyxa aubertii": "Blyxa aubertii",
    "cabomba": "Cabomba sp.",
    "cabomba caroliniana": "Cabomba caroliniana",
    "ceratopteris": "Ceratopteris sp.",
    "water sprite": "Ceratopteris thalictroides",
    "ceratopteris thalictroides": "Ceratopteris thalictroides",
    "bolbitis heudelotii": "Bolbitis heudelotii",
    "african water fern": "Bolbitis heudelotii",
    "thai water fern": "Microsorum sp.",
    "azolla": "Azolla filiculoides",
    "salvinia": "Salvinia sp.",
    "salvinia natans": "Salvinia natans",
    "salvinia minima": "Salvinia minima",
    "duckweed": "Lemna minor",
    "frogbit": "Limnobium laevigatum",
    "water lettuce": "Pistia stratiotes",
    "water hyacinth": "Eichhornia crassipes",
    "eichhornia": "Eichhornia sp.",
    "lotus": "Nelumbo sp.",
    "lotus red": "Nelumbo nucifera 'Red'",
    "lotus white": "Nelumbo nucifera 'White'",
    "lotus blue": "Nymphaea nouchali",
    "water lily": "Nymphaea sp.",
    "nymphaea": "Nymphaea sp.",
    "nymphaea lotus": "Nymphaea lotus",
    "nymphaea rubra": "Nymphaea rubra",
    "nymphaea stellata": "Nymphaea nouchali",
    "tiger lotus": "Nymphaea lotus 'Tiger'",
    "aponogetons": "Aponogeton sp.",
    "aponogeton": "Aponogeton sp.",
    "aponogeton crispus": "Aponogeton crispus",
    "aponogeton ulvaceus": "Aponogeton ulvaceus",
    "alternanthera reineckii": "Alternanthera reineckii",
    "alternanthera": "Alternanthera sp.",
    "myriophyllum": "Myriophyllum sp.",
    "myriophyllum aquaticum": "Myriophyllum aquaticum",
    "myriophyllum tuberculatum": "Myriophyllum tuberculatum",
    "egeria densa": "Egeria densa",
    "anacharis": "Egeria densa",
    "elodea": "Elodea sp.",
    "najas": "Najas sp.",
    "najas guadalupensis": "Najas guadalupensis",
    "potamogeton": "Potamogeton sp.",
    "eriocaulon": "Eriocaulon sp.",
    "utricularia": "Utricularia sp.",
    "utricularia graminifolia": "Utricularia graminifolia",
    "murdannia": "Murdannia sp.",
    "cyperus": "Cyperus sp.",
    "cyperus helferi": "Cyperus helferi",
    "pogostemon": "Pogostemon sp.",
    "pogostemon erectus": "Pogostemon erectus",
    "pogostemon stellatus": "Pogostemon stellatus",
    "pogostemon helferi": "Pogostemon helferi",
    "downoi": "Pogostemon helferi",
    "ottelia": "Ottelia sp.",
    "proserpinaca": "Proserpinaca sp.",
    "ludwigia inclinata": "Ludwigia inclinata",
    "didiplis": "Didiplis diandra",
    "tonina": "Tonina fluviatilis",
    "syngonanthus": "Syngonanthus sp.",
    "hydrotriche hottoniiflora": "Hydrotriche hottoniiflora",
    "rotala sp bonsai": "Rotala sp. 'Bonsai'",
    "rotala nanjenshan": "Rotala sp. 'Nanjenshan'",
    "taiwan": "Rotala sp. 'Taiwan'",
    "limnophila hippuridoides": "Limnophila hippuridoides",
    "fissidens": "Fissidens sp.",
    "fissidens fontanus": "Fissidens fontanus",
    "needle leaf java fern": "Microsorum pteropus 'Narrow Leaf'",
    "mini bolbitis": "Bolbitis sp. 'Mini'",
    "subwassertang": "Lomariopsis lineata",
    "susswassertang": "Lomariopsis lineata",
    "phoenix moss": "Fissidens sp. 'Phoenix'",
    "spiky moss": "Taxiphyllum sp. 'Spiky'",
}


def get_scientific_name(product_name: str) -> str:
    name_lower = product_name.lower()
    # Try longest match first
    sorted_keys = sorted(SCIENTIFIC_NAMES.keys(), key=len, reverse=True)
    for key in sorted_keys:
        if key in name_lower:
            return SCIENTIFIC_NAMES[key]
    return ""


def get_soup(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  [ERROR] {url} -> {e}")
        return None


def parse_price(text: str) -> float | None:
    text = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(text)
    except ValueError:
        return None


def apply_markup(price: float | None, markup_percent: float = 0) -> float | None:
    """Apply a percentage markup to a price."""
    if price is None:
        return None
    return price * (1 + markup_percent / 100)


def get_all_product_urls(category_url: str, limit: int = 0) -> list[str]:
    """
    Crawl all paginated listing pages under category_url and collect every
    product URL found.  Follows WooCommerce-style /page/N/ pagination.

    Parameters
    ----------
    category_url : Full URL of the WooCommerce category page.
    limit        : Stop after collecting this many URLs (0 = no limit).
    """
    urls = []
    page = 1
    while True:
        paged_url = category_url if page == 1 else f"{category_url}page/{page}/"
        print(f"  Scanning page {page}: {paged_url}")
        soup = get_soup(paged_url)
        if soup is None:
            break

        # Try selectors from most-specific to most-generic
        products = soup.select("li.product a.woocommerce-loop-product__link")
        if not products:
            products = soup.select("ul.products li.product h2 a")
        if not products:
            products = soup.select("li.product .product-title a")

        hrefs = []
        for a in products:
            href = a.get("href", "")
            if href and href not in hrefs:
                hrefs.append(href)

        if not hrefs:
            print("    No products found on this page — end of catalogue.")
            break

        urls.extend(hrefs)
        print(f"    Found {len(hrefs)} products on page {page} (running total: {len(urls)})")

        # Honour optional limit
        if limit and len(urls) >= limit:
            urls = urls[:limit]
            print(f"    Limit {limit} reached.")
            break

        # Follow pagination
        next_btn = soup.select_one("a.next.page-numbers")
        if not next_btn:
            print(f"    No next page — all {len(urls)} product URLs collected.")
            break
        page += 1
        time.sleep(0.8)

    return list(dict.fromkeys(urls))  # deduplicate preserving order


def scrape_product(url: str) -> dict:
    soup = get_soup(url)
    if soup is None:
        return {}

    data = {"url": url}

    # Name
    name_el = soup.select_one(".product_title") or soup.select_one("h1")
    data["name"] = name_el.get_text(strip=True) if name_el else ""

    # Scientific name
    data["scientific_name"] = get_scientific_name(data.get("name", ""))

    # SKU — strip leading '#' that WooCommerce sometimes renders in the .sku element
    sku_el = soup.select_one(".sku")
    data["sku"] = (sku_el.get_text(strip=True).lstrip('#') if sku_el else "")

    # Price — scope to <p class="price"> (main product price) to avoid grabbing
    # prices from related-product widgets that use <span class="price"> further down.
    sale_el = soup.select_one("p.price ins .woocommerce-Price-amount")
    regular_el = soup.select_one("p.price del .woocommerce-Price-amount")
    if not sale_el:
        sale_el = soup.select_one("p.price .woocommerce-Price-amount")

    data["sale_price"] = parse_price(sale_el.get_text()) if sale_el else None
    data["regular_price"] = parse_price(regular_el.get_text()) if regular_el else data["sale_price"]
    data["currency"] = "INR"

    # Stock
    stock_el = soup.select_one(".stock")
    stock_text = stock_el.get_text(strip=True) if stock_el else ""
    data["stock_status"] = stock_text
    data["stock"] = stock_text.lower().replace(' in stock', '') if stock_text else 0

    # In stock
    data["in_stock"] = "out" not in stock_text.lower() and stock_text != ""

    # Description - short
    short_desc_el = soup.select_one(".woocommerce-product-details__short-description")
    data["short_description"] = short_desc_el.get_text(" ", strip=True) if short_desc_el else ""

    # Description - full
    full_desc_el = soup.select_one("#tab-description")
    data["description"] = full_desc_el.get_text(" ", strip=True) if full_desc_el else ""

    # Categories
    cat_els = soup.select(".posted_in a")
    data["categories"] = [c.get_text(strip=True) for c in cat_els]

    # Tags
    tag_els = soup.select(".tagged_as a")
    data["tags"] = [t.get_text(strip=True) for t in tag_els]

    # Rating — <strong class="rating">4.50</strong>
    rating_el = soup.select_one("strong.rating")
    if rating_el:
        try:
            data["rating"] = float(rating_el.get_text(strip=True))
        except ValueError:
            data["rating"] = None
    else:
        data["rating"] = None

    # Additional info table (specifications)
    specs = {}
    rows = soup.select(".woocommerce-product-attributes tr")
    for row in rows:
        label_el = row.select_one("th")
        value_el = row.select_one("td")
        if label_el and value_el:
            specs[label_el.get_text(strip=True)] = value_el.get_text(" ", strip=True)
    data["specifications"] = specs

    # Image URLs
    img_els = soup.select(".woocommerce-product-gallery__image img")
    data["images"] = [img.get("src", "") for img in img_els if img.get("src")]

    return data


def _slug_from_url(url: str) -> str:
    """Derive a filesystem-safe slug from a category URL.

    https://himadriaquatics.com/product-category/aquatic-plants/  ->  aquatic-plants
    """
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or "products"


def main():
    parser = argparse.ArgumentParser(
        description="Scrape all products from himadriaquatics.com category pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_CATEGORY_URL,
        metavar="CATEGORY_URL",
        help=(
            "Full URL(s) of the category/categories to scrape. "
            "Can be a single URL or comma-separated URLs. "
            f"Default: {DEFAULT_CATEGORY_URL}"
        ),
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        metavar="FILE",
        help=(
            "Output JSON path. "
            "Default: himadri_<category-slug>.json (when single URL) or himadri_all_products.json (when multiple URLs)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after this many products per category (0 = fetch all, default).",
    )
    parser.add_argument(
        "--markup",
        type=float,
        default=0,
        help="Apply a percentage markup to prices (e.g., 14 for 14%%, default: 0)",
    )
    args = parser.parse_args()

    # Parse URLs (handle comma-separated or single URL)
    urls = [url.strip().rstrip("/") + "/" for url in args.url.split(",")]
    # urls = [DEFAULT_CATEGORY_URL]
    markup_percent = args.markup
    
    # Determine output path
    if args.output:
        json_path = args.output
    elif len(urls) == 1:
        slug = _slug_from_url(urls[0])
        json_path = f"himadri/himadri_{slug}.json"
    else:
        json_path = "himadri/himadri_plants.json"
    
    csv_path = json_path.replace(".json", ".csv")

    print(f"=== Himadri Aquatics Scraper ===")
    print(f"  Categories  : {len(urls)} URL(s)")
    print(f"  Limit       : {'none (all products)' if not args.limit else args.limit} per category")
    print(f"  Markup      : {markup_percent}%")
    print(f"  Output JSON : {json_path}")
    print(f"  Output CSV  : {csv_path}\n")

    all_products = []

    # Process each URL
    for url_idx, category_url in enumerate(urls, 1):
        print(f"\n{'='*60}")
        print(f"Category {url_idx}/{len(urls)}: {category_url}")
        print(f"{'='*60}\n")

        # ── Step 1: Collect all product URLs from listing pages ──────────────────
        print("[1] Collecting product URLs ...")
        product_urls = get_all_product_urls(category_url, limit=args.limit)
        print(f"\n  Found {len(product_urls)} product URLs to scrape\n")

        if not product_urls:
            print("  No products found for this URL — skipping.\n")
            continue

        # ── Step 2: Scrape each product page ─────────────────────────────────────
        print("[2] Scraping individual product pages ...")
        for i, url in enumerate(product_urls, 1):
            print(f"  [{i:>3}/{len(product_urls)}] {url}")
            product = scrape_product(url)
            if product:
                # Apply markup to prices
                if markup_percent > 0:
                    product["sale_price"] = apply_markup(product.get("sale_price"), markup_percent)
                    product["regular_price"] = apply_markup(product.get("regular_price"), markup_percent)
                all_products.append(product)
            time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"[3] Saving {len(all_products)} total products ...\n")

    # ── Step 3: Write JSON ────────────────────────────────────────────────────
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_products, f, ensure_ascii=False, indent=2)
    print(f"  Saved JSON : {json_path}")

    # ── Step 4: Write CSV ─────────────────────────────────────────────────────
    flat_fields = [
        "name", "scientific_name", "sku", "sale_price", "regular_price", "currency",
        "stock_status", "stock", "in_stock", "short_description", "description",
        "categories", "tags", "rating", "url", "images",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=flat_fields)
        writer.writeheader()
        for p in all_products:
            row = {k: p.get(k, "") for k in flat_fields}
            row["categories"] = " | ".join(p.get("categories", []))
            row["tags"]       = " | ".join(p.get("tags", []))
            row["images"]     = " | ".join(p.get("images", []))
            writer.writerow(row)
    print(f"  Saved CSV  : {csv_path}")

    print(f"\nDone — {len(all_products)} products scraped from {len(urls)} category/categories")
    if markup_percent > 0:
        print(f"Price markup applied: {markup_percent}%")
    print(f"Next step : python transform_himadri.py --input {json_path}")


if __name__ == "__main__":
    main()
