import json
import tempfile
import zipfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from sellers.models import SellerProfile
from .management.commands import import_greenwater as gw
from .models import Category, Product, ProductImage, ProductVariant, SubCategory

User = get_user_model()

SPECIES = {
    "display_name": "Rotala rotundifolia 'Green'", "scientific_name": "Rotala rotundifolia",
    "origin": "South & Southeast Asia", "growth_rate": "Fast", "co2_requirement": "Low",
    "water_temperature": "20–28°C", "ph_range": "6.0–7.5", "light_requirements": "High",
    "care_level": "Easy", "tagline": "Fine green Rotala.",
    "description": "Curated paragraph one.\n\nCurated paragraph two.",
}


def _row(sku, name="Plant", **overrides):
    row = {
        "SKU": sku, "Name": name, "Description": "", "Short description": "",
        "Sale price": "100", "Regular price": "175", "Stock": "7", "Tax class": "",
        "Weight (kg)": "0.05", "Length (cm)": "7.5", "Width (cm)": "7.5", "Height (cm)": "5",
        "Categories": "Emersed Grown Pots, Background plants",
        "Images": "https://example.com/a.jpg, https://example.com/b.jpg",
        "Attribute 1 name": "Light Requirement", "Attribute 1 value(s)": "Very Bright",
        "Attribute 2 name": "Plant Difficulty", "Attribute 2 value(s)": "Medium",
        "Attribute 3 name": "Plant Type", "Attribute 3 value(s)": "Stem",
    }
    row.update(overrides)
    return row


def _enrichment(products, species=None, supplies=None):
    return {
        "seller_store_slug": "green-water-aquascapes",
        "text_fixes": [["Roatla", "Rotala"]],
        "supplies": supplies or {},
        "species": species or {"rotala-green": dict(SPECIES)},
        "products": products,
    }


class DescriptionCleaningTests(SimpleTestCase):
    def test_strips_attributes_scripts_and_unknown_tags(self):
        html = gw.clean_description_html(
            '<div class="x" style="color:red"><strong onclick="evil()">Bold</strong> text'
            '<script>alert(1)</script><img src=x onerror=alert(1)></div>'
        )
        self.assertEqual(html, "<p><strong>Bold</strong> text</p>")

    def test_excel_line_breaks_become_paragraphs_and_shortcodes_are_removed(self):
        html = gw.clean_description_html("First line_x000D_\\nSecond line [gh_calculator]")
        self.assertEqual(html, "<p>First line</p>\n<p>Second line</p>")

    def test_escapes_text_and_marks_wide_gaps(self):
        html = gw.clean_description_html("Genus: Rotala      Family: Lythraceae &lt;b&gt;")
        self.assertEqual(html, "<p>Genus: Rotala · Family: Lythraceae &lt;b&gt;</p>")

    def test_removes_repeated_title_lines(self):
        html = gw.clean_description_html("<strong>Hygrophila</strong>\\nHygrophila\\nBody text")
        self.assertEqual(html, "<p><strong>Hygrophila</strong></p>\n<p>Body text</p>")

    def test_dash_lines_and_html_lists_become_lists(self):
        self.assertEqual(
            gw.clean_description_html("Why?\\n- One\\n- Two"),
            "<p>Why?</p>\n<ul>\n<li>One</li>\n<li>Two</li>\n</ul>",
        )
        self.assertEqual(
            gw.clean_description_html('<ol><li class="a">A</li><li>B</li></ol>'),
            "<ol>\n<li>A</li>\n<li>B</li>\n</ol>",
        )

    def test_unclosed_inline_tags_are_closed(self):
        self.assertEqual(gw.clean_description_html("<strong>Never closed"), "<p><strong>Never closed</strong></p>")

    def test_applies_text_fixes(self):
        self.assertEqual(gw.clean_description_html("Genus: Roatla", [["Roatla", "Rotala"]]), "<p>Genus: Rotala</p>")

    def test_empty_input(self):
        self.assertEqual(gw.clean_description_html(""), "")
        self.assertEqual(gw.clean_description_html("<blockquote>&nbsp;</blockquote>"), "")


