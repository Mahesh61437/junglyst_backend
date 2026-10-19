from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from analytics import sync_utils
from analytics.models import StockSyncSession
from core.models import Product, ProductVariant

User = get_user_model()


def _petkadai_item(sku, price=100, stock=None, status="IN_STOCK", name="Plant"):
    return {
        "sku": sku, "name": name, "stock_status": status, "only_x_left_in_stock": stock,
        "price_range": {"minimum_price": {"final_price": {"value": price}}},
    }


class SyncTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.seller = User.objects.create_user(email="supplier@test.com", username="supplier",
                                               password="Pass@12345", role="grower")
        self.other_seller = User.objects.create_user(email="other@test.com", username="other",
                                                     password="Pass@12345", role="grower")

    def tearDown(self):
        cache.clear()

    def _variant(self, sku, seller=None, stock=5, base=Decimal("100")):
        product = Product.objects.create(name=f"Product {sku}", seller=seller or self.seller, description="x")
        return ProductVariant.objects.create(product=product, sku=sku, base_price=base, price=base, stock=stock)


class ExtractorTests(TestCase):
    def test_petkadai_stock_rules(self):
        self.assertEqual(sync_utils._extract_petkadai(_petkadai_item("A", stock=3))["new_stock"], 3)
        self.assertEqual(sync_utils._extract_petkadai(_petkadai_item("A", status="OUT_OF_STOCK"))["new_stock"], 0)
        # IN_STOCK without a count must not overwrite the stored stock
        self.assertIsNone(sync_utils._extract_petkadai(_petkadai_item("A"))["new_stock"])
        self.assertIsNone(sync_utils._extract_petkadai(_petkadai_item("")))

    def test_himadri_uses_lower_price_and_markup(self):
        parsed = sync_utils._extract_himadri(
            {"sku": "H1", "name": "Anubias", "sale_price": 80, "regular_price": 100, "stock_status": "4 in stock"},
            Decimal("1.10"),
        )
        self.assertEqual(parsed["new_base"], Decimal("88"))
        self.assertEqual(parsed["new_stock"], 4)
        self.assertIsNone(sync_utils._extract_himadri({"sku": "H1", "sale_price": None}, Decimal("1")))


class ComputeDiffTests(SyncTestBase):
    def test_stock_applied_now_and_price_queued_for_approval(self):
        variant = self._variant("PK1", stock=5, base=Decimal("100"))
        result = sync_utils.compute_diff([_petkadai_item("PK1", price=120, stock=2)], "petkadai",
                                         self.seller.email, None, None, None)
        variant.refresh_from_db()
        self.assertEqual(variant.stock, 2)
        self.assertEqual(variant.base_price, Decimal("100.00"))  # price waits for approval
        self.assertEqual(result["stock_updated"], 1)
        session = StockSyncSession.objects.get(key=result["session_key"])
        self.assertIn(str(variant.id), session.price_changes)

    def test_unknown_sku_becomes_import_candidate(self):
        result = sync_utils.compute_diff([_petkadai_item("NEW1", stock=4)], "petkadai",
                                         self.seller.email, None, None, None)
        self.assertEqual(result["not_found"], 1)
        session = StockSyncSession.objects.get(key=result["session_key"])
        self.assertEqual(session.new_products["NEW1"]["new_stock"], 4)
        # the browser gets the lean row; the full scraped detail stays in the session for the import
        self.assertNotIn("_raw", result["new_products"][0])
        self.assertEqual(result["new_products"][0]["sku"], "NEW1")
        self.assertIn("_raw", session.new_products["NEW1"])

    def test_petkadai_zeroes_only_the_selected_sellers_missing_skus(self):
        seen = self._variant("PK1", stock=5)
        missing = self._variant("PK2", stock=5)
        other_sellers = self._variant("OT1", seller=self.other_seller, stock=5)
        sync_utils.compute_diff([_petkadai_item("PK1", stock=5)], "petkadai",
                                self.seller.email, None, None, None)
        for variant in (seen, missing, other_sellers):
            variant.refresh_from_db()
        self.assertEqual(seen.stock, 5)
        self.assertEqual(missing.stock, 0)
        self.assertEqual(other_sellers.stock, 5)

    def test_himadri_never_zeroes_missing_skus(self):
        missing = self._variant("HM2", stock=5)
        sync_utils.compute_diff([{"sku": "HM1", "sale_price": 50, "stock": 3}], "himadri",
                                self.seller.email, None, None, None)
        missing.refresh_from_db()
        self.assertEqual(missing.stock, 5)


def _gql_page(items, total_count, total_pages=1):
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": {"products": {
        "total_count": total_count, "items": items, "page_info": {"total_pages": total_pages}}}}
    return response


