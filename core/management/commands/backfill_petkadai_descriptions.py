"""
Management command: backfill_petkadai_descriptions
====================================================
Fixes the fallout of the `import_petkadai` / dashboard-sync bug where products
with no scraped description got `description = name` instead of real content
(e.g. a product literally described as "Ammonia Remover 500g").

Two source modes:
  --json-dir (default)  Match against local petkadai JSON snapshots in
                         pet_kadai/*.json by slug/name. Fast, no network,
                         but only as fresh as the last CLI scrape.
  --live                 Re-scrape petkadai.com's GraphQL API right now (same
                         code path as the Super Admin "Sync Pet Kadai" button)
                         and match by SKU via ProductVariant. Slower (hits
                         every category live) but far more complete — petkadai
                         stores the real product copy in `short_description`
                         and leaves `description` empty for every product, so
                         this pulls the same "Product Details" text shown on
                         the live page.

For every Product whose description is junk (description == name), this
looks for a real (non-junk) description in the chosen source and, if found,
stages an update. Products with nothing found anywhere are left untouched
and listed separately.

This is READ-ONLY by default. Nothing is written unless --apply is passed.

Usage
-----
  python manage.py backfill_petkadai_descriptions                # dry-run, local snapshots
  python manage.py backfill_petkadai_descriptions --apply         # write the fixes
  python manage.py backfill_petkadai_descriptions --live --apply  # re-scrape live + write
  python manage.py backfill_petkadai_descriptions --json-dir /path/to/pet_kadai
  python manage.py backfill_petkadai_descriptions --limit 20
  python manage.py backfill_petkadai_descriptions --still-missing-out missing.txt
"""
from __future__ import annotations

import json
import os
from glob import glob

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

DEFAULT_JSON_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),   # .../core/management/commands/
        "..", "..", "..",            # -> junglyst_backend/
        "..",                        # -> E:\JungLyst
        "pet_kadai",
    )
)


def _is_junk(description: str | None, name: str | None) -> bool:
    d = (description or "").strip()
    n = (name or "").strip()
    return bool(d) and d == n


