from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model

User = get_user_model()

class AuthTests(APITestCase):
    def setUp(self):
        self.register_url = reverse('auth_register')
        self.login_url = reverse('token_obtain_pair')
        self.user_data = {
            "email": "testuser@example.com",
            "username": "testuser",
            "password": "Password123",
            "role": "collector"
        }
        self.user = User.objects.create_user(**self.user_data)

    def test_registration(self):
        url = self.register_url
        data = {
            "email": "newuser@example.com",
            "username": "newuser",
            "password": "Password123",
            "role": "collector"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.filter(email="newuser@example.com").count(), 1)

    def test_login_with_email(self):
        url = self.login_url
        data = {
            "email": "testuser@example.com",
            "password": "Password123"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('user', response.data)

    def test_login_with_username(self):
        url = self.login_url
        data = {
            "email": "testuser", # Using the email field name but providing username value
            "password": "Password123"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

    def test_login_failed(self):
        url = self.login_url
        data = {
            "email": "testuser@example.com",
            "password": "WrongPassword"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


from core.models import Product, ProductVariant

class ProductFairnessTests(APITestCase):
    def setUp(self):
        self.products_url = reverse('product_list')
        self.home_url = reverse('home_data')
        
        # Create 3 sellers
        self.seller_a = User.objects.create_user(
            email="seller_a@example.com", username="seller_a", password="Password123", role="grower"
        )
        self.seller_b = User.objects.create_user(
            email="seller_b@example.com", username="seller_b", password="Password123", role="grower"
        )
        self.seller_c = User.objects.create_user(
            email="seller_c@example.com", username="seller_c", password="Password123", role="grower"
        )

        # Create products for each seller
        # Seller A: 5 products (all active, non-draft)
        self.products_a = []
        for i in range(5):
            p = Product.objects.create(
                name=f"Plant A{i}", description=f"Desc A{i}", seller=self.seller_a, is_active=True, is_draft=False
            )
            # 3 in stock, 2 out of stock
            stock = 10 if i < 3 else 0
            ProductVariant.objects.create(product=p, name="Standard", stock=stock, price=100.0)
            self.products_a.append(p)

        # Seller B: 5 products (all active, non-draft)
        self.products_b = []
        for i in range(5):
            p = Product.objects.create(
                name=f"Plant B{i}", description=f"Desc B{i}", seller=self.seller_b, is_active=True, is_draft=False
            )
            # 3 in stock, 2 out of stock
            stock = 10 if i < 3 else 0
            ProductVariant.objects.create(product=p, name="Standard", stock=stock, price=100.0)
            self.products_b.append(p)

        # Seller C: 5 products (all active, non-draft)
        self.products_c = []
        for i in range(5):
            p = Product.objects.create(
                name=f"Plant C{i}", description=f"Desc C{i}", seller=self.seller_c, is_active=True, is_draft=False
            )
            # 3 in stock, 2 out of stock
            stock = 10 if i < 3 else 0
            ProductVariant.objects.create(product=p, name="Standard", stock=stock, price=100.0)
            self.products_c.append(p)

    def test_fairness_engine_caps_seller_count(self):
        # The default MAX_PER_SELLER is 2.
        # We fetch the product feed.
        response = self.client.get(self.products_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Since we have pagination (page size 20), but we only generated 15 products total,
        # all allowed products should be on page 1.
        results = response.data.get('results', [])
        
        # Verify counts per seller
        seller_counts = {}
        for p_data in results:
            s_id = p_data.get('seller', {}).get('id')
            seller_counts[s_id] = seller_counts.get(s_id, 0) + 1
            
        # Every seller should have at most 2 products returned in the feed.
        for s_id, count in seller_counts.items():
            self.assertTrue(count <= 2, f"Seller {s_id} has {count} products (limit is 2)")

    def test_in_stock_first_with_fairness(self):
        response = self.client.get(self.products_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        results = response.data.get('results', [])
        
        # Out-of-stock products should not be sorted before in-stock ones.
        # Let's verify that once we see an out-of-stock product, no subsequent product is in-stock.
        seen_out_of_stock = False
        for p_data in results:
            # Check if any variant has stock
            has_stock = any(v.get('stock', 0) > 0 for v in p_data.get('variants', []))
            if not has_stock:
                seen_out_of_stock = True
            elif seen_out_of_stock:
                self.fail("Found an in-stock product after an out-of-stock product in the feed")

    def test_bypass_fairness_engine_when_ordering(self):
        # Request ordering by created_at. This should bypass the fairness limit.
        response = self.client.get(f"{self.products_url}?ordering=created_at")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        results = response.data.get('results', [])
        
        # Without fairness limits, we should get up to 15 products (5 per seller).
        self.assertEqual(len(results), 15)

    def test_bypass_fairness_engine_when_seller_filtered(self):
        # Filter by a specific seller. This should bypass the fairness engine and return all their products.
        response = self.client.get(f"{self.products_url}?seller={self.seller_a.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        results = response.data.get('results', [])
        self.assertEqual(len(results), 5) # All 5 products of Seller A should be returned.

    def test_home_page_fairness_engine(self):
        # Verify the home page aggregate endpoint also applies the fairness engine
        response = self.client.get(self.home_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        products = response.data.get('products', [])
        
        seller_counts = {}
        for p_data in products:
            s_id = p_data.get('seller', {}).get('id')
            seller_counts[s_id] = seller_counts.get(s_id, 0) + 1
            
        for s_id, count in seller_counts.items():
            self.assertTrue(count <= 2, f"Seller {s_id} has {count} products on homepage (limit is 2)")


class SellerStorePaginationTests(APITestCase):
    """The seller storefront filters products by ?seller_slug= and relies on
    server-side pagination + search. Regression guard: a seller with more than
    one page of products must expose every product across pages, not just the
    first page_size (20)."""

    def setUp(self):
        from sellers.models import SellerProfile
        self.products_url = reverse('product_list')
        self.seller = User.objects.create_user(
            email="store@example.com", username="store_seller", password="Password123", role="grower"
        )
        self.profile = SellerProfile.objects.create(
            user=self.seller, store_name="Big Botanicals", slug="big-botanicals"
        )
        # 25 products → spans two pages (page_size=20). One uniquely-named
        # product lets us assert search narrows the result set.
        for i in range(25):
            name = "Rare Monstera Albo" if i == 0 else f"Pothos {i}"
            p = Product.objects.create(
                name=name, description=f"Desc {i}", seller=self.seller,
                is_active=True, is_draft=False,
            )
            ProductVariant.objects.create(product=p, name="Standard", stock=10, price=100.0)

    def test_first_page_capped_at_page_size_with_full_count(self):
        response = self.client.get(f"{self.products_url}?seller_slug=big-botanicals")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('count'), 25)
        self.assertEqual(len(response.data.get('results', [])), 20)
        self.assertIsNotNone(response.data.get('next'))

    def test_second_page_returns_remaining_products(self):
        response = self.client.get(f"{self.products_url}?seller_slug=big-botanicals&page=2")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data.get('results', [])), 5)
        self.assertIsNone(response.data.get('next'))

    def test_search_narrows_seller_store_results(self):
        response = self.client.get(
            f"{self.products_url}?seller_slug=big-botanicals&search=Monstera"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('count'), 1)
        results = response.data.get('results', [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], "Rare Monstera Albo")

    def test_price_sort_within_seller_store(self):
        # ?ordering=price must sort the seller's storefront by price ascending
        # (in-stock products are all stocked here, so price drives the order).
        response = self.client.get(
            f"{self.products_url}?seller_slug=big-botanicals&ordering=price&page_size=100"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prices = [float(p['price']) for p in response.data.get('results', []) if p.get('price') is not None]
        self.assertTrue(prices, "Expected priced products in the response")
        self.assertEqual(prices, sorted(prices))

    def test_filters_only_match_this_seller(self):
        # A second seller with a matching product name must never leak into the
        # first seller's filtered/searched storefront results.
        from sellers.models import SellerProfile
        other = User.objects.create_user(
            email="other@example.com", username="other_seller", password="Password123", role="grower"
        )
        SellerProfile.objects.create(user=other, store_name="Other Store", slug="other-store")
        p = Product.objects.create(
            name="Rare Monstera Albo", description="d", seller=other,
            is_active=True, is_draft=False,
        )
        ProductVariant.objects.create(product=p, name="Standard", stock=10, price=100.0)

        response = self.client.get(
            f"{self.products_url}?seller_slug=big-botanicals&search=Monstera"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Only the first seller's single Monstera — not the other store's.
        self.assertEqual(response.data.get('count'), 1)
        sellers = {str(p['seller']['id']) for p in response.data.get('results', [])}
        self.assertEqual(sellers, {str(self.seller.id)})



class PostHogAPIMetricsMiddlewareTests(TestCase):
    """The metrics middleware must (a) stay silent when PostHog is unconfigured,
    (b) capture an api_request event for /api/ traffic when enabled, and
    (c) always capture errors regardless of sampling."""

    def _build(self, response_status=200, exc=None):
        from core.middleware import PostHogAPIMetricsMiddleware

        def get_response(request):
            if exc:
                raise exc
            from django.http import HttpResponse
            r = HttpResponse(status=response_status)
            return r
        return PostHogAPIMetricsMiddleware(get_response)

    def test_noop_when_disabled(self):
        from django.test import RequestFactory
        from unittest import mock
        from django.test import override_settings
        with override_settings(POSTHOG_API_KEY=''):
            mw = self._build()
            with mock.patch('core.middleware.get_posthog') as gp:
                mw(RequestFactory().get('/api/core/products/'))
                gp.assert_not_called()

    def test_captures_api_request_when_enabled(self):
        from django.test import RequestFactory, override_settings
        from unittest import mock
        with override_settings(POSTHOG_API_KEY='phc_test'):
            mw = self._build(response_status=200)
            client = mock.Mock()
            with mock.patch('core.middleware.get_posthog', return_value=client):
                mw(RequestFactory().get('/api/core/products/'))
            self.assertTrue(client.capture.called)
            _, kwargs = client.capture.call_args
            self.assertEqual(kwargs['event'], 'api_request')
            self.assertEqual(kwargs['properties']['status_code'], 200)
            self.assertTrue(kwargs['properties']['success'])

    def test_skips_non_api_paths(self):
        from django.test import RequestFactory, override_settings
        from unittest import mock
        with override_settings(POSTHOG_API_KEY='phc_test'):
            mw = self._build()
            client = mock.Mock()
            with mock.patch('core.middleware.get_posthog', return_value=client):
                mw(RequestFactory().get('/static/app.css'))
            client.capture.assert_not_called()

    def test_error_always_captured_even_when_fully_sampled_out(self):
        from django.test import RequestFactory, override_settings
        from unittest import mock
        # sample_rate=0 would drop all *successful* requests, but errors must survive.
        with override_settings(POSTHOG_API_KEY='phc_test', POSTHOG_API_SAMPLE_RATE=0.0):
            mw = self._build(exc=ValueError("boom"))
            client = mock.Mock()
            with mock.patch('core.middleware.get_posthog', return_value=client):
                with self.assertRaises(ValueError):
                    mw(RequestFactory().get('/api/core/products/'))
            self.assertTrue(client.capture.called)
            _, kwargs = client.capture.call_args
            self.assertEqual(kwargs['properties']['status_code'], 500)
            self.assertFalse(kwargs['properties']['success'])
            self.assertEqual(kwargs['properties']['error_type'], 'ValueError')
