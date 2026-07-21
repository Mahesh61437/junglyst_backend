"""
Management command: backfill_petkadai_descriptions
====================================================
Fixes the fallout of the `import_petkadai` bug where products with no
scraped description got `description = name` instead of real content
(e.g. a product literally described as "Ammonia Remover 500g").

For every Product whose description is junk (description == name), this
looks up the slug across the local petkadai JSON snapshots in
`pet_kadai/*.json` and, if a real (non-junk) description is found there,
stages an update. Products with no real description available anywhere
locally are left untouched and listed separately — they need a fresh
scrape from petkadai.com.

This is READ-ONLY by default. Nothing is written unless --apply is passed.

Usage
-----
  python manage.py backfill_petkadai_descriptions                # dry-run report only
  python manage.py backfill_petkadai_descriptions --apply         # write the fixes
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
            "--still-missing-out",
            default=None,
            help="Optional file path to write the list of slugs that couldn't be "
                 "fixed from local snapshots (need a fresh scrape).",
        )

    def handle(self, *args, **options):
        from core.models import Product

        json_dir          : str = options["json_dir"]
        apply_changes      : bool = options["apply"]
        limit               : int = options["limit"]
        still_missing_out : str | None = options["still_missing_out"]

        if not os.path.isdir(json_dir):
            raise CommandError(f"JSON directory not found: {json_dir}")

        self.stdout.write(
            self.style.WARNING("DRY RUN — no changes will be written.\n")
            if not apply_changes
            else self.style.WARNING("APPLY MODE — matched products WILL be updated.\n")
        )

        self.stdout.write(f"Scanning snapshots in {json_dir} ...")
        snapshot = _load_snapshot_map(json_dir)
        self.stdout.write(
            f"Loaded {len(snapshot['by_slug'])} usable descriptions from local snapshots.\n"
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
            updated = 0
            with transaction.atomic():
                for p, new_desc, new_tagline in fixable:
                    p.description = new_desc
                    if not p.tagline and new_tagline:
                        p.tagline = new_tagline[:499]
                    p.save(update_fields=["description", "tagline", "updated_at"])
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
