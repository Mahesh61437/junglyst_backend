from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .regions import (
    REGION_NORTH, REGION_NORTH_EAST, REGION_SOUTH,
    pincode_matches_state, region_for_pincode, region_for_state, resolve_address_region,
    state_code_for, states_for_pincode, states_payload,
)


class RegionForStateTests(SimpleTestCase):
    def test_south_states_by_name_and_abbreviation(self):
        for state in ('Andhra Pradesh', 'A.P', 'AP', 'T.N', 'Tamil Nadu', 'Karnataka', 'kerala', 'Telangana', 'TS', 'TG'):
            self.assertEqual(region_for_state(state), REGION_SOUTH, state)

    def test_north_east_states(self):
        for state in ('Assam', 'Arunachal Pradesh', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Tripura', 'Sikkim'):
            self.assertEqual(region_for_state(state), REGION_NORTH_EAST, state)

    def test_everything_else_is_north(self):
        for state in ('Delhi', 'NCT of Delhi', 'Maharashtra', 'Goa', 'West Bengal', 'Orissa', 'Jammu & Kashmir'):
            self.assertEqual(region_for_state(state), REGION_NORTH, state)

    def test_unknown_or_empty_state_returns_none(self):
        for state in (None, '', '   ', 'Atlantis', 123):
            self.assertIsNone(region_for_state(state), state)

    def test_every_state_resolves_to_its_own_region(self):
        for state in states_payload():
            self.assertEqual(region_for_state(state['name']), state['region'])
            self.assertEqual(region_for_state(state['code']), state['region'])


class StateCodeForTests(SimpleTestCase):
    def test_names_codes_and_aliases_resolve_to_code(self):
        for state, code in (('Kerala', 'KL'), ('k.l', 'KL'), ('Orissa', 'OD'), ('TG', 'TS'), ('NCT of Delhi', 'DL')):
            self.assertEqual(state_code_for(state), code, state)

    def test_unknown_returns_none(self):
        for state in (None, '', 'Kerela', 42):
            self.assertIsNone(state_code_for(state), state)


class RegionForPincodeTests(SimpleTestCase):
    def test_south_pincodes(self):
        for pincode in ('500001', '520001', '560001', '600001', '605001', '682001', '695001'):
            self.assertEqual(region_for_pincode(pincode), REGION_SOUTH, pincode)

    def test_north_east_pincodes_including_sikkim(self):
        for pincode in ('781001', '793001', '799001', '737101'):
            self.assertEqual(region_for_pincode(pincode), REGION_NORTH_EAST, pincode)

    def test_north_pincodes(self):
        for pincode in ('110001', '400001', '403001', '700001', '751001', '800001'):
            self.assertEqual(region_for_pincode(pincode), REGION_NORTH, pincode)

    def test_malformed_pincodes_return_none(self):
        for pincode in (None, '', '56000', '5600011', 'abcdef', '012345'):
            self.assertIsNone(region_for_pincode(pincode), pincode)


class PincodeStateMatchTests(SimpleTestCase):
    def test_pincode_maps_to_its_circle_states(self):
        self.assertEqual(states_for_pincode('517128'), ('AP', 'TS'))
        self.assertIn('CH', states_for_pincode('160017'))
        self.assertIn('SK', states_for_pincode('737101'))

    def test_matching_state_passes(self):
        for pincode, state in (('517128', 'AP'), ('560001', 'KA'), ('160017', 'CH'), ('171001', 'HP'),
                               ('403001', 'GA'), ('682555', 'LD'), ('110001', 'DL')):
            self.assertTrue(pincode_matches_state(pincode, state), (pincode, state))

    def test_state_from_another_circle_fails(self):
        for pincode, state in (('517128', 'HP'), ('560001', 'DL'), ('110001', 'KA'), ('781001', 'KL')):
            self.assertFalse(pincode_matches_state(pincode, state), (pincode, state))

    def test_unknown_inputs_are_not_flagged(self):
        self.assertTrue(pincode_matches_state('', 'HP'))
        self.assertTrue(pincode_matches_state('517128', None))
        self.assertTrue(pincode_matches_state('912345', 'KA'))   # APO circle, unmapped
        self.assertEqual(states_for_pincode('51712'), ())

    def test_every_state_is_reachable_from_some_pincode_circle(self):
        from .regions import _PINCODE_CIRCLE_STATES, STATE_CODES
        covered = {code for codes in _PINCODE_CIRCLE_STATES.values() for code in codes}
        self.assertEqual(set(STATE_CODES) - covered, set())


class ResolveAddressRegionTests(SimpleTestCase):
    def test_pincode_wins_over_state(self):
        self.assertEqual(resolve_address_region(state='Delhi', pincode='560001'), REGION_SOUTH)

    def test_falls_back_to_state_when_pincode_missing_or_bad(self):
        self.assertEqual(resolve_address_region(state='Assam', pincode=''), REGION_NORTH_EAST)
        self.assertEqual(resolve_address_region(state='Kerala', pincode='12'), REGION_SOUTH)

    def test_nothing_known_returns_none(self):
        self.assertIsNone(resolve_address_region())


class DeliveryRegionsEndpointTests(TestCase):
    def test_lists_regions_and_states_publicly(self):
        response = APIClient().get('/api/shipping/regions/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [r['code'] for r in response.data['regions']],
            [REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST],
        )
        states = {s['code']: s['region'] for s in response.data['states']}
        self.assertEqual(states['AP'], REGION_SOUTH)
        self.assertEqual(states['DL'], REGION_NORTH)
        self.assertEqual(states['AS'], REGION_NORTH_EAST)
        self.assertIn('max-age', response['Cache-Control'])


class PincodeCheckStatesTests(TestCase):
    def test_response_lists_candidate_states(self):
        from unittest.mock import patch
        with patch('shipping.views.check_pincode_deliverable', return_value=(True, 'Delivery available.')):
            response = APIClient().get('/api/shipping/pincode-check/', {'pincode': '517128'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['states'], ['AP', 'TS'])
        self.assertEqual(response.data['region'], REGION_SOUTH)


class RegionEndpointsEdgeTests(TestCase):
    def test_pincode_check_rejects_malformed_pincodes(self):
        for pincode in ('', '12345', 'abcdef', '1234567'):
            response = APIClient().get('/api/shipping/pincode-check/', {'pincode': pincode})
            self.assertEqual(response.status_code, 400, pincode)

    def test_regions_catalog_has_unique_codes_covering_every_region(self):
        response = APIClient().get('/api/shipping/regions/')
        codes = [s['code'] for s in response.data['states']]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 36)
        self.assertEqual(
            {s['region'] for s in response.data['states']},
            {REGION_SOUTH, REGION_NORTH, REGION_NORTH_EAST},
        )