class PetkadaiFetchCompletenessTests(TestCase):
    @mock.patch("analytics.sync_utils.requests.post")
    def test_complete_when_all_pages_arrive(self, post):
        post.side_effect = [_gql_page([{"sku": "A"}, {"sku": "B"}], 3, 2), _gql_page([{"sku": "C"}], 3, 2)]
        items, complete = sync_utils._fetch_petkadai_category("uid", "Plants")
        self.assertEqual(len(items), 3)
        self.assertTrue(complete)

    @mock.patch("analytics.sync_utils.requests.post")
    def test_incomplete_when_a_later_page_times_out(self, post):
        import requests
        post.side_effect = [_gql_page([{"sku": "A"}], 3, 2), requests.Timeout()]
        items, complete = sync_utils._fetch_petkadai_category("uid", "Plants")
        self.assertEqual(len(items), 1)
        self.assertFalse(complete)

    @mock.patch("analytics.sync_utils.requests.post")
    def test_incomplete_when_api_returns_fewer_items_than_total_count(self, post):
        post.side_effect = [_gql_page([{"sku": "A"}], 40, 2), _gql_page([], 40, 2)]
        self.assertFalse(sync_utils._fetch_petkadai_category("uid", "Plants")[1])

    @mock.patch("analytics.sync_utils.requests.post")
    def test_incomplete_on_graphql_errors_and_bad_json(self, post):
        errors = mock.Mock(raise_for_status=mock.Mock(), json=mock.Mock(return_value={"errors": [{"message": "x"}]}))
        post.return_value = errors
        self.assertFalse(sync_utils._fetch_petkadai_category("uid", "Plants")[1])
        post.return_value = mock.Mock(raise_for_status=mock.Mock(), json=mock.Mock(side_effect=ValueError))
        self.assertFalse(sync_utils._fetch_petkadai_category("uid", "Plants")[1])

    @mock.patch("analytics.sync_utils.requests.post")
    def test_empty_category_with_zero_total_is_complete(self, post):
        post.return_value = _gql_page([], 0)
        self.assertEqual(sync_utils._fetch_petkadai_category("uid", "Plants"), ([], True))

    @mock.patch("analytics.sync_utils._fetch_petkadai_category")
    def test_scrape_lists_incomplete_categories(self, fetch):
        fetch.side_effect = lambda uid, name: ([], name != "Fish Food")
        result = sync_utils.scrape_live_petkadai()
        self.assertEqual(result.incomplete, ["Fish Food"])


class DelistGuardTests(SyncTestBase):
    def _run(self, records, **kwargs):
        return sync_utils.compute_diff(records, "petkadai", self.seller.email, None, None, None, **kwargs)

    def test_incomplete_scrape_does_not_zero_but_still_updates_matched_stock(self):
        seen, missing = self._variant("PK1", stock=5), self._variant("PK2", stock=5)
        result = self._run([_petkadai_item("PK1", stock=2)], scrape_incomplete=["Fish Food"])
        seen.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(seen.stock, 2)      # matched products still sync
        self.assertEqual(missing.stock, 5)   # unseen products are left alone
        self.assertEqual(result["delisted"], 0)
        self.assertIn("Fish Food", result["delist_skipped"])
        self.assertEqual(result["scrape_incomplete"], ["Fish Food"])

    def test_wrong_seller_selected_does_not_zero_their_catalogue(self):
        variants = [self._variant(f"WR{i}", stock=5) for i in range(10)]
        result = self._run([_petkadai_item("PK1", stock=3)])  # scrape matches none of this seller's SKUs
        for variant in variants:
            variant.refresh_from_db()
        self.assertEqual({v.stock for v in variants}, {5})
        self.assertIn("wrong seller", result["delist_skipped"])

    def test_complete_matching_scrape_still_zeroes_delisted_products(self):
        seen = [self._variant(f"PK{i}", stock=5) for i in range(4)]
        gone = self._variant("GONE", stock=5)
        result = self._run([_petkadai_item(v.sku, stock=5) for v in seen])
        gone.refresh_from_db()
        self.assertEqual(gone.stock, 0)
        self.assertEqual(result["delisted"], 1)
        self.assertIsNone(result["delist_skipped"])

    def test_threshold_is_inclusive(self):
        self.assertIsNone(sync_utils._delist_block_reason([], {"A", "B"}, {"A"}))
        self.assertIsNotNone(sync_utils._delist_block_reason([], {"A", "B", "C"}, {"A"}))
        self.assertIsNone(sync_utils._delist_block_reason([], set(), {"A"}))


def _himadri(url, categories, sku=None, **extra):
    record = {"sku": sku if sku is not None else url[-3:], "name": f"Product {url[-3:]}", "url": url,
              "categories": categories, "sale_price": 100, "regular_price": 100, "stock_status": "5 in stock"}
    record.update(extra)
    return record


def _api_product(**overrides):
    product = {
        "name": "Anubias &#8211; Nana", "sku": "#POTA93", "slug": "anubias-nana",
        "permalink": "https://himadriaquatics.com/products/anubias-nana/",
        "is_in_stock": True, "add_to_cart": {"maximum": 4},
        "prices": {"price": "20000", "regular_price": "25000", "sale_price": "20000", "currency_minor_unit": 2},
        "categories": [{"name": "All Plants"}, {"name": "Anubias, Bucephalandras &amp; Lagenandras"}],
        "tags": [{"name": "Rhizome"}], "average_rating": "4.50",
        "images": [{"src": "https://img/a.jpg"}, {"src": ""}],
        "short_description": "<p>Hardy &amp; slow</p>", "description": "<p>Long <b>text</b></p>",
    }
    product.update(overrides)
    return product


