"""Per-category delivery rules: seller and superadmin management, feeds, cart data, checkout."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import Category, Product, ProductVariant
from orders.models import Order
from payments.models import PaymentGatewaySettings
from shipping.models import ShippingAddress
from shipping.regions import REGION_NORTH, REGION_NORTH_EAST, REGION_SOUTH
from .models import SellerCategoryRegion, SellerProfile

User = get_user_model()

SELLER_URL = '/api/sellers/delivery-regions/'


def admin_url(profile):
    return f'/api/sellers/profiles/{profile.pk}/delivery-regions/'


class CategoryRegionFixture:
    """A PetKadai-like seller: ships all over India, but plants should go south only."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.seller = User.objects.create_user(email='petkadai@test.com', username='petkadai', password='Pass@12345', role='grower')
        self.profile = SellerProfile.objects.create(user=self.seller, store_name='Pet Kadai', slug='pet-kadai')
        self.plants = Category.objects.create(name='Aquatic Plants', slug='aquatic-plants')
        self.food = Category.objects.create(name='Fish Food', slug='fish-food')
        self.unused = Category.objects.create(name='Hardscape', slug='hardscape')
        self.plant = self._product('Anubias', self.plants)
        self.food_item = self._product('Flakes', self.food)

    def tearDown(self):
        cache.clear()

    def _product(self, name, *categories):
        product = Product.objects.create(name=name, seller=self.seller, is_active=True, is_draft=False)
        product.categories.add(*categories)
        ProductVariant.objects.create(product=product, name='Standard', base_price=Decimal('100'), price=Decimal('110'), stock=10)
        return product

    def _restrict(self, category, *regions):
        fields = {'south': 'ships_to_south', 'north': 'ships_to_north', 'north_east': 'ships_to_north_east'}
        return SellerCategoryRegion.objects.create(
            seller=self.profile, category=category, **{field: code in regions for code, field in fields.items()},
        )