class MergeDescriptionTests(SimpleTestCase):
    LONG = "<p>" + "A long and useful description of this aquarium plant. " * 3 + "</p>"
    OTHER = "<p>" + "Completely different care notes about light and CO2 levels. " * 2 + "</p>"

    def test_duplicate_short_description_is_dropped(self):
        self.assertEqual(gw.merge_seller_descriptions(self.LONG, self.LONG), self.LONG)

    def test_distinct_descriptions_are_combined(self):
        self.assertEqual(gw.merge_seller_descriptions(self.LONG, self.OTHER), f"{self.LONG}\n{self.OTHER}")

    def test_falls_back_to_short_description(self):
        self.assertEqual(gw.merge_seller_descriptions("<p>tiny</p>", self.OTHER), self.OTHER)

    def test_both_too_short_returns_empty(self):
        self.assertEqual(gw.merge_seller_descriptions("<p>a</p>", "<p>b</p>"), "")


class FieldMappingTests(SimpleTestCase):
    def test_light_and_care_mapping(self):
        self.assertEqual(gw.map_light("Very Bright"), "High")
        self.assertEqual(gw.map_light("bright"), "Medium to High")
        self.assertIsNone(gw.map_light("Sunny"))
        self.assertEqual(gw.map_care_level("Medium"), "Moderate")
        self.assertEqual(gw.map_care_level("Difficult"), "Advanced")
        self.assertIsNone(gw.map_care_level(None))

    def test_variant_detection(self):
        self.assertEqual(gw.plant_variant("#GWAT001", {}, {}), ("Tissue Culture Cup", "Tissue Culture"))
        self.assertEqual(gw.plant_variant("#GWAE102", {"Packing": "10 stems"}, {}), ("10 Stems", "Bunch"))
        self.assertEqual(gw.plant_variant("#GWAE001", {"Packing": "Pots"}, {}), ("Pot", "Pot"))
        self.assertEqual(
            gw.plant_variant("#GWAF001", {}, {"variant_name": "Portion", "variant_type": "Other"}),
            ("Portion", "Other"),
        )

    def test_image_urls_are_split_deduplicated_and_https_only(self):
        self.assertEqual(
            gw.parse_image_urls("https://a/x.jpg, https://a/x.jpg,http://insecure/y.jpg, javascript:alert(1)"),
            ["https://a/x.jpg"],
        )

    def test_categories_for_pot_tissue_culture_and_terrestrial(self):
        species = dict(SPECIES, subcategories=["Carpet Plants"], is_rare=True)
        self.assertEqual(gw.plant_categories(species, "#GWAE001", "Pot"), [
            ("Plants", "Aquatic Plants"), ("Plants", "Pot Plants"),
            ("Plants", "Carpet Plants"), ("Plants", "Rare and Exotic"),
        ])
        self.assertIn(("Plants", "Tissue Culture"), gw.plant_categories(species, "#GWAT001", "Tissue Culture"))
        terrestrial = dict(SPECIES, is_terrestrial=True, subcategories=["Ferns"])
        self.assertEqual(gw.plant_categories(terrestrial, "#GWAP001", "Pot"),
                         [("Terrarium & Paludarium", None), ("Plants", "Ferns")])

    def test_beginner_friendly_uses_species_data(self):
        low_light_easy = dict(SPECIES, light_requirements="Low", care_level="Easy")
        self.assertIn(("Plants", "Beginner Friendly"), gw.plant_categories(low_light_easy, "#GWAE1", "Pot"))
        self.assertNotIn(("Plants", "Beginner Friendly"), gw.plant_categories(SPECIES, "#GWAE1", "Pot"))


