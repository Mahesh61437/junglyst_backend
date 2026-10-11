from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from shipping.regions import REGION_NORTH, REGION_NORTH_EAST, REGION_SOUTH
from .models import SellerProfile, SellerExcludedState

User = get_user_model()

URL = '/api/sellers/delivery-regions/'


def _make_seller(email='regions@test.com', slug='region-store'):
    user = User.objects.create_user(email=email, username=slug, password='Pass@12345', role='grower')
    profile = SellerProfile.objects.create(user=user, store_name=slug.title(), slug=slug)
    return user, profile


class SellerProfileRegionModelTests(TestCase):
    def setUp(self):
        _, self.profile = _make_seller()

    def test_new_profile_ships_all_over_india(self):
        self.assertEqual(self.profile.service_regions, [REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST])
        for region in (REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST):
            self.assertTrue(self.profile.ships_to(region))
            self.assertIsNone(self.profile.delivery_fee_for(region))

    def test_ships_to_respects_flags(self):
        self.profile.ships_to_north = False
        self.profile.ships_to_north_east = False

        self.assertEqual(self.profile.service_regions, [REGION_SOUTH])
        self.assertTrue(self.profile.ships_to(REGION_SOUTH))
        self.assertFalse(self.profile.ships_to(REGION_NORTH))

    def test_unknown_region_never_blocks_and_has_no_fee(self):
        self.profile.ships_to_south = False
        self.assertTrue(self.profile.ships_to('mars'))
        self.assertIsNone(self.profile.delivery_fee_for('mars'))

    def test_delivery_fee_for_region(self):
        self.profile.delivery_fee_north_east = Decimal('149.00')
        self.assertEqual(self.profile.delivery_fee_for(REGION_NORTH_EAST), Decimal('149.00'))


class SellerDeliveryRegionsViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user, self.profile = _make_seller()
        self.client.force_authenticate(user=self.user)

    def test_get_returns_current_settings(self):
        response = self.client.get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['ships_to_south'])
        self.assertIsNone(response.data['delivery_fee_north'])
        self.assertEqual(response.data['service_regions'], [REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST])

    def test_patch_updates_regions_and_fees(self):
        response = self.client.patch(URL, {
            'ships_to_north': False,
            'delivery_fee_south': '40',
            'delivery_fee_north_east': '120',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.ships_to_north)
        self.assertEqual(self.profile.delivery_fee_south, Decimal('40'))
        self.assertEqual(self.profile.delivery_fee_north_east, Decimal('120'))
        self.assertEqual(response.data['service_regions'], [REGION_SOUTH, REGION_NORTH_EAST])

    def test_patch_null_fee_clears_it(self):
        self.profile.delivery_fee_south = Decimal('50')
        self.profile.save()

        response = self.client.patch(URL, {'delivery_fee_south': None}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.delivery_fee_south)

    def test_rejects_deselecting_every_region(self):
        response = self.client.patch(URL, {
            'ships_to_south': False, 'ships_to_north': False, 'ships_to_north_east': False,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.ships_to_south)

    def test_rejects_deselecting_last_remaining_region(self):
        self.profile.ships_to_north = False
        self.profile.ships_to_north_east = False
        self.profile.save()

        response = self.client.patch(URL, {'ships_to_south': False}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_negative_and_excessive_fees(self):
        for fee in ('-1', '999999', 'abc', '49.50'):
            response = self.client.patch(URL, {'delivery_fee_north': fee}, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, fee)

    def test_buyer_cannot_access(self):
        buyer = User.objects.create_user(email='buyer@test.com', username='buyer', password='Pass@12345', role='collector')
        self.client.force_authenticate(user=buyer)

        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.patch(URL, {'ships_to_north': False}, format='json').status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_access(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_seller_only_edits_own_profile(self):
        _, other_profile = _make_seller(email='other@test.com', slug='other-store')

        self.client.patch(URL, {'ships_to_north': False}, format='json')

        other_profile.refresh_from_db()
        self.assertTrue(other_profile.ships_to_north)


class SellerExcludedStatesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user, self.profile = _make_seller()
        self.client.force_authenticate(user=self.user)

    def test_new_profile_excludes_nothing(self):
        self.assertEqual(self.client.get(URL).data['excluded_states'], [])

    def test_patch_sets_excluded_states(self):
        response = self.client.patch(URL, {'excluded_states': ['KL', 'TN']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['excluded_states'], ['KL', 'TN'])
        self.assertEqual(self.profile.excluded_state_codes, ['KL', 'TN'])

    def test_patch_replaces_previous_exclusions(self):
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        response = self.client.patch(URL, {'excluded_states': ['AS']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.profile.excluded_state_codes, ['AS'])

    def test_patch_empty_list_clears_exclusions(self):
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        self.client.patch(URL, {'excluded_states': []}, format='json')

        self.assertEqual(self.profile.excluded_state_codes, [])

    def test_omitting_excluded_states_keeps_them(self):
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')

        self.client.patch(URL, {'delivery_fee_south': 30}, format='json')

        self.assertEqual(self.profile.excluded_state_codes, ['KL'])

    def test_duplicate_codes_are_stored_once(self):
        response = self.client.patch(URL, {'excluded_states': ['KL', 'KL']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.profile.excluded_state_codes, ['KL'])

    def test_rejects_unknown_state_code(self):
        response = self.client.patch(URL, {'excluded_states': ['XX']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.profile.excluded_state_codes, [])

    def test_rejects_excluding_every_served_state(self):
        south_only = {'ships_to_north': False, 'ships_to_north_east': False}
        all_south = ['AP', 'TS', 'TN', 'KA', 'KL', 'PY', 'LD']

        response = self.client.patch(URL, {**south_only, 'excluded_states': all_south}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.ships_to_north)

    def test_change_clears_cached_feeds(self):
        from django.core.cache import cache
        cache.set('sentinel', 1)

        self.client.patch(URL, {'excluded_states': ['KL']}, format='json')

        self.assertIsNone(cache.get('sentinel'))

    def test_unchanged_exclusions_keep_cache(self):
        from django.core.cache import cache
        SellerExcludedState.objects.create(seller=self.profile, state_code='KL')
        cache.set('sentinel', 1)

        self.client.patch(URL, {'excluded_states': ['KL']}, format='json')

        self.assertEqual(cache.get('sentinel'), 1)


class DeliveryRegionsAccessTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_anonymous_patch_is_rejected(self):
        response = self.client.patch(URL, {'ships_to_north': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_grower_without_profile_gets_404(self):
        user = User.objects.create_user(email='noprofile@test.com', username='noprofile', password='Pass@12345', role='grower')
        self.client.force_authenticate(user=user)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_role_can_manage_own_profile(self):
        user = User.objects.create_user(email='adminrole@test.com', username='adminrole', password='Pass@12345', role='admin')
        SellerProfile.objects.create(user=user, store_name='Admin Store', slug='admin-store')
        self.client.force_authenticate(user=user)

        response = self.client.patch(URL, {'excluded_states': ['DL']}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['excluded_states'], ['DL'])


class DeliveryRegionsValidationEdgeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user, self.profile = _make_seller(email='edge@test.com', slug='edge-store')
        self.client.force_authenticate(user=self.user)

    def test_exclusions_inside_a_disabled_region_do_not_count_against_served_states(self):
        all_north_east = ['AS', 'AR', 'MN', 'ML', 'MZ', 'NL', 'TR', 'SK']
        response = self.client.patch(URL, {
            'ships_to_north_east': False, 'excluded_states': all_north_east,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_fee_boundaries(self):
        for fee, expected in (('0', 200), ('5000', 200), ('5001', 400), ('0.5', 400)):
            response = self.client.patch(URL, {'delivery_fee_south': fee}, format='json')
            self.assertEqual(response.status_code, expected, (fee, response.data))

    def test_excluded_states_must_be_a_list(self):
        response = self.client.patch(URL, {'excluded_states': 'KL'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