class HimadriStockRuleTests(TestCase):
    def test_out_of_stock_is_zero_and_a_real_count_is_kept(self):
        self.assertEqual(sync_utils._himadri_stock({"stock_status": "Out of stock"}), 0)
        self.assertEqual(sync_utils._himadri_stock({"stock_status": "5 in stock"}), 5)
        self.assertEqual(sync_utils._himadri_stock({"stock_status": "10 In Stock"}), 10)

    def test_in_stock_without_a_usable_count_is_unknown_not_zero(self):
        for status in ("", None, "In stock", "0 in stock", "99998347 in stock", "9999 in stock"):
            self.assertIsNone(sync_utils._himadri_stock({"stock_status": status}), status)
        self.assertIsNone(sync_utils._himadri_stock({}))


class HimadriKeySkuTests(TestCase):
    def test_real_sku_wins_and_blank_skus_get_a_slug_key(self):
        self.assertEqual(sync_utils._himadri_key_sku({"sku": " POTA93 "}), "POTA93")
        self.assertEqual(sync_utils._himadri_key_sku({"sku": "", "url": "https://h.com/products/anubias-nana/"}),
                         "HIM-anubias-nana")
        self.assertEqual(sync_utils._himadri_key_sku({"slug": "java-fern", "url": "https://h.com/products/x/"}),
                         "HIM-java-fern")

    def test_no_sku_and_no_url_gives_no_key_and_long_slugs_fit_the_column(self):
        self.assertEqual(sync_utils._himadri_key_sku({}), "")
        self.assertLessEqual(len(sync_utils._himadri_key_sku({"slug": "x" * 300})), 100)

    def test_extractor_uses_the_key_and_the_stock_rules(self):
        parsed = sync_utils._extract_himadri(
            {"name": "Java Fern", "url": "https://h.com/products/java-fern/", "sale_price": 80, "regular_price": 100,
             "stock_status": ""}, Decimal("1.10"))
        self.assertEqual(parsed["sku"], "HIM-java-fern")
        self.assertEqual(parsed["new_base"], Decimal("88"))
        self.assertIsNone(parsed["new_stock"])
        self.assertIsNone(sync_utils._extract_himadri({"name": "x", "sale_price": 5}, Decimal("1")))


class HimadriApiTests(TestCase):
    def test_to_record_matches_the_page_scraper_shape(self):
        from analytics import himadri_api
        record = himadri_api.to_record(_api_product())
        self.assertEqual(record["name"], "Anubias – Nana")
        self.assertEqual(record["sku"], "POTA93")
        self.assertEqual((record["regular_price"], record["sale_price"]), (250.0, 200.0))
        self.assertEqual((record["stock"], record["stock_status"]), (4, "4 in stock"))
        self.assertEqual(record["categories"], ["All Plants", "Anubias, Bucephalandras & Lagenandras"])
        self.assertEqual(record["tags"], ["Rhizome"])
        self.assertEqual(record["rating"], 4.5)
        self.assertEqual(record["images"], ["https://img/a.jpg"])
        self.assertEqual((record["short_description"], record["description"]), ("Hardy & slow", "Long text"))

    def test_stock_states(self):
        from analytics import himadri_api
        out = himadri_api.to_record(_api_product(is_in_stock=False))
        untracked = himadri_api.to_record(_api_product(add_to_cart={"maximum": 9999}))
        no_limit = himadri_api.to_record(_api_product(add_to_cart={}))
        self.assertEqual((out["stock"], out["stock_status"]), (0, "Out of stock"))
        self.assertEqual((untracked["stock"], untracked["stock_status"]), (None, ""))
        self.assertIsNone(no_limit["stock"])

    def test_price_without_a_sale_has_no_sale_price_and_blank_sku_stays_blank(self):
        from analytics import himadri_api
        record = himadri_api.to_record(_api_product(
            sku="", prices={"price": "9900", "regular_price": "9900", "sale_price": "9900", "currency_minor_unit": 2}))
        self.assertEqual((record["regular_price"], record["sale_price"], record["sku"]), (99.0, None, ""))

    def _response(self, products, total, pages):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = products
        response.headers = {"X-WP-Total": str(total), "X-WP-TotalPages": str(pages)}
        return response

    @mock.patch("analytics.himadri_api.requests.Session")
    def test_fetch_reads_every_page(self, session_class):
        from analytics import himadri_api
        session_class.return_value.get.side_effect = [
            self._response([{"id": 1}, {"id": 2}], 3, 2), self._response([{"id": 3}], 3, 2)]
        self.assertEqual([p["id"] for p in himadri_api.fetch_all_products()], [1, 2, 3])

    @mock.patch("analytics.himadri_api.requests.Session")
    def test_fetch_refuses_a_partial_catalogue(self, session_class):
        from analytics import himadri_api
        session_class.return_value.get.return_value = self._response([{"id": 1}], 5, 1)
        with self.assertRaisesMessage(himadri_api.HimadriApiError, "1 of 5"):
            himadri_api.fetch_all_products()

    @mock.patch("analytics.himadri_api.time.sleep")
    @mock.patch("analytics.himadri_api.requests.Session")
    def test_fetch_retries_then_gives_up(self, session_class, _sleep):
        import requests
        from analytics import himadri_api
        session_class.return_value.get.side_effect = requests.Timeout()
        with self.assertRaises(himadri_api.HimadriApiError):
            himadri_api.fetch_all_products()
        self.assertEqual(session_class.return_value.get.call_count, himadri_api.MAX_ATTEMPTS)

    @mock.patch("analytics.himadri_api.requests.Session")
    def test_fetch_rejects_an_unexpected_or_empty_response(self, session_class):
        from analytics import himadri_api
        session_class.return_value.get.return_value = self._response({"error": "x"}, 0, 1)
        with self.assertRaises(himadri_api.HimadriApiError):
            himadri_api.fetch_all_products()
        session_class.return_value.get.return_value = self._response([], 0, 1)
        with self.assertRaises(himadri_api.HimadriApiError):
            himadri_api.fetch_all_products()