class BuildSpecTests(SimpleTestCase):
    def test_seller_attributes_win_and_species_fills_gaps(self):
        [spec] = gw.build_product_specs([_row("#GWAE038")], _enrichment({"#GWAE038": {"species": "rotala-green"}}))
        self.assertEqual(spec.light_requirements, "High")
        self.assertEqual(spec.care_level, "Moderate")  # seller's "Medium"
        self.assertEqual(spec.scientific_name, "Rotala rotundifolia")
        self.assertEqual(spec.tags, ["Stem", "Background"])
        self.assertEqual(spec.description, "<p>Curated paragraph one.</p>\n<p>Curated paragraph two.</p>")
        self.assertEqual(spec.variant.base_price, Decimal("100"))
        self.assertEqual(spec.variant.regular_price, Decimal("175"))
        self.assertEqual(spec.warnings, [])

    def test_tissue_culture_name_suffix(self):
        [spec] = gw.build_product_specs([_row("#GWAT019")], _enrichment({"#GWAT019": {"species": "rotala-green"}}))
        self.assertEqual(spec.name, "Rotala rotundifolia 'Green' (Tissue Culture)")
        self.assertEqual(spec.variant.variant_type, "Tissue Culture")

    def test_description_from_sibling_and_species_override(self):
        seller_copy = "A thorough description written by the seller about this plant and its care."
        rows = [_row("#GWAE038", Description=seller_copy), _row("#GWAT019")]
        specs = gw.build_product_specs(rows, _enrichment({
            "#GWAE038": {"species": "rotala-green", "use_species_description": True},
            "#GWAT019": {"species": "rotala-green", "description_from": "#GWAE038"},
        }))
        by_sku = {s.sku: s for s in specs}
        self.assertIn("Curated paragraph one", by_sku["#GWAE038"].description)
        self.assertIn("thorough description", by_sku["#GWAT019"].description)

    def test_supply_rows_use_reduced_gst_and_configured_categories(self):
        supply = {"name": "GWA Carbon", "variant_name": "Standard", "tagline": "Liquid carbon.",
                  "categories": [["Aquascaping", "Fertilizers"]], "packed_weight_grams": 350}
        row = _row("#GWAPT01", **{"Tax class": "5tax", "Description": "Liquid carbon for planted tanks. " * 3})
        [spec] = gw.build_product_specs([row], _enrichment({}, supplies={"#GWAPT01": supply}))
        self.assertEqual(spec.variant.gst_rate, Decimal("5.00"))
        self.assertEqual(spec.categories, [("Aquascaping", "Fertilizers")])
        self.assertEqual(spec.variant.packed_weight_grams, 350)

    def test_sibling_images_only_assigned_to_rows_without_images(self):
        rows = [_row("#GWAE038"), _row("#GWAT019", Images="")]
        specs = gw.build_product_specs(rows, _enrichment({
            "#GWAE038": {"species": "rotala-green"}, "#GWAT019": {"species": "rotala-green"},
        }))
        by_sku = {s.sku: s for s in specs}
        self.assertEqual(by_sku["#GWAE038"].fallback_image_urls, [])
        self.assertEqual(by_sku["#GWAT019"].fallback_image_urls, ["https://example.com/a.jpg", "https://example.com/b.jpg"])

    def test_unmapped_sku_raises(self):
        with self.assertRaisesMessage(CommandError, "#GWAX999"):
            gw.build_product_specs([_row("#GWAX999")], _enrichment({}))

    def test_missing_price_is_reported(self):
        [spec] = gw.build_product_specs(
            [_row("#GWAE038", **{"Sale price": "", "Regular price": "abc"})],
            _enrichment({"#GWAE038": {"species": "rotala-green"}}),
        )
        self.assertEqual(spec.variant.base_price, Decimal("0.00"))
        self.assertIn("no valid price", spec.warnings)


