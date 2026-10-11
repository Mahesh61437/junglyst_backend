"""Checkout rules for seller delivery regions and region-based delivery fees."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import Product, ProductVariant
from orders.models import Order, SubOrder
from payments.models import PaymentGatewaySettings
from sellers.models import SellerProfile, SellerShippingConfig, SellerExcludedState
from shipping.models import ShippingAddress

User = get_user_model()

FAKE_RAZORPAY_ORDER = {"id": "rzp_ord_REGION", "amount": 100000, "currency": "INR"}

SOUTH_ADDRESS = dict(state='Karnataka', pincode='560001', city='Bengaluru')
NORTH_ADDRESS = dict(state='Delhi', pincode='110001', city='New Delhi')
NORTH_EAST_ADDRESS = dict(state='Assam', pincode='781001', city='Guwahati')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
@patch('orders.views.check_pincode_deliverable', return_value=(True, 'Delivery available.'))
@patch('orders.views.create_razorpay_order', return_value=FAKE_RAZORPAY_ORDER)
@patch('payments.tasks.schedule_payment_checks')
class CheckoutDeliveryRegionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.buyer = User.objects.create_user(
            email='region-buyer@test.com', username='region-buyer', password='Pass@12345', role='collector',
        )
        self.client.force_authenticate(user=self.buyer)
        seller = User.objects.create_user(
            email='region-seller@test.com', username='region-seller', password='Pass@12345', role='grower',
        )
        self.profile = SellerProfile.objects.create(
            user=seller, store_name='South Greens', slug='south-greens',
            ships_to_north=False, ships_to_north_east=False,
        )
        product = Product.objects.create(name='Anubias', seller=seller, is_active=True, is_draft=False)
        self.variant = variant = ProductVariant.objects.create(
            product=product, name='Standard', base_price=Decimal('200'), price=Decimal('250.00'), stock=10,
        )
        # Slab config: the cart subtotal falls in tier 1 → ₹99
        SellerShippingConfig.objects.create(
            seller=seller, item_category='light',
            tier1_max=Decimal('1000'), tier1_fee=Decimal('99'),
            tier2_max=Decimal('2000'), tier2_fee=Decimal('49'),
        )
        self.cart = Cart.objects.create(user=self.buyer)
        CartItem.objects.create(cart=self.cart, product=product, variant=variant, quantity=2)
        PaymentGatewaySettings.objects.update_or_create(pk=1, defaults={'active_gateway': 'razorpay'})

    def _checkout_to(self, address_fields):
        address = ShippingAddress.objects.create(
            user=self.buyer, full_name='Buyer', phone='9876543210', email='region-buyer@test.com',
            address_line1='1 Leaf Lane', **address_fields,
        )
        return self.client.post('/api/orders/checkout/', {
            'cart_id': str(self.cart.id), 'address_id': str(address.id),
        }, format='json')

    def test_blocks_seller_that_does_not_ship_to_address_region(self, *_mocks):
        response = self._checkout_to(NORTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("South Greens doesn't deliver to North India", response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_allows_seller_shipping_to_address_region_with_slab_fee(self, *_mocks):
        response = self._checkout_to(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        sub_order = SubOrder.objects.get()
        self.assertEqual(sub_order.shipping_fee, Decimal('99'))

    def test_regional_fee_overrides_slab_fee(self, *_mocks):
        self.profile.delivery_fee_south = Decimal('40')
        self.profile.save()

        response = self._checkout_to(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_fee, Decimal('40'))
        self.assertEqual(order.total_amount, order.subtotal + Decimal('40'))
        self.assertEqual(order.sub_orders.get().seller_total, order.subtotal + Decimal('40'))

    def test_regional_fee_for_other_region_is_not_applied(self, *_mocks):
        self.profile.ships_to_north_east = True
        self.profile.delivery_fee_south = Decimal('40')
        self.profile.save()

        response = self._checkout_to(NORTH_EAST_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(SubOrder.objects.get().shipping_fee, Decimal('99'))

    def test_zero_regional_fee_means_free_delivery(self, *_mocks):
        self.profile.delivery_fee_south = Decimal('0')
        self.profile.save()

        response = self._checkout_to(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(SubOrder.objects.get().shipping_fee, Decimal('0'))

    def test_pincode_decides_region_over_mismatched_state(self, *_mocks):
        # State says Karnataka but the pincode is Delhi's — couriers route on pincode.
        response = self._checkout_to(dict(state='Karnataka', pincode='110001', city='Delhi'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def _guest_checkout_to(self, address_fields):
        """One-time address (guest, or a logged-in buyer not saving it) with inline items."""
        self.client.force_authenticate(user=None)
        address = dict(full_name='Guest', phone='9876543210', email='guest@test.com',
                       address_line1='2 Fern Road', **address_fields)
        return self.client.post('/api/orders/checkout/', {
            'items': [{'variant_id': str(self.variant.id), 'quantity': 1}],
            'guest_info': {'email': 'guest@test.com', 'phone': '9876543210', 'address': address},
            'pincode': address_fields.get('pincode', ''),
        }, format='json')

    def test_one_time_address_outside_seller_regions_is_blocked(self, *_mocks):
        response = self._guest_checkout_to(NORTH_EAST_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doesn't deliver to North East India", response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_one_time_address_inside_seller_regions_is_accepted(self, *_mocks):
        response = self._guest_checkout_to(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_state_decides_region_when_pincode_missing(self, *_mocks):
        response = self._guest_checkout_to(dict(state='Delhi', pincode='', city='New Delhi'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doesn't deliver to North India", response.data['error'])


    def test_excluded_state_is_blocked(self, *_mocks):
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        response = self._checkout_to(dict(state='Kerala', pincode='682001', city='Kochi'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("South Greens doesn't deliver to Kerala", response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_other_state_in_region_still_allowed(self, *_mocks):
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        response = self._checkout_to(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unrecognised_state_blocked_only_when_seller_has_exclusions(self, *_mocks):
        misspelt = dict(state='Karnatka', pincode='560001', city='Bengaluru')
        self.assertEqual(self._checkout_to(misspelt).status_code, status.HTTP_201_CREATED)

        Order.objects.all().delete()
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        response = self._checkout_to(misspelt)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('valid state', response.data['error'])

    def test_one_time_address_in_excluded_state_is_blocked(self, *_mocks):
        SellerExcludedState.objects.create(seller=self.profile, state_code='TN')

        response = self._guest_checkout_to(dict(state='Tamil Nadu', pincode='600001', city='Chennai'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doesn't deliver to Tamil Nadu", response.data['error'])


    def test_pincode_from_another_state_is_rejected(self, *_mocks):
        # Seller serves the south; the PIN is in Andhra Pradesh but the state says Himachal
        response = self._checkout_to(dict(state='Himachal Pradesh', pincode='517128', city='Yadamari'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("PIN code 517128 doesn't belong to Himachal Pradesh", response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_one_time_address_with_mismatched_pincode_is_rejected(self, *_mocks):
        response = self._guest_checkout_to(dict(state='Kerala', pincode='110001', city='Kochi'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doesn't belong to Kerala", response.data['error'])

    def test_matching_pincode_and_state_is_accepted(self, *_mocks):
        response = self._checkout_to(dict(state='Andhra Pradesh', pincode='517128', city='Yadamari'))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_saved_address_uses_its_own_pincode_over_request_pincode(self, *_mocks):
        # UI sent the saved address id with a PIN typed for a different address;
        # the order ships to the saved address, so that is what gets checked.
        address = ShippingAddress.objects.create(
            user=self.buyer, full_name='Buyer', phone='9876543210', email='region-buyer@test.com',
            address_line1='1-16', state='Andhra Pradesh', pincode='517128', city='Yadamari',
        )
        response = self.client.post('/api/orders/checkout/', {
            'cart_id': str(self.cart.id), 'address_id': str(address.id), 'pincode': '174033',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(Order.objects.get().shipping_address['pincode'], '517128')


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
@patch('orders.views.check_pincode_deliverable', return_value=(True, 'Delivery available.'))
@patch('orders.views.create_razorpay_order', return_value=FAKE_RAZORPAY_ORDER)
@patch('payments.tasks.schedule_payment_checks')
class MultiSellerCheckoutRegionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.buyer = User.objects.create_user(email='multi@test.com', username='multi', password='Pass@12345', role='collector')
        self.client.force_authenticate(user=self.buyer)
        self.cart = Cart.objects.create(user=self.buyer)
        self._seller('south-store', 'South Store', ships_to_north=False, delivery_fee_south=Decimal('30'))
        self._seller('all-store', 'All Store', delivery_fee_south=Decimal('20'))
        PaymentGatewaySettings.objects.update_or_create(pk=1, defaults={'active_gateway': 'razorpay'})

    def _seller(self, slug, store_name, **profile_fields):
        seller = User.objects.create_user(email=f'{slug}@test.com', username=slug, password='Pass@12345', role='grower')
        SellerProfile.objects.create(user=seller, store_name=store_name, slug=slug, **profile_fields)
        product = Product.objects.create(name=f'{store_name} plant', seller=seller, is_active=True, is_draft=False)
        variant = ProductVariant.objects.create(
            product=product, name='Standard', base_price=Decimal('100'), price=Decimal('110'), stock=10,
        )
        CartItem.objects.create(cart=self.cart, product=product, variant=variant, quantity=1)

    def _checkout(self, address_fields):
        address = ShippingAddress.objects.create(
            user=self.buyer, full_name='Buyer', phone='9876543210', email='multi@test.com',
            address_line1='1 Fern Road', **address_fields,
        )
        return self.client.post('/api/orders/checkout/', {
            'cart_id': str(self.cart.id), 'address_id': str(address.id),
        }, format='json')

    def test_names_the_seller_who_cannot_deliver(self, *_mocks):
        response = self._checkout(NORTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('South Store', response.data['error'])
        self.assertFalse(Order.objects.exists())

    def test_each_seller_charges_its_own_regional_fee(self, *_mocks):
        response = self._checkout(SOUTH_ADDRESS)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        fees = {
            sub.seller.seller_profile.slug: sub.shipping_fee
            for sub in SubOrder.objects.select_related('seller__seller_profile')
        }
        self.assertEqual(fees, {'south-store': Decimal('30'), 'all-store': Decimal('20')})
        self.assertEqual(Order.objects.get().shipping_fee, Decimal('50'))