class SellerCategoryRulesApiTests(CategoryRegionFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.seller)

    def test_get_lists_rules_and_the_categories_the_seller_sells(self):
        self._restrict(self.plants, 'south')

        data = self.client.get(SELLER_URL).data

        self.assertEqual(data['category_regions'], [
            {'category_id': self.plants.id, 'category_name': 'Aquatic Plants', 'regions': [REGION_SOUTH]},
        ])
        self.assertEqual([c['name'] for c in data['seller_categories']], ['Aquatic Plants', 'Fish Food'])
        self.assertEqual([c['name'] for c in data['all_categories']], ['Aquatic Plants', 'Fish Food', 'Hardscape'])

    def test_can_restrict_a_category_the_seller_has_no_products_in_yet(self):
        response = self.client.patch(SELLER_URL, {
            'category_regions': [{'category_id': self.unused.id, 'regions': ['south']}],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['category_regions'][0]['category_name'], 'Hardscape')

    def test_rule_set_in_advance_applies_once_products_are_listed(self):
        self._restrict(self.unused, 'south')
        rock = self._product('Dragon stone', self.unused)

        response = self.client.get('/api/core/products/', {'region': REGION_NORTH})

        self.assertNotIn(str(rock.id), {item['id'] for item in response.data['results']})

    def test_patch_sets_rules(self):
        response = self.client.patch(SELLER_URL, {
            'category_regions': [{'category_id': self.plants.id, 'regions': ['south']}],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rule = SellerCategoryRegion.objects.get(seller=self.profile, category=self.plants)
        self.assertEqual(rule.regions, [REGION_SOUTH])

    def test_rule_allowing_every_region_is_not_stored(self):
        self.client.patch(SELLER_URL, {
            'category_regions': [{'category_id': self.plants.id, 'regions': ['south', 'north', 'north_east']}],
        }, format='json')
        self.assertFalse(SellerCategoryRegion.objects.exists())

    def test_patch_replaces_and_empty_list_clears(self):
        self._restrict(self.plants, 'south')

        self.client.patch(SELLER_URL, {
            'category_regions': [{'category_id': self.food.id, 'regions': ['north']}],
        }, format='json')
        self.assertEqual(list(SellerCategoryRegion.objects.values_list('category_id', flat=True)), [self.food.id])

        self.client.patch(SELLER_URL, {'category_regions': []}, format='json')
        self.assertFalse(SellerCategoryRegion.objects.exists())

    def test_omitting_category_regions_keeps_them(self):
        self._restrict(self.plants, 'south')
        self.client.patch(SELLER_URL, {'delivery_fee_south': 30}, format='json')
        self.assertTrue(SellerCategoryRegion.objects.filter(category=self.plants).exists())

    def test_rejects_invalid_rules(self):
        bad_payloads = [
            [{'category_id': self.plants.id, 'regions': []}],
            [{'category_id': self.plants.id, 'regions': ['west']}],
            [{'category_id': 999999, 'regions': ['south']}],
            [{'category_id': self.plants.id, 'regions': ['south']}, {'category_id': self.plants.id, 'regions': ['north']}],
            [{'regions': ['south']}],
            'not-a-list',
        ]
        for payload in bad_payloads:
            response = self.client.patch(SELLER_URL, {'category_regions': payload}, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
        self.assertFalse(SellerCategoryRegion.objects.exists())

    def test_change_clears_cached_feeds_but_no_op_does_not(self):
        cache.set('sentinel', 1)
        self.client.patch(SELLER_URL, {'category_regions': [{'category_id': self.plants.id, 'regions': ['south']}]}, format='json')
        self.assertIsNone(cache.get('sentinel'))

        cache.set('sentinel', 1)
        self.client.patch(SELLER_URL, {'category_regions': [{'category_id': self.plants.id, 'regions': ['south']}]}, format='json')
        self.assertEqual(cache.get('sentinel'), 1)


class SuperadminCategoryRulesApiTests(CategoryRegionFixture, TestCase):
    def test_superuser_manages_any_seller(self):
        admin = User.objects.create_superuser(email='root@test.com', username='root', password='Pass@12345')
        self.client.force_authenticate(user=admin)

        response = self.client.patch(admin_url(self.profile), {
            'ships_to_north_east': False,
            'category_regions': [{'category_id': self.plants.id, 'regions': ['south']}],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.ships_to_north_east)
        self.assertEqual(response.data['category_regions'][0]['regions'], [REGION_SOUTH])

    def test_staff_seller_cannot_use_admin_endpoint(self):
        other = User.objects.create_user(email='rival@test.com', username='rival', password='Pass@12345', role='grower', is_staff=True)
        self.client.force_authenticate(user=other)

        self.assertEqual(self.client.get(admin_url(self.profile)).status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(admin_url(self.profile), {'ships_to_south': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.ships_to_south)

    def test_anonymous_and_unknown_profile(self):
        self.assertEqual(self.client.get(admin_url(self.profile)).status_code, status.HTTP_401_UNAUTHORIZED)

        admin = User.objects.create_superuser(email='root2@test.com', username='root2', password='Pass@12345')
        self.client.force_authenticate(user=admin)
        self.assertEqual(self.client.get('/api/sellers/profiles/999999/delivery-regions/').status_code, status.HTTP_404_NOT_FOUND)


class CategoryRulesInFeedsTests(CategoryRegionFixture, TestCase):
    def _listed(self, **params):
        response = self.client.get('/api/core/products/', params)
        self.assertEqual(response.status_code, 200)
        return {item['id'] for item in response.data['results']}

    def test_restricted_category_hidden_outside_its_regions_only(self):
        self._restrict(self.plants, 'south')

        self.assertEqual(self._listed(region=REGION_SOUTH), {str(self.plant.id), str(self.food_item.id)})
        self.assertEqual(self._listed(region=REGION_NORTH), {str(self.food_item.id)})
        self.assertEqual(self._listed(state='HP'), {str(self.food_item.id)})
        self.assertEqual(self._listed(), {str(self.plant.id), str(self.food_item.id)})

    def test_product_in_two_categories_needs_both_to_allow(self):
        combo = self._product('Plant food kit', self.plants, self.food)
        self._restrict(self.food, 'south', 'north')
        self._restrict(self.plants, 'south', 'north_east')

        self.assertIn(str(combo.id), self._listed(region=REGION_SOUTH))
        self.assertNotIn(str(combo.id), self._listed(region=REGION_NORTH))
        self.assertNotIn(str(combo.id), self._listed(region=REGION_NORTH_EAST))

    def test_rule_cannot_widen_store_regions(self):
        self.profile.ships_to_north = False
        self.profile.save()
        self._restrict(self.plants, 'north')

        self.assertNotIn(str(self.plant.id), self._listed(region=REGION_NORTH))

    def test_rules_of_one_seller_do_not_affect_another(self):
        other = User.objects.create_user(email='other@test.com', username='other', password='Pass@12345', role='grower')
        SellerProfile.objects.create(user=other, store_name='Other', slug='other')
        other_plant = Product.objects.create(name='Other plant', seller=other, is_active=True, is_draft=False)
        other_plant.categories.add(self.plants)
        self._restrict(self.plants, 'south')

        self.assertIn(str(other_plant.id), self._listed(region=REGION_NORTH))

    def test_home_follows_category_rules(self):
        self._restrict(self.plants, 'south')
        response = self.client.get('/api/core/home/', {'state': 'DL'})
        self.assertEqual({p['id'] for p in response.data['products']}, {str(self.food_item.id)})

    def test_adding_a_rule_refreshes_cached_listing(self):
        self.assertIn(str(self.plant.id), self._listed(region=REGION_NORTH))
        self._restrict(self.plants, 'south')
        self.assertNotIn(str(self.plant.id), self._listed(region=REGION_NORTH))

    def test_product_detail_reports_effective_regions(self):
        self.profile.ships_to_north_east = False
        self.profile.save()
        self._restrict(self.plants, 'south', 'north_east')

        plant = self.client.get(f'/api/core/products/id/{self.plant.id}/').data
        food = self.client.get(f'/api/core/products/id/{self.food_item.id}/').data

        self.assertEqual(plant['delivery_regions'], [REGION_SOUTH])
        self.assertEqual(food['delivery_regions'], [REGION_SOUTH, REGION_NORTH])

    def test_cart_configs_report_per_product_regions(self):
        self._restrict(self.plants, 'south')

        response = self.client.get('/api/cart/shipping-configs/', {
            'seller_ids': str(self.seller.id),
            'product_ids': f'{self.plant.id},{self.food_item.id},not-a-uuid',
        })

        product_regions = response.data[str(self.seller.id)]['delivery']['product_regions']
        self.assertEqual(product_regions[str(self.plant.id)], [REGION_SOUTH])
        self.assertEqual(product_regions[str(self.food_item.id)], [REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
@patch('orders.views.check_pincode_deliverable', return_value=(True, 'Delivery available.'))
@patch('orders.views.create_razorpay_order', return_value={'id': 'rzp_cat', 'amount': 100, 'currency': 'INR'})
@patch('payments.tasks.schedule_payment_checks')
class CategoryRulesAtCheckoutTests(CategoryRegionFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.buyer = User.objects.create_user(email='catbuyer@test.com', username='catbuyer', password='Pass@12345', role='collector')
        self.client.force_authenticate(user=self.buyer)
        PaymentGatewaySettings.objects.update_or_create(pk=1, defaults={'active_gateway': 'razorpay'})

    def _checkout(self, products, state, pincode):
        cart = Cart.objects.create(user=self.buyer)
        for product in products:
            CartItem.objects.create(cart=cart, product=product, variant=product.variants.first(), quantity=1)
        address = ShippingAddress.objects.create(
            user=self.buyer, full_name='Buyer', phone='9876543210', email='catbuyer@test.com',
            address_line1='1 Leaf Lane', city='Town', state=state, pincode=pincode,
        )
        return self.client.post('/api/orders/checkout/', {'cart_id': str(cart.id), 'address_id': str(address.id)}, format='json')

    def test_restricted_category_blocked_outside_its_regions(self, *_mocks):
        self._restrict(self.plants, 'south')

        response = self._checkout([self.plant, self.food_item], 'Delhi', '110001')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Pet Kadai doesn't deliver Aquatic Plants to North India", response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_other_categories_of_the_same_seller_still_ship(self, *_mocks):
        self._restrict(self.plants, 'south')

        response = self._checkout([self.food_item], 'Delhi', '110001')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_restricted_category_ships_inside_its_regions(self, *_mocks):
        self._restrict(self.plants, 'south')

        response = self._checkout([self.plant], 'Karnataka', '560001')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
