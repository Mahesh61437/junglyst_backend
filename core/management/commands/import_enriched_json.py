import json
import os
from decimal import Decimal
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

# Junglyst defaults
DEFAULT_GST_RATE = Decimal("0.00")          # 0%
DEFAULT_COMMISSION_RATE = Decimal("0.00")  # 10%
DEFAULT_WEIGHT_KG = Decimal("0.20")
DEFAULT_LENGTH_CM = Decimal("10.00")
DEFAULT_WIDTH_CM = Decimal("10.00")
DEFAULT_HEIGHT_CM = Decimal("10.00")
DEFAULT_PACKED_WEIGHT_GRAMS = 200

class Command(BaseCommand):
    help = "Import enriched products from a JSON file into Junglyst, uploading images to Firebase Storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            required=True,
            help="Path to the aquatic_exotica_enriched.json file.",
        )
        parser.add_argument(
            "--seller-id",
            default="",
            help="UUID of the Junglyst seller user to assign products to.",
        )
        parser.add_argument(
            "--seller-email",
            default="",
            help="Email of the Junglyst seller user (alternative to --seller-id).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse products but do not write to the database.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Overwrite fields on products that already exist in the DB.",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Do not download or upload images (keeps existing URLs).",
        )
        parser.add_argument(
            "--force-images",
            action="store_true",
            help="Force delete and re-download images even if they already exist.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after importing this many products (0 = no limit).",
        )

    def handle(self, *args, **options):
        from core.models import (
            Category, Product, ProductImage, ProductVariant, SubCategory, Tag, User
        )
        from core.storage import upload_to_firebase

        json_file: str = options["file"]
        dry_run: bool = options["dry_run"]
        do_update: bool = options["update"]
        skip_images: bool = options["skip_images"]
        force_images: bool = options["force_images"]
        limit: int = options["limit"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be written."))

        seller = self._resolve_seller(User, options["seller_id"], options["seller_email"])
        self.stdout.write(f"Seller  : {seller.email}  (id={seller.id})")

        if not os.path.exists(json_file):
            self.stderr.write(self.style.ERROR(f"File not found: {json_file}"))
            return

        with open(json_file, "r", encoding="utf-8") as f:
            products = json.load(f)
            
        if limit:
            products = products[:limit]
            
        self.stdout.write(f"  -> {len(products)} products to process from {json_file}")

        created = updated = skipped = img_ok = img_fail = errors = 0
        existing_slugs: set = set(Product.all_objects.values_list("slug", flat=True))

        for idx, raw in enumerate(products, start=1):
            ae_id = raw.get("source_id")
            ae_key = f"ae-{ae_id}"
            slug = self._name_slug(raw.get("name", ""), ae_id, existing_slugs)

            try:
                result, n_img_ok, n_img_fail = self._import_product(
                    raw=raw,
                    slug=slug,
                    ae_key=ae_key,
                    seller=seller,
                    do_update=do_update,
                    skip_images=skip_images,
                    force_images=force_images,
                    dry_run=dry_run,
                    upload_fn=upload_to_firebase,
                    models={
                        "Product": Product,
                        "ProductVariant": ProductVariant,
                        "ProductImage": ProductImage,
                        "Category": Category,
                        "SubCategory": SubCategory,
                        "Tag": Tag,
                    },
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(
                    self.style.ERROR(f"  [{idx}/{len(products)}] ERROR  {slug} - {exc}")
                )
                continue

            if result == "created": created += 1
            elif result == "updated": updated += 1
            elif result == "skipped": skipped += 1

            img_ok += n_img_ok
            img_fail += n_img_fail

            status_label = {"created": "CREATED", "updated": "UPDATED", "skipped": "SKIPPED"}[result]
            img_note = f"  [{n_img_ok} imgs]" if n_img_ok else ""
            self.stdout.write(
                f"  [{idx}/{len(products)}] {status_label}  {slug}  (ae_key={ae_key})  "
                f"{raw.get('name', '')[:55]}{img_note}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("-" * 60))
        self.stdout.write(self.style.SUCCESS(
            f"Done.  created={created}  updated={updated}  skipped={skipped}  "
            f"errors={errors}  images_uploaded={img_ok}  images_failed={img_fail}"
        ))

    def _resolve_seller(self, User, seller_id: str, seller_email: str):
        if seller_id:
            return User.objects.get(id=seller_id)
        if seller_email:
            return User.objects.get(email=seller_email)
        
        seller = User.objects.filter(email="accessmaheshforu@gmail.com").first()
        if not seller:
            seller = User.objects.create_user(
                username="system_auto_importer",
                email="accessmaheshforu@gmail.com",
                password="AutoGeneratedSuperSecret",
                first_name="System",
                last_name="AutoImporter"
            )
        return seller

    def _name_slug(self, name: str, ae_id, existing_slugs: set) -> str:
        base = slugify(name) or f"ae-{ae_id}"
        slug = base
        counter = 1
        while slug in existing_slugs:
            slug = f"{base}-{counter}"
            counter += 1
        existing_slugs.add(slug)
        return slug

    @transaction.atomic
    def _import_product(self, *, raw: dict, slug: str, ae_key: str, seller, do_update: bool, skip_images: bool, force_images: bool, dry_run: bool, upload_fn, models: dict) -> tuple[str, int, int]:
        Product = models["Product"]
        ProductVariant = models["ProductVariant"]
        ProductImage = models["ProductImage"]
        Category = models["Category"]
        SubCategory = models["SubCategory"]
        Tag = models["Tag"]

        existing = (
            Product.all_objects.filter(slug=ae_key).first()
            or Product.all_objects.filter(slug=slug).first()
            or Product.all_objects.filter(seller=seller, name__iexact=raw.get("name", "")).first()
        )

        if existing and not do_update:
            return "skipped", 0, 0
        if dry_run:
            return ("created" if not existing else "updated"), 0, 0

        # -- Product ----------------------------------------------------------
        product_fields = dict(
            name=raw.get("name", ""),
            tagline="",
            description=raw.get("description", ""),
            seller=seller,
            scientific_name=raw.get("scientific_name", ""),
            care_level=raw.get("care_level", ""),
            light_requirements=raw.get("light_requirements", ""),
            growth_rate=raw.get("growth_rate", ""),
            co2_requirement=raw.get("co2_requirement", ""),
            water_temperature=raw.get("water_temperature", ""),
            ph_range=raw.get("ph_range", ""),
            origin=raw.get("origin", ""),
            is_rare=False,
            is_active=True,
            is_draft=False,
            rating=Decimal("5.0"),
        )

        if existing:
            for field, value in product_fields.items():
                setattr(existing, field, value)
            if existing.slug == ae_key:
                existing.slug = slug
            existing.save()
            product = existing
            action = "updated"
        else:
            product = Product(slug=slug, **product_fields)
            product.save()
            action = "created"

        # -- Categories (M2M) -------------------------------------------------
        cats = []
        subs = []
        for c_name in raw.get("categories", []):
            try:
                cat_obj = Category.objects.get(name=c_name)
                cats.append(cat_obj)
            except Category.DoesNotExist:
                pass

        for s_name in raw.get("sub_categories", []):
            try:
                sub_obj = SubCategory.objects.get(name=s_name)
                subs.append(sub_obj)
            except SubCategory.DoesNotExist:
                pass

        product.categories.set(cats)
        product.sub_categories.set(subs)

        tags = []
        for tag_name in raw.get("tags", []):
            tag, _ = Tag.objects.get_or_create(name=tag_name)
            tags.append(tag)
        product.tags.set(tags)

        # -- Variant ----------------------------------------------------------
        raw_price = raw.get("price")
        if raw_price:
            # Final price = api_price + 10% commission + 0% GST
            # Therefore, base_price is exactly the API price
            base_price = Decimal(str(raw_price))
        else:
            base_price = Decimal("0.00")

        stock = int(raw.get("stock") or 0)

        variant_qs = ProductVariant.all_objects.filter(product=product, name="Standard")
        if variant_qs.exists():
            variant = variant_qs.first()
            if action == "updated":
                variant.base_price = base_price
                variant.gst_rate = DEFAULT_GST_RATE
                variant.commission_rate = DEFAULT_COMMISSION_RATE
                variant.stock = stock
                variant.save()
        else:
            variant = ProductVariant(
                product=product,
                name="Standard",
                variant_type="Plant",
                base_price=base_price,
                gst_rate=DEFAULT_GST_RATE,
                commission_rate=DEFAULT_COMMISSION_RATE,
                price=Decimal("0.00"),
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

        # -- Images -----------------------------------------------------------
        img_ok = img_fail = 0

        if not skip_images:
            has_existing_images = False
            if action == "updated" and not force_images:
                has_existing_images = ProductImage.all_objects.filter(product=product).exists()

            if action == "updated" and force_images:
                ProductImage.all_objects.filter(product=product).delete()

            if not has_existing_images:
                candidate_urls = raw.get("images", [])
                for order, src_url in enumerate(candidate_urls):
                    if not src_url: continue
                    # In this script, we download from src_url and upload to Firebase using upload_to_firebase
                    import requests
                    try:
                        r = requests.get(src_url, stream=True, timeout=10)
                        r.raise_for_status()
                        data = r.raw.read()
                        content_type = r.headers.get("Content-Type", "image/jpeg")
                        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
                        
                        import io
                        img_file = io.BytesIO(data)
                        img_file.name = f"product.{ext}"
                        img_file.content_type = content_type

                        junglyst_url = upload_fn(img_file, str(seller.id), "product")
                        
                        if junglyst_url:
                            ProductImage.objects.create(
                                product=product,
                                variant=variant,
                                image_url=junglyst_url,
                                is_primary=(order == 0),
                                order=order,
                            )
                            img_ok += 1
                        else:
                            print(f"Failed to get junglyst_url for {src_url}")
                            img_fail += 1
                    except Exception as e:
                        print(f"Exception during upload for {src_url}: {e}")
                        img_fail += 1

        return action, img_ok, img_fail
