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



import uuid

from django.core.cache import cache

from core.feed import FEED_CACHE_KEY, get_ordered_product_ids


class StaleFeedCacheTests(APITestCase):
    """The cached feed must rebuild when it doesn't match this database."""

    def setUp(self):
        cache.clear()
        self.seller = User.objects.create_user(
            email="feed_seller@example.com", username="feed_seller", password="Password123", role="grower"
        )
        self.products = []
        for i in range(3):
            p = Product.objects.create(
                name=f"Feed Plant {i}", description="d", seller=self.seller, is_active=True, is_draft=False
            )
            ProductVariant.objects.create(product=p, name="Standard", stock=5, price=100.0)
            self.products.append(p)
        self.live_ids = {p.id for p in self.products}

    def tearDown(self):
        cache.clear()

    def test_feed_from_another_database_is_rebuilt(self):
        cache.set(FEED_CACHE_KEY, [str(uuid.uuid4()) for _ in range(5)], 3600)
        self.assertEqual(set(get_ordered_product_ids()), self.live_ids)

    def test_empty_cached_feed_is_rebuilt_when_products_exist(self):
        cache.set(FEED_CACHE_KEY, [], 3600)
        self.assertEqual(set(get_ordered_product_ids()), self.live_ids)

    def test_valid_cached_feed_is_kept(self):
        cached = [str(p.id) for p in reversed(self.products)]
        cache.set(FEED_CACHE_KEY, cached, 3600)
        self.assertEqual([str(i) for i in get_ordered_product_ids()], cached)

    def test_home_and_shop_recover_from_stale_feed(self):
        cache.set(FEED_CACHE_KEY, [str(uuid.uuid4()) for _ in range(5)], 3600)
        home = self.client.get(reverse('home_data'))
        self.assertEqual(len(home.data['products']), 3)
        shop = self.client.get(reverse('product_list'))
        self.assertEqual(shop.data['count'], 3)


from core.views import HomeDataView


class ProductStripSizeTests(APITestCase):
    """Home shows HOME_PRODUCT_LIMIT products; the shop honours page_size=24."""

    SHOP_PAGE_SIZE = 24  # mirrors SHOP_PAGE_SIZE in junglyst_ui/src/pages/Shop.jsx

    def setUp(self):
        cache.clear()
        # One product per seller so seller-fair ordering can't hide any of them
        for i in range(30):
            seller = User.objects.create_user(
                email=f"strip_seller_{i}@example.com", username=f"strip_seller_{i}",
                password="Password123", role="grower",
            )
            product = Product.objects.create(
                name=f"Strip Plant {i}", description="d", seller=seller, is_active=True, is_draft=False
            )
            ProductVariant.objects.create(product=product, name="Standard", stock=5, price=100.0)

    def tearDown(self):
        cache.clear()

    def test_home_returns_up_to_limit_products(self):
        response = self.client.get(reverse('home_data'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(HomeDataView.HOME_PRODUCT_LIMIT, 24)
        self.assertEqual(len(response.data['products']), HomeDataView.HOME_PRODUCT_LIMIT)

    def test_home_returns_all_when_fewer_than_limit(self):
        Product.objects.filter(name__in=[f"Strip Plant {i}" for i in range(20)]).update(is_active=False)
        cache.clear()
        response = self.client.get(reverse('home_data'))
        self.assertEqual(len(response.data['products']), 10)

    def test_shop_page_size_24_paginates(self):
        first = self.client.get(reverse('product_list'), {'page': 1, 'page_size': self.SHOP_PAGE_SIZE})
        second = self.client.get(reverse('product_list'), {'page': 2, 'page_size': self.SHOP_PAGE_SIZE})
        self.assertEqual(first.data['count'], 30)
        self.assertEqual(len(first.data['results']), self.SHOP_PAGE_SIZE)
        self.assertEqual(len(second.data['results']), 30 - self.SHOP_PAGE_SIZE)
        first_ids = {p['id'] for p in first.data['results']}
        second_ids = {p['id'] for p in second.data['results']}
        self.assertFalse(first_ids & second_ids)