class XlsxReaderTests(SimpleTestCase):
    def _write_xlsx(self, path):
        shared = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  '<si><t>SKU</t></si><si><t>Name</t></si><si><t>#GWAE001</t></si><si><t>Lilacina</t></si></sst>')
        sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="inlineStr"><is><t>Stock</t></is></c></row>'
                 '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c><c r="C2"><v>4</v></c></row>'
                 '<row r="3"></row></sheetData></worksheet>')
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", shared)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

    def test_reads_shared_inline_and_numeric_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.csv"  # WooCommerce mislabels the workbook
            self._write_xlsx(path)
            self.assertEqual(gw.read_xlsx_rows(path), [{"SKU": "#GWAE001", "Name": "Lilacina", "Stock": "4"}])

    def test_non_workbook_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.csv"
            path.write_text("SKU,Name\n")
            with self.assertRaises(CommandError):
                gw.read_xlsx_rows(path)


class ShippedDataIntegrityTests(SimpleTestCase):
    """Guards the committed export + enrichment file against silent regressions."""

    def test_every_export_row_maps_cleanly(self):
        rows = gw.read_xlsx_rows(gw.DEFAULT_EXPORT_PATH)
        enrichment = json.loads(gw.DEFAULT_ENRICHMENT_PATH.read_text(encoding="utf-8"))
        specs = gw.build_product_specs(rows, enrichment)

        self.assertEqual(len(specs), 147)
        self.assertEqual([s for s in specs if s.warnings], [])
        names = [s.name for s in specs]
        self.assertEqual(len(names), len(set(names)), "product names must be unique")
        for spec in specs:
            self.assertGreaterEqual(len(gw._plain_text(spec.description)), gw.MIN_DESCRIPTION_CHARS, spec.sku)
            self.assertTrue(spec.categories, spec.sku)
            self.assertGreater(spec.variant.base_price, 0, spec.sku)
            self.assertNotIn("�", spec.name + spec.description, spec.sku)
            self.assertNotIn("<script", spec.description, spec.sku)
            self.assertNotIn("style=", spec.description, spec.sku)


