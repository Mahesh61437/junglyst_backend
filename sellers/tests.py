from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from .models import SellerProfile, AllowedSeller

User = get_user_model()

class SellerDashboardTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='grower@junglyst.com',
            username='grower1',
            password='password123',
            role='grower'
        )
        AllowedSeller.objects.create(email='grower@junglyst.com', is_active=True)
        self.client.force_authenticate(user=self.user)

    def test_get_dashboard_data(self):
        response = self.client.get('/api/sellers/dashboard/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('metrics', response.data)
        self.assertIn('profile', response.data)

    def test_update_profile(self):
        data = {
            'store_name': 'Green Sanctuary',
            'bio': 'A beautiful bio',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        profile = SellerProfile.objects.get(user=self.user)
        self.assertEqual(profile.store_name, 'Green Sanctuary')
        self.assertEqual(profile.slug, 'green-sanctuary')

    def test_update_profile_missing_mandatory_fields(self):
        # Missing pickup_address
        data = {
            'store_name': 'Green Sanctuary',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertEqual(response.data['error'], 'Pickup street address is required.')

    def test_update_profile_invalid_phone(self):
        data = {
            'store_name': 'Green Sanctuary',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '12345'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertEqual(response.data['error'], 'Phone number must be a valid 10-digit Indian mobile number.')

    def test_collector_upgrade_on_save(self):
        # Create a collector
        collector = User.objects.create_user(
            email='collector@junglyst.com',
            username='collector1',
            password='password123',
            role='collector'
        )
        AllowedSeller.objects.create(email='collector@junglyst.com', is_active=True)
        self.client.force_authenticate(user=collector)
        
        data = {
            'store_name': 'New Studio',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check if role upgraded
        collector.refresh_from_db()
        self.assertEqual(collector.role, 'grower')

    def test_unauthenticated_access(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/sellers/dashboard/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


from datetime import date, time, datetime, timedelta
from django.utils import timezone as _tz
from .models import SellerBlackoutDate


def _make_seller(email='ship@junglyst.com', store='Ship Store', days=None, cutoff=time(12, 0)):
    user = User.objects.create_user(
        email=email, username=email.split('@')[0], password='pw', role='grower'
    )
    profile, _ = SellerProfile.objects.get_or_create(
        user=user,
        defaults={'store_name': store, 'slug': store.lower().replace(' ', '-'), 'brand_color': '#000'},
    )
    if days is not None:
        profile.shipping_days = days
    profile.daily_cutoff_time = cutoff
    profile.save()
    return user, profile


def _aware(year, month, day, hour=9, minute=0):
    return _tz.make_aware(datetime(year, month, day, hour, minute))


class NextShippingDateTest(TestCase):
    """get_next_shipping_date — cutoff + blackout behaviour."""

    def test_empty_shipping_days_returns_none(self):
        _, profile = _make_seller(days=[])
        self.assertIsNone(profile.get_next_shipping_date())

    def test_before_cutoff_on_shipping_day_ships_today(self):
        # 2026-05-25 is a Monday (weekday 0)
        _, profile = _make_seller(days=[0], cutoff=time(12, 0))
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 25, 9, 0))
        self.assertEqual(result, date(2026, 5, 25))

    def test_after_cutoff_on_shipping_day_rolls_forward(self):
        # Same Monday, but 14:00 — past 12:00 cut-off → roll to next Monday
        _, profile = _make_seller(days=[0], cutoff=time(12, 0))
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 25, 14, 0))
        self.assertEqual(result, date(2026, 6, 1))

    def test_exact_cutoff_time_rolls_forward(self):
        # At cutoff exactly → seller missed it
        _, profile = _make_seller(days=[0], cutoff=time(12, 0))
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 25, 12, 0))
        self.assertEqual(result, date(2026, 6, 1))

    def test_non_shipping_day_picks_next_in_week(self):
        # Tue, Thu, Sun → Wednesday 2026-05-27 should pick Thu 2026-05-28
        _, profile = _make_seller(days=[1, 3, 6])
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 27, 9, 0))
        self.assertEqual(result, date(2026, 5, 28))

    def test_blackout_skips_eligible_day(self):
        # Mondays only; mark Mon 2026-06-01 as blackout — should jump to Mon 2026-06-08
        _, profile = _make_seller(days=[0], cutoff=time(12, 0))
        SellerBlackoutDate.objects.create(
            seller=profile,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            reason='Vacation',
        )
        # Use 2026-05-25 14:00 so today rolls forward (post-cutoff) and the next Mon is blacked out
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 25, 14, 0))
        self.assertEqual(result, date(2026, 6, 8))

    def test_blackout_range_spans_multiple_days(self):
        # Ships Mon+Wed; blackout the whole week of June 1
        _, profile = _make_seller(days=[0, 2])
        SellerBlackoutDate.objects.create(
            seller=profile,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 7),
            reason='Festival',
        )
        # Friday 2026-05-29 → next ship would be Mon 2026-06-01 (blacked) → then Wed 2026-06-03 (blacked) → Mon 2026-06-08
        result = profile.get_next_shipping_date(as_of=_aware(2026, 5, 29, 9, 0))
        self.assertEqual(result, date(2026, 6, 8))


class BlackoutEndpointsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='vac@junglyst.com', username='vac', password='pw', role='grower'
        )
        AllowedSeller.objects.create(email='vac@junglyst.com', is_active=True)
        # Create seller profile via dashboard so role + profile exist
        SellerProfile.objects.create(
            user=self.user, store_name='Vac Store', slug='vac-store', brand_color='#000'
        )
        self.client.force_authenticate(user=self.user)

    def test_create_and_list_blackout(self):
        today = _tz.localdate()
        r = self.client.post('/api/sellers/blackouts/', {
            'start_date': (today + timedelta(days=3)).isoformat(),
            'end_date': (today + timedelta(days=5)).isoformat(),
            'reason': 'Out of town',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)

        r = self.client.get('/api/sellers/blackouts/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(r.data[0]['reason'], 'Out of town')

    def test_end_before_start_rejected(self):
        r = self.client.post('/api/sellers/blackouts/', {
            'start_date': '2026-07-10', 'end_date': '2026-07-05',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_delete_blackout(self):
        b = SellerBlackoutDate.objects.create(
            seller=self.user.seller_profile,
            start_date=_tz.localdate() + timedelta(days=2),
            end_date=_tz.localdate() + timedelta(days=2),
        )
        r = self.client.delete(f'/api/sellers/blackouts/{b.id}/')
        self.assertEqual(r.status_code, 204)
        self.assertFalse(SellerBlackoutDate.objects.filter(pk=b.id).exists())

    def test_other_seller_cannot_delete(self):
        b = SellerBlackoutDate.objects.create(
            seller=self.user.seller_profile,
            start_date=_tz.localdate() + timedelta(days=2),
            end_date=_tz.localdate() + timedelta(days=2),
        )
        other = User.objects.create_user(
            email='other@junglyst.com', username='other', password='pw', role='grower'
        )
        SellerProfile.objects.create(
            user=other, store_name='Other Store', slug='other-store', brand_color='#000'
        )
        self.client.force_authenticate(user=other)
        r = self.client.delete(f'/api/sellers/blackouts/{b.id}/')
        self.assertEqual(r.status_code, 404)

    def test_collector_cannot_call_blackouts(self):
        buyer = User.objects.create_user(
            email='buy@junglyst.com', username='buy', password='pw', role='collector'
        )
        self.client.force_authenticate(user=buyer)
        r = self.client.get('/api/sellers/blackouts/')
        self.assertEqual(r.status_code, 403)

    def test_update_cutoff_time_via_dashboard(self):
        # Have to send all mandatory fields too
        r = self.client.post('/api/sellers/dashboard/', {
            'store_name': 'Vac Store',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '1 Lane',
            'phone': '9876543210',
            'daily_cutoff_time': '15:30',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.user.seller_profile.refresh_from_db()
        self.assertEqual(self.user.seller_profile.daily_cutoff_time, time(15, 30))


class DefaultShippingDaysTest(TestCase):
    """New seller profiles auto-get Mon/Wed/Fri so checkout never blocks."""

    def test_new_profile_has_default_shipping_days(self):
        user = User.objects.create_user(
            email='newgrower@junglyst.com', username='newg', password='pw', role='grower'
        )
        # SellerProfileManager.get_or_get_default — used by GrowerDashboardView.get
        profile, created = SellerProfile.objects.get_or_get_default(user=user)
        self.assertTrue(created)
        self.assertEqual(profile.shipping_days, [0, 2, 4])

    def test_bare_create_uses_field_default(self):
        # Even plain `objects.create` (e.g. signals / migrations) gets the default
        user = User.objects.create_user(
            email='bare@junglyst.com', username='bare', password='pw', role='grower'
        )
        profile = SellerProfile.objects.create(
            user=user, store_name='Bare', slug='bare', brand_color='#000'
        )
        self.assertEqual(profile.shipping_days, [0, 2, 4])

    def test_get_next_shipping_date_works_for_default_seller(self):
        user = User.objects.create_user(
            email='def@junglyst.com', username='def', password='pw', role='grower'
        )
        profile, _ = SellerProfile.objects.get_or_get_default(user=user)
        # Default seller can always reach a ship date — no None
        self.assertIsNotNone(profile.get_next_shipping_date(as_of=_aware(2026, 5, 24, 9, 0)))