class HimadriScrapeSourceTests(TestCase):
    """scrape_live_himadri reads the Store API and only crawls pages when that fails."""

    @mock.patch("analytics.sync_utils._scrape_himadri_pages")
    @mock.patch("analytics.himadri_api.fetch_all_products")
    def test_uses_the_api_and_drops_fish_and_live_food(self, fetch, crawl):
        fetch.return_value = [
            _api_product(),
            _api_product(slug="guppy", sku="G1", categories=[{"name": "Fishes"}, {"name": "Guppies"}]),
            _api_product(slug="live", sku="L1", categories=[{"name": "Fish foods"}, {"name": "live foods"}]),
            _api_product(slug="flakes", sku="F1", categories=[{"name": "Fish foods"},
                                                              {"name": "Dry foods (pellets and flakes)"}]),
        ]
        result = sync_utils.scrape_live_himadri()
        self.assertEqual([r["sku"] for r in result.records], ["POTA93", "F1"])
        self.assertEqual(result.incomplete, [])
        crawl.assert_not_called()

    @mock.patch("analytics.sync_utils._scrape_himadri_pages")
    @mock.patch("analytics.himadri_api.fetch_all_products")
    def test_falls_back_to_the_page_crawl_when_the_api_fails(self, fetch, crawl):
        from analytics import himadri_api
        fetch.side_effect = himadri_api.HimadriApiError("page 2: Timeout")
        crawl.return_value = sync_utils.ScrapeResult([{"sku": "X"}], ["combo"])
        messages = []
        result = sync_utils.scrape_live_himadri(progress_cb=messages.append)
        self.assertEqual(result.records, [{"sku": "X"}])
        self.assertTrue(any("API unavailable" in m for m in messages))


class HimadriPageCrawlFallbackTests(TestCase):
    """The page crawl: every non-livestock category, each product once, fish dropped, failures reported."""

    def _scrape(self, listing, pages):
        with mock.patch("analytics.sync_utils.time.sleep"), \
             mock.patch("analytics.himadri_scraper.get_all_product_urls",
                        side_effect=lambda cat_url: listing(cat_url)), \
             mock.patch("analytics.himadri_scraper.scrape_product", side_effect=pages):
            return sync_utils._scrape_himadri_pages()

    def test_crawls_all_plant_and_supply_categories_but_never_fish_or_live_food(self):
        urls = sync_utils._HIMADRI_CATEGORY_URLS
        joined = " ".join(urls)
        for wanted in ("/plants/", "/landscape/", "/combo/", "/alocasia/", "/pond-plants/",
                       "/aquarium-accessories/", "/fish-foods/dry-foods/", "/fish-medicines-and-water-conditioners/"):
            self.assertIn(wanted, joined)
        for unwanted in ("/fishes", "live-foods", "snails", "shrimps", "crayfish", "/fish-foods/live"):
            self.assertNotIn(unwanted, joined)
        self.assertEqual(len(urls), len(set(urls)))

    def test_products_listed_in_several_categories_are_scraped_once(self):
        fetched = []

        def page(url):
            fetched.append(url)
            return _himadri(url, ["All Plants"])

        result = self._scrape(lambda cat: ["https://h/p1", "https://h/p2"], page)
        self.assertEqual(sorted(fetched), ["https://h/p1", "https://h/p2"])
        self.assertEqual(len(result.records), 2)

    def test_fish_livestock_and_live_food_are_dropped_wherever_they_were_found(self):
        pages = {
            "https://h/plant": _himadri("https://h/plant", ["All Plants", "Aquatic Plants"]),
            "https://h/food": _himadri("https://h/food", ["Fish foods", "Dry foods (pellets and flakes)"]),
            "https://h/fish": _himadri("https://h/fish", ["Combo offers", "Fishes", "Guppies"]),
            "https://h/snail": _himadri("https://h/snail", ["Algae Eater", "Snails , shrimps and crayfish"]),
            "https://h/live": _himadri("https://h/live", ["Fish foods", "live foods"]),
        }
        result = self._scrape(lambda cat: list(pages), lambda url: pages[url])
        self.assertEqual({r["url"] for r in result.records}, {"https://h/plant", "https://h/food"})

    def test_exclusion_ignores_case_spacing_and_html_entities(self):
        for name in ("SNAILS ,  shrimps and CRAYFISH", "Siamese fighting fish (Betta splendens)", "Live Foods"):
            self.assertTrue(sync_utils.is_excluded_himadri_product({"categories": [name]}), name)
        for name in ("Fish foods", "Dry foods (pellets and flakes)", "Aquatic Plants",
                     "Medicines, Water Conditioners and Test Kits"):
            self.assertFalse(sync_utils.is_excluded_himadri_product({"categories": [name]}), name)
        self.assertFalse(sync_utils.is_excluded_himadri_product({"categories": None}))
        self.assertFalse(sync_utils.is_excluded_himadri_product({}))

    def test_failed_listing_and_failed_pages_are_reported_not_hidden(self):
        def listing(cat_url):
            if cat_url.endswith("/combo/"):
                raise RuntimeError("listing down")
            return ["https://h/ok", "https://h/bad"] if cat_url.endswith("/plants/") else []

        def page(url):
            if url.endswith("bad"):
                raise RuntimeError("bad page")
            return _himadri(url, ["All Plants"])

        result = self._scrape(listing, page)
        self.assertEqual([r["url"] for r in result.records], ["https://h/ok"])
        self.assertEqual(result.incomplete, ["combo", "1 product page(s)"])

    def test_empty_product_pages_are_skipped_without_counting_as_failures(self):
        result = self._scrape(lambda cat: ["https://h/gone"] if cat.endswith("/plants/") else [], lambda url: {})
        self.assertEqual(result.records, [])
        self.assertEqual(result.incomplete, [])