class ImportCommandTests(TestCase):
    def setUp(self):
        cache.clear()
        self.seller = User.objects.create_user(
            email="gwa@test.com", username="gwa", password="Pass@12345", role="grower",
        )
        SellerProfile.objects.create(user=self.seller, store_name="Green Water Aquascapes",
                                     slug="green-water-aquascapes")
        plants = Category.objects.create(name="Plants", slug="plants")
        for sub in ("Aquatic Plants", "Pot Plants", "Tissue Culture"):
            SubCategory.objects.create(category=plants, name=sub, slug=f"plants-{sub.lower().replace(' ', '-')}")

        self.tmp = tempfile.TemporaryDirectory()
        self.enrichment_path = Path(self.tmp.name) / "enrichment.json"
        self.enrichment_path.write_text(json.dumps(_enrichment({
            "#GWAE038": {"species": "rotala-green"},
            "#GWAT019": {"species": "rotala-green"},
        })), encoding="utf-8")
        self.rows = [_row("#GWAE038"), _row("#GWAT019", **{"Sale price": "250", "Regular price": "260"})]

    def tearDown(self):
        self.tmp.cleanup()
        cache.clear()

    def _run(self, *extra):
        out = StringIO()
        with mock.patch.object(gw, "read_xlsx_rows", return_value=self.rows):
            call_command("import_greenwater", "--enrichment", str(self.enrichment_path), *extra, stdout=out)
        return out.getvalue()

    def _existing_product(self, sku):
        product = Product.objects.create(name="Rotala �Green�", seller=self.seller, description="old",
                                         is_active=False)
        variant = ProductVariant.objects.create(product=product, name="Standard", sku=sku,
                                                base_price=Decimal("1"), price=Decimal("1"), stock=0)
        ProductImage.objects.create(product=product, variant=variant, image_url="https://old/x.jpg", is_primary=True)
        return product

    def test_creates_new_products_with_taxonomy_variant_and_images(self):
        self._run()
        variant = ProductVariant.objects.select_related("product").get(sku="#GWAE038")
        product = variant.product
        self.assertEqual(product.seller, self.seller)
        self.assertTrue(product.is_active)
        self.assertEqual(product.name, "Rotala rotundifolia 'Green'")
        self.assertEqual(set(product.sub_categories.values_list("name", flat=True)), {"Aquatic Plants", "Pot Plants"})
        self.assertEqual(variant.variant_type, "Pot")
        self.assertEqual(variant.base_price, Decimal("100.00"))
        self.assertEqual(variant.price, Decimal("110.00"))  # default 10% seller-side markup
        self.assertEqual(variant.compare_at_price, Decimal("175.00"))
        self.assertEqual(variant.stock, 7)
        self.assertEqual(list(product.images.order_by("order").values_list("image_url", "is_primary")),
                         [("https://example.com/a.jpg", True), ("https://example.com/b.jpg", False)])

    def test_compare_at_price_dropped_when_not_above_buyer_price(self):
        self._run()
        tissue_culture = ProductVariant.objects.get(sku="#GWAT019")
        self.assertEqual(tissue_culture.price, Decimal("275.00"))
        self.assertIsNone(tissue_culture.compare_at_price)

    def test_updates_existing_product_in_place(self):
        existing = self._existing_product("#GWAE038")
        self._run()
        existing.refresh_from_db()
        self.assertEqual(Product.all_objects.filter(seller=self.seller).count(), 2)
        self.assertEqual(existing.name, "Rotala rotundifolia 'Green'")
        self.assertTrue(existing.is_active)
        self.assertEqual(existing.variants.get().stock, 7)
        self.assertEqual(list(existing.images.values_list("image_url", flat=True)),
                         ["https://example.com/a.jpg", "https://example.com/b.jpg"])

    def test_rerun_is_idempotent(self):
        self._run()
        self._run()
        self.assertEqual(Product.objects.filter(seller=self.seller).count(), 2)
        self.assertEqual(ProductImage.all_objects.count(), 4)

    def test_keeps_existing_images_when_row_has_none(self):
        existing = self._existing_product("#GWAE038")
        self.rows[0]["Images"] = ""
        self._run()
        self.assertEqual(list(existing.images.values_list("image_url", flat=True)), ["https://old/x.jpg"])

    def test_row_without_images_borrows_sibling_photos_when_product_has_none(self):
        self.rows[1]["Images"] = ""
        self._run()
        tissue_culture = ProductVariant.objects.get(sku="#GWAT019").product
        self.assertEqual(list(tissue_culture.images.order_by("order").values_list("image_url", flat=True)),
                         ["https://example.com/a.jpg", "https://example.com/b.jpg"])

    def test_sibling_photos_never_replace_existing_images(self):
        existing = self._existing_product("#GWAT019")
        self.rows[1]["Images"] = ""
        self._run()
        self.assertEqual(list(existing.images.values_list("image_url", flat=True)), ["https://old/x.jpg"])

    def test_dry_run_writes_nothing(self):
        output = self._run("--dry-run")
        self.assertIn("Would create 2 and update 0 products.", output)
        self.assertFalse(Product.objects.exists())

    def test_refuses_sku_owned_by_another_seller(self):
        other = User.objects.create_user(email="other@test.com", username="other", password="Pass@12345")
        product = Product.objects.create(name="Not theirs", seller=other, description="x")
        ProductVariant.objects.create(product=product, sku="#GWAE038", base_price=Decimal("1"), price=Decimal("1"))
        with self.assertRaisesMessage(CommandError, "another seller"):
            self._run()
        product.refresh_from_db()
        self.assertEqual(product.name, "Not theirs")

    def test_missing_subcategory_aborts_before_writing(self):
        SubCategory.objects.filter(name="Pot Plants").delete()
        with self.assertRaisesMessage(CommandError, "Plants > Pot Plants"):
            self._run()
        self.assertFalse(Product.objects.exists())

    def test_missing_seller_profile_aborts(self):
        SellerProfile.objects.all().delete()
        with self.assertRaisesMessage(CommandError, "green-water-aquascapes"):
            self._run()
