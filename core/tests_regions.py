from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from sellers.models import SellerProfile, SellerExcludedState
from shipping.regions import REGION_NORTH, REGION_NORTH_EAST, REGION_SOUTH
from .models import Product, ProductVariant

User = get_user_model()


def _seller_with_product(slug, **region_flags):
    user = User.objects.create_user(email=f'{slug}@test.com', username=slug, password='Pass@12345', role='grower')
    SellerProfile.objects.create(user=user, store_name=slug.title(), slug=slug, **region_flags)
    product = Product.objects.create(name=f'{slug} plant', seller=user, is_active=True, is_draft=False)
    ProductVariant.objects.create(
        product=product, name='Standard', base_price=Decimal('100'), price=Decimal('120'), stock=5,
    )
    return user, product


class RegionProductFeedTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        _, self.all_india = _seller_with_product('all-india')
        _, self.south_only = _seller_with_product('south-only', ships_to_north=False, ships_to_north_east=False)
        _, self.north_east_only = _seller_with_product('ne-only', ships_to_south=False, ships_to_north=False)

    def tearDown(self):
        cache.clear()

    def _listed_ids(self, **params):
        response = self.client.get('/api/core/products/', params)
        self.assertEqual(response.status_code, 200)
        return {item['id'] for item in response.data['results']}

    def test_without_region_lists_every_seller(self):
        self.assertEqual(
            self._listed_ids(),
            {str(self.all_india.id), str(self.south_only.id), str(self.north_east_only.id)},
        )

    def test_region_keeps_only_sellers_shipping_there(self):
        self.assertEqual(self._listed_ids(region=REGION_SOUTH), {str(self.all_india.id), str(self.south_only.id)})
        self.assertEqual(self._listed_ids(region=REGION_NORTH), {str(self.all_india.id)})
        self.assertEqual(
            self._listed_ids(region=REGION_NORTH_EAST),
            {str(self.all_india.id), str(self.north_east_only.id)},
        )

    def test_region_combines_with_sorting(self):
        self.assertEqual(
            self._listed_ids(region=REGION_NORTH, ordering='-created_at'),
            {str(self.all_india.id)},
        )

    def test_unknown_region_is_ignored(self):
        self.assertEqual(len(self._listed_ids(region='atlantis')), 3)

    def test_home_products_follow_region(self):
        response = self.client.get('/api/core/home/', {'region': REGION_NORTH})
        self.assertEqual({p['id'] for p in response.data['products']}, {str(self.all_india.id)})

        response = self.client.get('/api/core/home/')
        self.assertEqual(len(response.data['products']), 3)

    def _exclude(self, slug, state_code):
        SellerExcludedState.objects.create(seller=SellerProfile.objects.get(slug=slug), state_code=state_code)

    def test_state_drops_sellers_who_excluded_it(self):
        self._exclude('south-only', 'KL')

        self.assertEqual(self._listed_ids(state='KL'), {str(self.all_india.id)})
        self.assertEqual(self._listed_ids(state='TN'), {str(self.all_india.id), str(self.south_only.id)})

    def test_state_implies_its_region(self):
        # Kerala is in the south even if a stale region param says otherwise
        self.assertEqual(
            self._listed_ids(state='KL', region=REGION_NORTH),
            {str(self.all_india.id), str(self.south_only.id)},
        )

    def test_state_code_is_case_insensitive_and_unknown_is_ignored(self):
        self._exclude('south-only', 'KL')
        self.assertEqual(self._listed_ids(state='kl'), {str(self.all_india.id)})
        self.assertEqual(len(self._listed_ids(state='ZZ')), 3)

    def test_home_products_follow_state_exclusions(self):
        self._exclude('all-india', 'AS')

        response = self.client.get('/api/core/home/', {'state': 'AS'})

        self.assertEqual({p['id'] for p in response.data['products']}, {str(self.north_east_only.id)})

    def test_excluding_a_state_refreshes_cached_listing(self):
        self.assertEqual(len(self._listed_ids(state='KL')), 2)

        self._exclude('south-only', 'KL')

        self.assertEqual(self._listed_ids(state='KL'), {str(self.all_india.id)})

    def test_product_detail_exposes_excluded_states(self):
        self._exclude('south-only', 'KL')
        response = self.client.get(f'/api/core/products/id/{self.south_only.id}/')
        self.assertEqual(response.data['seller']['seller_profile']['excluded_states'], ['KL'])

    def test_region_change_refreshes_cached_listing(self):
        self.assertEqual(self._listed_ids(region=REGION_NORTH), {str(self.all_india.id)})

        profile = SellerProfile.objects.get(slug='south-only')
        profile.ships_to_north = True
        profile.save()

        self.assertEqual(
            self._listed_ids(region=REGION_NORTH),
            {str(self.all_india.id), str(self.south_only.id)},
        )

    def test_unrelated_profile_save_keeps_cache(self):
        cache.set('sentinel', 1)
        profile = SellerProfile.objects.get(slug='south-only')
        profile.bio = 'Updated bio'
        profile.save()

        self.assertEqual(cache.get('sentinel'), 1)

    def test_product_detail_exposes_service_regions(self):
        response = self.client.get(f'/api/core/products/id/{self.south_only.id}/')
        self.assertEqual(response.data['seller']['seller_profile']['service_regions'], [REGION_SOUTH])


class CartShippingConfigRegionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.seller, _ = _seller_with_product('fee-seller', ships_to_north_east=False)
        SellerProfile.objects.filter(user=self.seller).update(delivery_fee_south=Decimal('45.00'))
        SellerExcludedState.objects.create(seller=self.seller.seller_profile, state_code='KL')

    def test_includes_service_regions_and_region_fees(self):
        response = self.client.get('/api/cart/shipping-configs/', {'seller_ids': str(self.seller.id)})

        self.assertEqual(response.status_code, 200)
        delivery = response.data[str(self.seller.id)]['delivery']
        self.assertEqual(delivery['service_regions'], [REGION_SOUTH, REGION_NORTH])
        self.assertEqual(delivery['excluded_states'], ['KL'])
        self.assertEqual(delivery['region_fees'], {REGION_SOUTH: 45.0, REGION_NORTH: None, REGION_NORTH_EAST: None})

    def test_ignores_malformed_seller_ids(self):
        response = self.client.get('/api/cart/shipping-configs/', {'seller_ids': f'not-a-uuid,{self.seller.id}'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), [str(self.seller.id)])

    def test_only_malformed_ids_returns_empty(self):
        response = self.client.get('/api/cart/shipping-configs/', {'seller_ids': 'x,y'})
        self.assertEqual(response.data, {})


class RegionFeedCombinationTests(TestCase):
    """Region/state filters combined with the shop's other filters, sorting and paging."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        from .models import Category
        self.plants = Category.objects.create(name='Aquatic Plants', shipping_type='plant')
        _, self.south_plant = _seller_with_product('south-a', ships_to_north=False, ships_to_north_east=False)
        _, self.everywhere_plant = _seller_with_product('everywhere-b')
        self.south_plant.categories.add(self.plants)
        self.everywhere_plant.categories.add(self.plants)
        _, self.north_rock = _seller_with_product('north-c', ships_to_south=False, ships_to_north_east=False)

    def tearDown(self):
        cache.clear()

    def _ids(self, **params):
        response = self.client.get('/api/core/products/', params)
        self.assertEqual(response.status_code, 200)
        return [item['id'] for item in response.data['results']], response.data['count']

    def test_state_and_category_filters_combine(self):
        ids, count = self._ids(state='TN', category='Aquatic Plants')
        self.assertEqual(set(ids), {str(self.south_plant.id), str(self.everywhere_plant.id)})
        self.assertEqual(count, 2)

        ids, _ = self._ids(state='HP', category='Aquatic Plants')
        self.assertEqual(ids, [str(self.everywhere_plant.id)])

    def test_state_with_price_sorting_keeps_only_deliverable(self):
        ids, count = self._ids(state='HP', ordering='price')
        self.assertEqual(set(ids), {str(self.everywhere_plant.id), str(self.north_rock.id)})
        self.assertEqual(count, 2)

    def test_count_and_pages_reflect_the_filtered_set(self):
        ids, count = self._ids(state='TN', page_size=1)
        self.assertEqual(count, 2)
        self.assertEqual(len(ids), 1)

    def test_home_caches_each_state_separately(self):
        SellerExcludedState.objects.create(seller=SellerProfile.objects.get(slug='everywhere-b'), state_code='KL')

        kerala = {p['id'] for p in self.client.get('/api/core/home/', {'state': 'KL'}).data['products']}
        tamil_nadu = {p['id'] for p in self.client.get('/api/core/home/', {'state': 'TN'}).data['products']}

        self.assertEqual(kerala, {str(self.south_plant.id)})
        self.assertEqual(tamil_nadu, {str(self.south_plant.id), str(self.everywhere_plant.id)})


class ExcludedStateCacheSignalTests(TestCase):
    """Admin inline edits go through the ORM, not the seller API — they must still refresh feeds."""

    def setUp(self):
        _, self.product = _seller_with_product('signal-seller')
        self.profile = SellerProfile.objects.get(slug='signal-seller')

    def test_adding_and_removing_an_exclusion_clears_cache(self):
        cache.set('sentinel', 1)
        exclusion = SellerExcludedState.objects.create(seller=self.profile, state_code='KL')
        self.assertIsNone(cache.get('sentinel'))

        cache.set('sentinel', 1)
        exclusion.delete()
        self.assertIsNone(cache.get('sentinel'))


class CartShippingConfigQueryTests(TestCase):
    def test_query_count_does_not_grow_with_sellers(self):
        sellers = [_seller_with_product(f'qc-{i}')[0] for i in range(3)]
        for seller in sellers:
            SellerExcludedState.objects.create(seller=seller.seller_profile, state_code='KL')
        client = APIClient()

        def query_count(seller_ids):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            with CaptureQueriesContext(connection) as ctx:
                response = client.get('/api/cart/shipping-configs/', {'seller_ids': ','.join(seller_ids)})
            self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        one = query_count([str(sellers[0].id)])
        three = query_count([str(s.id) for s in sellers])
        self.assertEqual(one, three)