def _load_snapshot_map(json_dir: str) -> dict[str, dict]:
    """
    Scan every *.json file in json_dir and build slug -> {description, tagline, name}
    for records that have a real (non-junk) description. First match wins.
    """
    by_slug: dict[str, dict] = {}
    by_name: dict[str, dict] = {}

    for path in sorted(glob(os.path.join(json_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        records = data if isinstance(data, list) else data.get("products", [])
        for rec in records:
            product = rec.get("product", rec) if isinstance(rec, dict) else None
            if not isinstance(product, dict):
                continue

            name  = (product.get("name") or "").strip()
            desc  = (product.get("description") or "").strip()
            slug  = (product.get("slug") or slugify(name)).strip()

            if not name or not desc or _is_junk(desc, name):
                continue  # no usable description in this snapshot either

            entry = {
                "description": product.get("description"),
                "tagline":     product.get("tagline"),
                "name":        name,
            }
            by_slug.setdefault(slug, entry)
            by_name.setdefault(name.lower(), entry)

    return {"by_slug": by_slug, "by_name": by_name}


def _load_live_sku_map(stdout) -> dict[str, dict]:
    """
    Re-scrape petkadai.com live (all categories) via the same GraphQL path the
    Super Admin dashboard sync uses, and build sku -> {description, tagline, name}
    for every item with real (non-empty) description content.
    """
    from analytics.sync_utils import scrape_live_petkadai

    def _progress(msg: str) -> None:
        stdout.write(f"  {msg}")

    records = scrape_live_petkadai(progress_cb=_progress)

    by_sku: dict[str, dict] = {}
    for item in records:
        sku = item.get("sku") or ""
        name = (item.get("name") or "").strip()
        desc = (item.get("description") or "").strip()
        short = (item.get("short_description") or "").strip()
        if not sku or not desc:
            continue
        by_sku[sku] = {"description": desc, "tagline": short, "name": name}

    return by_sku


class Command(BaseCommand):
    help = (
        "Backfill real descriptions for petkadai-imported products whose "
        "description was junk-filled with the product name. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json-dir",
            default=DEFAULT_JSON_DIR,
            help=f"Directory of petkadai *.json snapshots to search. Default: {DEFAULT_JSON_DIR}",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the fixes. Without this flag, only a report is printed.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after examining this many junk products (0 = all).",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            help="Re-scrape petkadai.com live instead of reading local JSON "
                 "snapshots, and match by SKU. Slower, far more complete.",
        )
        parser.add_argument(
            "--description-only",
            action="store_true",
            help="Only write the description field. Do not touch tagline "
                 "or any other field, even when tagline is currently blank.",
        )
        parser.add_argument(
            "--still-missing-out",
            default=None,
            help="Optional file path to write the list of slugs that couldn't be "
                 "fixed (need a fresh scrape / no longer on petkadai).",
        )

    def handle(self, *args, **options):
        from core.models import Product, ProductVariant

        json_dir          : str = options["json_dir"]
        apply_changes      : bool = options["apply"]
        limit               : int = options["limit"]
        live                : bool = options["live"]
        description_only    : bool = options["description_only"]
        still_missing_out : str | None = options["still_missing_out"]

        self.stdout.write(
            self.style.WARNING("DRY RUN — no changes will be written.\n")
            if not apply_changes
            else self.style.WARNING("APPLY MODE — matched products WILL be updated.\n")
        )

        junk_products = [
            p for p in Product.objects.all()
            if _is_junk(p.description, p.name)
        ]
        if limit:
            junk_products = junk_products[:limit]

        self.stdout.write(f"Found {len(junk_products)} products with junk descriptions.\n")

        fixable: list[tuple] = []   # (product, new_description, new_tagline)
        still_missing: list[str] = []

        if live:
            self.stdout.write("Re-scraping petkadai.com live (all categories)...")
            sku_map = _load_live_sku_map(self.stdout)
            self.stdout.write(
                f"Loaded {len(sku_map)} usable descriptions from the live site.\n"
            )

            # Preload sku -> product_id for all junk products in one query
            junk_ids = [p.id for p in junk_products]
            sku_by_product: dict = {}
            for pid, sku in ProductVariant.objects.filter(product_id__in=junk_ids).values_list("product_id", "sku"):
                sku_by_product.setdefault(pid, []).append(sku)

            for p in junk_products:
                match = None
                for sku in sku_by_product.get(p.id, []):
                    if sku in sku_map:
                        match = sku_map[sku]
                        break
                if match:
                    fixable.append((p, match["description"], match.get("tagline")))
                else:
                    still_missing.append(p.slug)
        else:
            if not os.path.isdir(json_dir):
                raise CommandError(f"JSON directory not found: {json_dir}")

            self.stdout.write(f"Scanning snapshots in {json_dir} ...")
            snapshot = _load_snapshot_map(json_dir)
            self.stdout.write(
                f"Loaded {len(snapshot['by_slug'])} usable descriptions from local snapshots.\n"
            )

            for p in junk_products:
                match = (
                    snapshot["by_slug"].get(p.slug)
                    or snapshot["by_name"].get((p.name or "").strip().lower())
                )
                if match:
                    fixable.append((p, match["description"], match.get("tagline")))
                else:
                    still_missing.append(p.slug)

        self.stdout.write(self.style.SUCCESS(f"Fixable from local snapshots: {len(fixable)}"))
        self.stdout.write(self.style.WARNING(f"Still missing (need live re-scrape): {len(still_missing)}\n"))

        for p, new_desc, _ in fixable[:15]:
            preview = (new_desc or "")[:70].replace("\n", " ")
            self.stdout.write(f"  [fix] {p.slug:<55} -> \"{preview}...\"")
        if len(fixable) > 15:
            self.stdout.write(f"  ... and {len(fixable) - 15} more")

        if apply_changes and fixable:
            self.stdout.write("")
            if description_only:
                self.stdout.write(self.style.WARNING("--description-only: tagline will NOT be touched.\n"))
            updated = 0
            with transaction.atomic():
                for p, new_desc, new_tagline in fixable:
                    p.description = new_desc
                    update_fields = ["description", "updated_at"]
                    if not description_only and not p.tagline and new_tagline:
                        p.tagline = new_tagline[:499]
                        update_fields.append("tagline")
                    p.save(update_fields=update_fields)
                    updated += 1
            self.stdout.write(self.style.SUCCESS(f"Updated {updated} products."))
        elif not apply_changes and fixable:
            self.stdout.write(self.style.WARNING("\nRe-run with --apply to write these fixes."))

        if still_missing_out and still_missing:
            with open(still_missing_out, "w", encoding="utf-8") as fh:
                fh.write("\n".join(still_missing))
            self.stdout.write(f"\nWrote {len(still_missing)} unresolved slugs to {still_missing_out}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS(
            f"Summary: junk={len(junk_products)}  fixable_locally={len(fixable)}  "
            f"still_missing={len(still_missing)}  applied={'yes' if apply_changes else 'no (dry-run)'}"
        ))