class HimadriDiffTests(SyncTestBase):
    """compute_diff for Himadri: out-of-stock rule, unknown stock, generated keys, name matching."""

    def _run(self, records, **kwargs):
        return sync_utils.compute_diff(records, "himadri", self.seller.email, Decimal("0"), None, None, **kwargs)

    def test_new_out_of_stock_products_are_not_offered_for_import(self):
        result = self._run([_himadri("https://h/products/gone", ["All Plants"], sku="G1", stock_status="Out of stock"),
                            _himadri("https://h/products/here", ["All Plants"], sku="H1", stock_status="3 in stock")])
        self.assertEqual([p["sku"] for p in result["new_products"]], ["H1"])
        self.assertEqual((result["not_found"], result["skipped_out_of_stock"]), (1, 1))

    def test_new_product_in_stock_without_a_count_is_offered_with_an_estimate(self):
        result = self._run([_himadri("https://h/products/x", ["All Plants"], sku="X1", stock_status="")])
        [product] = result["new_products"]
        self.assertEqual((product["new_stock"], product["stock_known"]), (sync_utils._UNKNOWN_COUNT_IMPORT_STOCK, False))

    def test_new_product_without_a_sku_is_keyed_by_its_url_slug(self):
        result = self._run([_himadri("https://h/products/java-fern/", ["All Plants"], sku="")])
        self.assertEqual(result["new_products"][0]["sku"], "HIM-java-fern")

    def test_existing_stock_is_left_alone_when_the_count_is_unknown(self):
        variant = self._variant("K1", stock=7)
        result = self._run([_himadri("https://h/products/k", ["All Plants"], sku="K1", stock_status="")])
        variant.refresh_from_db()
        self.assertEqual((variant.stock, result["stock_unchanged"]), (7, 1))

    def test_existing_stock_follows_a_real_count_and_out_of_stock(self):
        counted, sold_out = self._variant("C1", stock=7), self._variant("S1", stock=7)
        self._run([_himadri("https://h/products/c", ["All Plants"], sku="C1", stock_status="2 in stock"),
                   _himadri("https://h/products/s", ["All Plants"], sku="S1", stock_status="Out of stock")])
        counted.refresh_from_db()
        sold_out.refresh_from_db()
        self.assertEqual((counted.stock, sold_out.stock), (2, 0))

    def _skuless(self, name, seller=None, stock=9):
        product = Product.objects.create(name=name, seller=seller or self.seller, description="x")
        return ProductVariant.objects.create(product=product, sku=None, base_price=Decimal("100"),
                                             price=Decimal("110"), stock=stock)

    def test_a_skuless_existing_product_is_matched_by_name_instead_of_offered_again(self):
        existing = self._skuless("Java Fern (single plant)")
        record = _himadri("https://h/products/java-fern/", ["All Plants"], sku="", stock_status="4 in stock",
                          name="Java  Fern (single plant)")
        result = self._run([record])
        existing.refresh_from_db()
        self.assertEqual(existing.stock, 4)
        self.assertEqual((result["matched_by_name"], result["not_found"], result["new_products"]), (1, 0, []))

    def test_name_matching_ignores_ambiguous_names_and_other_sellers(self):
        self._skuless("Twin Plant")
        self._skuless("Twin Plant")
        self._skuless("Foreign Plant", seller=self.other_seller)
        result = self._run([_himadri("https://h/products/twin", ["All Plants"], sku="", name="Twin Plant"),
                            _himadri("https://h/products/foreign", ["All Plants"], sku="", name="Foreign Plant")])
        self.assertEqual(result["matched_by_name"], 0)
        self.assertEqual(result["not_found"], 2)

    def test_petkadai_never_matches_by_name(self):
        self._skuless("Seachem Flourish")
        result = sync_utils.compute_diff([_petkadai_item("PKX", name="Seachem Flourish", stock=3)], "petkadai",
                                         self.seller.email, None, None, None)
        self.assertEqual((result["matched_by_name"], result["not_found"]), (0, 1))

    def test_variants_are_looked_up_in_one_batch(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(25):
            self._variant(f"B{i}", stock=3)
        records = [_himadri(f"https://h/products/b{i}", ["All Plants"], sku=f"B{i}", stock_status="3 in stock")
                   for i in range(25)]
        with CaptureQueriesContext(connection) as queries:
            self._run(records)
        self.assertLess(len(queries), 15, [q["sql"][:80] for q in queries])


class HimadriCategoryPairTests(TestCase):
    """Pure mapping: which Junglyst categories a Himadri product belongs in."""

    def pairs(self, categories, name="Item"):
        return sync_utils.himadri_category_pairs(categories, name)

    def test_aquatic_plants_keep_the_original_subcategory_rules_and_always_get_aquatic_plants(self):
        anubias = self.pairs(["All Plants", "Anubias, Bucephalandras & Lagenandras"])
        self.assertEqual(set(anubias), {("Plants", "Epiphytes"), ("Plants", "Aquatic Plants")})
        low_tech = self.pairs(["All Plants", "Aquatic Plants", "Low tech-aquarium plants"])
        self.assertEqual(set(low_tech), {("Plants", "Aquatic Plants"), ("Plants", "Beginner Friendly")})
        self.assertIn(("Plants", "Tissue Culture"), self.pairs(["Aquatic Plant Tissue Culture Cups (TC Cups)"]))
        self.assertEqual(set(self.pairs(["Floating & pond plants", "Lillies and Lotus"])),
                         {("Plants", "Floating Plants"), ("Plants", "Lily"), ("Plants", "Aquatic Plants")})

    def test_a_product_tagged_only_with_a_plant_keyword_is_still_aquatic(self):
        self.assertEqual(set(self.pairs(["Cryptocorynes"])), {("Plants", "Aquatic Plants")})
        self.assertEqual(set(self.pairs(["Echinodorous/ Sword Varieties"])), {("Plants", "Aquatic Plants")})
        self.assertIn(("Plants", "Epiphytes"), self.pairs(["Anubias, Bucephalandras & Lagenandras"]))

    def test_non_aquatic_plants_go_to_indoor_or_terrarium_never_to_aquatic(self):
        self.assertEqual(self.pairs(["All Plants", "Philodendrons", "Foliage plants"]), [("Indoor Plants", None)])
        self.assertEqual(self.pairs(["Garden Ferns", "Garden Plants (outdoor plant varieties)"]), [("Indoor Plants", None)])
        self.assertEqual(self.pairs(["landscape"]), [("Indoor Plants", None)])
        self.assertEqual(self.pairs(["All Plants", "Terrarium Plants"]), [("Terrarium & Paludarium", None)])
        self.assertEqual(set(self.pairs(["Terrarium Plants", "Foliage plants"])),
                         {("Terrarium & Paludarium", None), ("Indoor Plants", None)})

    def test_an_aquatic_plant_also_tagged_foliage_is_not_filed_as_indoor_but_keeps_terrarium(self):
        pairs = self.pairs(["All Plants", "Aquatic Plants", "Foliage plants", "Terrarium Plants"])
        self.assertNotIn(("Indoor Plants", None), pairs)
        self.assertIn(("Terrarium & Paludarium", None), pairs)
        self.assertIn(("Plants", "Aquatic Plants"), pairs)

    def test_a_plant_tagged_only_all_plants_falls_back_to_aquatic_plants(self):
        self.assertEqual(self.pairs(["All Plants"]), [("Plants", "Aquatic Plants")])
        self.assertEqual(self.pairs([]), [("Plants", "Aquatic Plants")])

    def test_supplies_are_filed_by_what_the_product_is(self):
        accessories = ["Aquarium Accessories & Rocks"]
        cases = {
            "Aquarium Dragon Rock (1kg)": ("Aquascaping", "Rocks & Stones"),
            "Japanese black seiryu aquascape stone (1kg)": ("Aquascaping", "Rocks & Stones"),
            "Aquarium Walstad Clay Soil (loose-1 kg)": ("Aquascaping", "Aqua Soil"),
            "Egyptian Nile river sand (1 kg)": ("Aquascaping", "Aqua Soil"),
            "Drift wood /Aquascaping wood (large size)": ("Aquascaping", "Driftwood"),
            "Hanging filter- sebo WP607H (large)": ("Aquarium Filters", "Hang On Filters"),
            "Venus Aqua SP1000L Power Aquarium Filter": ("Aquarium Filters", "Accessories & Spares"),
            "Himadri terraplanter (Medium size)": ("Terrarium & Paludarium", "Terrarium Accessories"),
            "Mixed Sea Shells (1/2 kg)": ("Aquarium Accessories", "Tank Accessories"),
        }
        for name, expected in cases.items():
            self.assertEqual(self.pairs(accessories, name), [expected], name)

    def test_a_supply_tagged_with_a_plant_category_is_still_a_supply_but_a_plant_combo_is_a_plant(self):
        self.assertEqual(self.pairs(["accessories", "Aquarium Accessories & Rocks", "Aquatic Ferns & Mosses", "decor"],
                                    "Dried Sphagnum Moss (100 grams)"), [("Aquarium Accessories", "Tank Accessories")])
        combo = self.pairs(["accessories", "Anubias, Bucephalandras & Lagenandras",
                            "Aquarium Accessories & Rocks"], "Beginner Friendly Aquarium Combo")
        self.assertIn(("Plants", "Epiphytes"), combo)
        self.assertNotIn(("Aquarium Accessories", "Tank Accessories"), combo)

    def test_a_decor_tagged_plant_stays_a_plant(self):
        self.assertEqual(self.pairs(["All Plants", "Croton", "decor", "Foliage plants"], "Gold Dust Croton"),
                         [("Indoor Plants", None)])

    def test_fish_food_and_medicine_categories(self):
        self.assertEqual(self.pairs(["Fish foods", "Dry foods (pellets and flakes)"]),
                         [("Fish & Aquarium Care", "Fish Food")])
        medicines = ["Medicines, Water Conditioners and Test Kits"]
        self.assertEqual(self.pairs(medicines, "Paracidol-fw"), [("Fish & Aquarium Care", "Aquarium Medicines")])
        self.assertEqual(self.pairs(medicines, "Chlor-Away -Plantoz- 50ml (chlorine remover)"),
                         [("Fish & Aquarium Care", "Water Conditioners")])
        self.assertEqual(self.pairs(medicines, "pH indicator solution kit"),
                         [("Fish & Aquarium Care", "Water Conditioners")])
        self.assertEqual(self.pairs(medicines, "Eco macro fertliser Plantoz (100ml)"), [("Aquascaping", "Fertilizers")])
        self.assertEqual(self.pairs(medicines, "Combo plant nutrient kit"), [("Aquascaping", "Fertilizers")])
        self.assertEqual(self.pairs(medicines + ["accessories"], "Rooting ball- large"),
                         [("Aquarium Accessories", "Tank Accessories")])


class HimadriCategoryResolveTests(TestCase):
    def setUp(self):
        from core.models import Category, SubCategory
        for category_name, subs in {
            "Plants": ["Aquatic Plants", "Epiphytes", "Mosses", "Aquatic Moss", "Ferns"], "Indoor Plants": [],
            "Terrarium & Paludarium": ["Terrarium Accessories"],
        }.items():
            category = Category.objects.create(name=category_name, slug=category_name.lower().replace(" ", "-"))
            for sub in subs:
                SubCategory.objects.create(category=category, name=sub, slug=f"{category.slug}-{sub.lower().replace(' ', '-')}")

    def test_returns_objects_for_every_pair_and_category_only_when_there_is_no_subcategory(self):
        placements = sync_utils.resolve_himadri_categories(["All Plants", "Aquatic Ferns & Mosses", "Terrarium Plants"], "x")
        self.assertEqual({(c.name, s.name if s else None) for c, s in placements},
                         {("Plants", "Mosses"), ("Plants", "Aquatic Moss"), ("Plants", "Ferns"),
                          ("Plants", "Aquatic Plants"), ("Terrarium & Paludarium", None)})

    def test_categories_missing_from_the_database_are_skipped_not_fatal(self):
        from core.models import Category
        Category.objects.filter(name="Indoor Plants").delete()
        self.assertEqual(sync_utils.resolve_himadri_categories(["Philodendrons"], "x"), [])

    def test_a_missing_subcategory_still_files_the_product_in_its_category(self):
        from core.models import SubCategory
        SubCategory.objects.filter(name="Mosses").delete()
        placements = sync_utils.resolve_himadri_categories(["Aquatic Ferns & Mosses"], "x")
        self.assertIn(("Plants", None), {(c.name, s.name if s else None) for c, s in placements})


class HimadriImportTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from core.models import Category, SubCategory
        plants = Category.objects.create(name="Plants", slug="plants")
        SubCategory.objects.create(category=plants, name="Aquatic Plants", slug="aquatic-plants")
        SubCategory.objects.create(category=plants, name="Epiphytes", slug="plants-epiphytes")
        Category.objects.create(name="Terrarium & Paludarium", slug="terrarium-paludarium")

    def _session(self, **item):
        entry = {"sku": "HIM-java-fern", "name": "Java Fern", "new_base": 100.0, "new_stock": 5, "stock_known": False,
                 "source": "himadri", "_raw": {"categories": ["All Plants", "Anubias, Bucephalandras & Lagenandras",
                                                              "Terrarium Plants"], "images": [], "tags": []}}
        entry.update(item)
        return StockSyncSession.objects.create(source="himadri", seller_email=self.seller.email,
                                               new_products={entry["sku"]: entry})

    def test_a_himadri_product_is_filed_in_every_category_it_maps_to_under_its_generated_key(self):
        session = self._session()
        result = sync_utils.import_new_products(str(session.key), ["HIM-java-fern"], self.seller.email, Decimal("10"))
        self.assertEqual((result["imported"], result["errors"]), (1, []))
        variant = ProductVariant.objects.get(sku="HIM-java-fern")
        self.assertEqual({c.name for c in variant.product.categories.all()}, {"Plants", "Terrarium & Paludarium"})
        self.assertEqual({s.name for s in variant.product.sub_categories.all()}, {"Aquatic Plants", "Epiphytes"})
        self.assertEqual(variant.stock, 5)

    def test_importing_twice_does_not_create_a_duplicate(self):
        session = self._session()
        sync_utils.import_new_products(str(session.key), ["HIM-java-fern"], self.seller.email, None)
        again = sync_utils.import_new_products(str(session.key), ["HIM-java-fern"], self.seller.email, None)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(ProductVariant.objects.filter(sku="HIM-java-fern").count(), 1)


class SellerEmailCaseTests(SyncTestBase):
    """The account is stored as "Support@petkadai.com"; the form is often typed in lower case."""

    def setUp(self):
        super().setUp()
        self.mixed_case = User.objects.create_user(email="Support@Shop.com", username="shop", password="Pass@12345",
                                                   role="grower")

    def test_find_seller_ignores_case_and_whitespace(self):
        self.assertEqual(sync_utils.find_seller_by_email("support@shop.com"), self.mixed_case)
        self.assertEqual(sync_utils.find_seller_by_email("  SUPPORT@SHOP.COM "), self.mixed_case)

    def test_find_seller_returns_none_for_unknown_or_blank(self):
        self.assertIsNone(sync_utils.find_seller_by_email("nobody@shop.com"))
        self.assertIsNone(sync_utils.find_seller_by_email(""))
        self.assertIsNone(sync_utils.find_seller_by_email(None))

    def test_delisting_scope_matches_seller_regardless_of_case(self):
        seen = self._variant("PK1", seller=self.mixed_case, stock=5)
        gone = self._variant("GONE", seller=self.mixed_case, stock=5)
        result = sync_utils.compute_diff([_petkadai_item("PK1", stock=5)], "petkadai", "support@shop.com",
                                         None, None, None)
        seen.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual((seen.stock, gone.stock), (5, 0))
        self.assertEqual(result["delisted"], 1)

    def test_import_finds_seller_by_lowercase_email(self):
        session = StockSyncSession.objects.create(source="petkadai", seller_email="support@shop.com")
        result = sync_utils.import_new_products(str(session.key), [], "support@shop.com", None)
        self.assertNotIn("Seller not found", " ".join(result["errors"]))

    def test_import_reports_unknown_seller(self):
        session = StockSyncSession.objects.create(source="petkadai", seller_email="x@y.com")
        result = sync_utils.import_new_products(str(session.key), [], "x@y.com", None)
        self.assertEqual(result["errors"], ["Seller not found: x@y.com"])

    @mock.patch("core.tasks.scrape_and_sync.delay")
    def test_preview_rejects_unknown_seller_before_scraping(self, delay):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(User.objects.create_superuser(email="root@test.com", username="root",
                                                                password="Pass@12345"))
        response = client.post("/api/analytics/super-admin/stock-sync/preview/",
                               {"source": "petkadai", "seller_email": "nobody@shop.com"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No seller account found", response.data["error"])
        delay.assert_not_called()

    @mock.patch("core.tasks.scrape_and_sync.delay")
    def test_preview_accepts_lowercase_email_for_mixed_case_account(self, delay):
        from rest_framework.test import APIClient
        delay.return_value = mock.Mock(id="job-1")
        client = APIClient()
        client.force_authenticate(User.objects.create_superuser(email="root@test.com", username="root",
                                                                password="Pass@12345"))
        response = client.post("/api/analytics/super-admin/stock-sync/preview/",
                               {"source": "petkadai", "seller_email": "support@shop.com"}, format="json")
        self.assertEqual(response.status_code, 202)
        delay.assert_called_once()


class SyncEndpointPermissionTests(SyncTestBase):
    SYNC_URLS = [
        ("get", "/api/analytics/super-admin/stock-sync/status/"),
        ("post", "/api/analytics/super-admin/stock-sync/preview/"),
        ("get", "/api/analytics/super-admin/stock-sync/job/abc/"),
        ("post", "/api/analytics/super-admin/stock-sync/apply/"),
        ("post", "/api/analytics/super-admin/stock-sync/import/"),
    ]

    def test_anonymous_sellers_and_plain_staff_are_refused(self):
        from rest_framework.test import APIClient
        staff_user = User.objects.create_user(email="staff@test.com", username="staff", password="Pass@12345",
                                              is_staff=True)
        anonymous, seller, staff = APIClient(), APIClient(), APIClient()
        seller.force_authenticate(self.seller)
        staff.force_authenticate(staff_user)
        for method, url in self.SYNC_URLS:
            self.assertIn(getattr(anonymous, method)(url).status_code, (401, 403), url)
            self.assertEqual(getattr(seller, method)(url).status_code, 403, url)
            self.assertEqual(getattr(staff, method)(url).status_code, 403, url)

    @mock.patch("core.tasks.scrape_and_sync.delay")
    def test_superuser_can_start_a_sync(self, delay):
        from rest_framework.test import APIClient
        delay.return_value = mock.Mock(id="job-1")
        admin = User.objects.create_superuser(email="admin@test.com", username="admin", password="Pass@12345")
        client = APIClient()
        client.force_authenticate(admin)
        response = client.post("/api/analytics/super-admin/stock-sync/preview/",
                               {"source": "himadri", "seller_email": self.seller.email}, format="json")
        self.assertLess(response.status_code, 300, response.content)
        delay.assert_called_once()


class ScrapeAndSyncTaskTests(SyncTestBase):
    @mock.patch("analytics.sync_utils.scrape_live_himadri", return_value=sync_utils.ScrapeResult([]))
    @mock.patch("analytics.sync_utils.scrape_live_petkadai", return_value=sync_utils.ScrapeResult([]))
    def test_task_dispatches_to_the_requested_source(self, petkadai, himadri):
        from core.tasks import scrape_and_sync
        with mock.patch.object(scrape_and_sync, "update_state"):
            result = scrape_and_sync.apply(args=("himadri", self.seller.email, None, None, None)).get()
        himadri.assert_called_once()
        petkadai.assert_not_called()
        self.assertEqual(result["source"], "himadri")
        self.assertIn("last_synced", result)
