"""
Management command: seed_products
Creates 10 unique product listings per grower seller, each with 1-3 variants.
Safe to run multiple times — skips existing products by name+seller.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify
from decimal import Decimal
import uuid
from core.models import User, Product, ProductVariant, ProductImage, Category

CATALOG = [
    # (name, description, category_name, variants)
    # variants: list of (name, price, stock, item_category, weight_g, length, breadth, height)
    (
        "Anubias Nana Petite",
        "A miniature variety of Anubias ideal for foreground placement. Extremely slow growing and low maintenance, perfect for shrimp tanks.",
        "Aquatic Plants",
        [("Single Rhizome", 349, 20, "light", 60, 10, 8, 5),
         ("Portion (3 Rhizomes)", 899, 10, "light", 150, 12, 10, 6)],
    ),
    (
        "Bucephalandra Wavy Green",
        "Endemic to Borneo, this Bucephalandra variety features wavy green leaves with iridescent shimmer under LED lighting.",
        "Aquatic Plants",
        [("Single Stem", 249, 25, "light", 40, 8, 6, 4),
         ("Bunch (5 Stems)", 999, 12, "light", 180, 12, 10, 6)],
    ),
    (
        "Rotala Rotundifolia H'Ra",
        "A vibrant red stem plant that thrives under high light. Perfect for creating colour contrast in Dutch-style aquascapes.",
        "Aquatic Plants",
        [("Bunch (10 Stems)", 179, 30, "light", 100, 15, 8, 5)],
    ),
    (
        "Java Moss",
        "Classic aquarium moss that attaches easily to driftwood and rocks. Hardy and adaptable for beginners and experts alike.",
        "Mosses & Liverworts",
        [("Golf Ball Portion", 99, 50, "light", 50, 8, 8, 5),
         ("Large Portion (200g)", 349, 20, "light", 220, 15, 12, 8)],
    ),
    (
        "Flame Moss",
        "Unique upward-growing moss that resembles flickering flames. Creates stunning vertical texture in aquascapes.",
        "Mosses & Liverworts",
        [("Small Portion", 149, 35, "light", 55, 8, 8, 5),
         ("Large Portion", 399, 15, "light", 200, 14, 10, 7)],
    ),
    (
        "Eleocharis Parvula (Dwarf Hairgrass)",
        "Creates a stunning grass carpet effect. Best grown in nutrient-rich substrate with moderate to high lighting.",
        "Aquatic Plants",
        [("1 Pot", 129, 40, "light", 120, 7, 7, 12),
         ("3 Pots", 339, 18, "light", 340, 15, 12, 12)],
    ),
    (
        "Bolbitis Heudelotii",
        "African water fern with elegant dark green fronds. Grows well attached to hardscape and prefers good water flow.",
        "Rare Ferns",
        [("Single Rhizome (10cm)", 449, 15, "light", 90, 12, 10, 6),
         ("Large Rhizome (20cm)", 849, 8, "light", 200, 18, 14, 8)],
    ),
    (
        "Cryptocoryne Wendtii Brown",
        "A robust Crypt variety with distinctive brown-red leaves. Tolerates low light and minimal CO2, a true beginner plant.",
        "Aquatic Plants",
        [("Single Potted Plant", 139, 25, "light", 150, 9, 9, 12),
         ("3 Plants", 369, 12, "light", 420, 16, 14, 14)],
    ),
    (
        "Marsilea Hirsuta",
        "Foreground carpeting plant with four-leaf clover shape. Grows without CO2 at a moderate pace.",
        "Aquatic Plants",
        [("Portion (10 Runners)", 199, 30, "light", 80, 10, 8, 4),
         ("Large Portion (30 Runners)", 499, 12, "light", 200, 15, 12, 6)],
    ),
    (
        "Subwassertang",
        "Freshwater liverwort that creates lush, cloud-like mounds. Excellent for shrimp tanks as it provides hiding spots.",
        "Mosses & Liverworts",
        [("Golf Ball Portion", 119, 40, "light", 60, 9, 9, 6),
         ("Large Portion", 299, 20, "light", 180, 14, 12, 8)],
    ),
    # Extras so each seller gets a different set when we rotate
    (
        "Vesicularia Montagnei (Christmas Moss)",
        "Named for its branch structure resembling a Christmas tree. Rich green colour and easy to care for.",
        "Mosses & Liverworts",
        [("Small Portion", 129, 35, "light", 55, 8, 8, 5),
         ("Large Portion", 349, 15, "light", 200, 14, 10, 7)],
    ),
    (
        "Staurogyne Repens",
        "Compact bushy foreground plant with light green leaves. Ideal for Dutch aquascapes, grows without CO2.",
        "Aquatic Plants",
        [("Bunch (5 Stems)", 159, 30, "light", 100, 12, 8, 6),
         ("Bunch (15 Stems)", 399, 14, "light", 280, 18, 12, 8)],
    ),
    (
        "Taxiphyllum Alternans (Taiwan Moss)",
        "Beautiful feathery moss perfect for creating dense walls or carpets. Very hardy and fast growing.",
        "Mosses & Liverworts",
        [("Golf Ball Portion", 109, 40, "light", 55, 8, 8, 5)],
    ),
    (
        "Ludwigia Super Red Mini",
        "Intensely red stem plant that stays compact. Needs high light and CO2 to develop its signature red colouration.",
        "Aquatic Plants",
        [("Bunch (5 Stems)", 249, 20, "light", 110, 14, 8, 5),
         ("Bunch (10 Stems)", 449, 10, "light", 200, 18, 10, 6)],
    ),
    (
        "Hygrophila Pinnatifida",
        "Unusual stem plant with deeply pinnatifid leaves that can also be attached to hardscape like a rhizome plant.",
        "Aquatic Plants",
        [("Single Stem", 179, 25, "light", 70, 14, 8, 5),
         ("Bunch (5 Stems)", 749, 10, "light", 280, 18, 12, 7)],
    ),
]

PLACEHOLDER_IMAGES = [
    "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600",
    "https://images.unsplash.com/photo-1591858064831-a4ed24d4a9c2?w=600",
    "https://images.unsplash.com/photo-1622659580640-11e8adc9c9c8?w=600",
    "https://images.unsplash.com/photo-1574263867128-a3d5c1862e8e?w=600",
    "https://images.unsplash.com/photo-1632513370984-2f5e7c5e5b20?w=600",
    "https://images.unsplash.com/photo-1585320806297-9794b3e4aaae?w=600",
    "https://images.unsplash.com/photo-1509316785289-025f5b846b35?w=600",
    "https://images.unsplash.com/photo-1592150621744-aca64f48394a?w=600",
    "https://images.unsplash.com/photo-1614594975525-e45190c55d0b?w=600",
    "https://images.unsplash.com/photo-1616694006973-5b7ee5abcc0b?w=600",
]


class Command(BaseCommand):
    help = "Seed 10 unique product listings per grower seller"

    def handle(self, *args, **options):
        sellers = list(User.objects.filter(role="grower"))
        if not sellers:
            self.stderr.write("No grower users found. Create some first.")
            return

        cats = {c.name: c for c in Category.objects.all()}
        if not cats:
            self.stderr.write("No categories found. Seed categories first.")
            return

        default_cat = next(iter(cats.values()))
        created_total = 0

        for seller_idx, seller in enumerate(sellers):
            # Give each seller a unique slice of 10 from the catalog (rotate by seller index)
            start = (seller_idx * 3) % len(CATALOG)
            indices = [(start + i) % len(CATALOG) for i in range(10)]
            seller_catalog = [CATALOG[i] for i in indices]

            seller_created = 0
            for prod_idx, (name, desc, cat_name, variants) in enumerate(seller_catalog):
                full_name = f"{name} — {seller.username[:12]}" if len(sellers) > 1 else name

                if Product.objects.filter(seller=seller, name=full_name).exists():
                    self.stdout.write(f"  Skip (exists): {full_name}")
                    continue

                cat = cats.get(cat_name, default_cat)
                img_url = PLACEHOLDER_IMAGES[prod_idx % len(PLACEHOLDER_IMAGES)]

                with transaction.atomic():
                    base_slug = slugify(full_name)
                    slug = base_slug
                    if Product.objects.filter(slug=slug).exists():
                        slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
                    product = Product.objects.create(
                        seller=seller,
                        name=full_name,
                        slug=slug,
                        description=desc,
                        is_active=True,
                    )
                    ProductImage.objects.create(product=product, image_url=img_url, is_primary=True, order=0)
                    product.categories.add(cat)

                    for v_name, v_price, v_stock, v_cat, v_weight, v_l, v_b, v_h in variants:
                        gst = float(cat.gst_percentage)
                        base = round(v_price / (1 + gst / 100), 2)
                        ProductVariant.objects.create(
                            product=product,
                            name=v_name,
                            base_price=Decimal(str(base)),
                            gst_rate=cat.gst_percentage,
                            commission_rate=Decimal("15.00"),
                            price=Decimal(str(v_price)),
                            stock=v_stock,
                            item_category=v_cat,
                            packed_weight_grams=v_weight,
                            length=Decimal(str(v_l)),
                            width=Decimal(str(v_b)),
                            height=Decimal(str(v_h)),
                            is_active=True,
                        )

                self.stdout.write(f"  Created: {full_name} ({len(variants)} variants)")
                seller_created += 1

            self.stdout.write(self.style.SUCCESS(
                f"Seller '{seller.username}': {seller_created} products created"
            ))
            created_total += seller_created

        self.stdout.write(self.style.SUCCESS(f"\nDone. Total created: {created_total}"))
