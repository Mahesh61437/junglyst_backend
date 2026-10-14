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
            {"sku": "H1", "name": "Anubias", "sale_price": 80, "regular_price": 100, "stock": "4 in stock"},
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


class HimadriScrapeTests(TestCase):
    @mock.patch("analytics.sync_utils.time.sleep")
    @mock.patch("analytics.himadri_scraper.scrape_product")
    @mock.patch("analytics.himadri_scraper.get_all_product_urls")
    def test_uses_bundled_scraper_and_skips_duplicate_urls(self, get_urls, scrape_product, _sleep):
        get_urls.side_effect = [["https://h/p1", "https://h/p2"], ["https://h/p2", "https://h/p3"]]
        scrape_product.side_effect = lambda url: {"sku": url[-2:], "url": url}
        result = sync_utils.scrape_live_himadri()
        self.assertEqual([r["url"] for r in result.records], ["https://h/p1", "https://h/p2", "https://h/p3"])
        self.assertEqual(result.incomplete, [])

    @mock.patch("analytics.sync_utils.time.sleep")
    @mock.patch("analytics.himadri_scraper.scrape_product", side_effect=RuntimeError("bad page"))
    @mock.patch("analytics.himadri_scraper.get_all_product_urls", return_value=["https://h/p1"])
    def test_a_failing_product_page_is_skipped_and_reported(self, _urls, _scrape, _sleep):
        result = sync_utils.scrape_live_himadri()
        self.assertEqual(result.records, [])
        self.assertEqual(result.incomplete, ["1 product page(s)"])


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
