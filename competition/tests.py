from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from core.config_utils import set_config
from .views import (
    DEFAULT_RESULTS_BANNER_DAYS,
    IST,
    MAX_RESULTS_BANNER_DAYS,
    _promo_visible_until,
    _resolve_promo_enabled,
    _resolve_results_banner_days,
    _should_show_promo,
)

ANNOUNCEMENT = datetime(2026, 7, 1, tzinfo=IST)


class ResolvePromoEnabledTests(SimpleTestCase):
    def test_defaults_to_enabled_when_missing(self):
        self.assertTrue(_resolve_promo_enabled({}))
        self.assertTrue(_resolve_promo_enabled(None))

    def test_boolean_values(self):
        self.assertTrue(_resolve_promo_enabled({'promo_enabled': True}))
        self.assertFalse(_resolve_promo_enabled({'promo_enabled': False}))

    def test_string_values(self):
        for falsy in ('false', 'False', '0', 'no', 'off', ' '):
            self.assertFalse(_resolve_promo_enabled({'promo_enabled': falsy}), falsy)
        for truthy in ('true', 'yes', '1'):
            self.assertTrue(_resolve_promo_enabled({'promo_enabled': truthy}), truthy)


class ResolveResultsBannerDaysTests(SimpleTestCase):
    def test_default_when_missing_or_invalid(self):
        for data in ({}, None, {'results_banner_days': 'abc'}, {'results_banner_days': None}):
            self.assertEqual(_resolve_results_banner_days(data), DEFAULT_RESULTS_BANNER_DAYS)

    def test_accepts_int_and_numeric_string(self):
        self.assertEqual(_resolve_results_banner_days({'results_banner_days': 3}), 3)
        self.assertEqual(_resolve_results_banner_days({'results_banner_days': '10'}), 10)

    def test_clamps_out_of_range(self):
        self.assertEqual(_resolve_results_banner_days({'results_banner_days': -5}), 0)
        self.assertEqual(
            _resolve_results_banner_days({'results_banner_days': 10_000}), MAX_RESULTS_BANNER_DAYS,
        )


class ShouldShowPromoTests(SimpleTestCase):
    def test_disabled_hides_in_every_phase(self):
        until = _promo_visible_until(ANNOUNCEMENT, 7)
        for phase in ('submission', 'voting', 'results'):
            self.assertFalse(_should_show_promo(ANNOUNCEMENT, phase, False, until), phase)

    def test_pre_results_phases_show_when_enabled(self):
        for phase in ('submission', 'voting'):
            self.assertTrue(_should_show_promo(ANNOUNCEMENT, phase, True, None), phase)

    def test_results_visible_within_window(self):
        until = _promo_visible_until(ANNOUNCEMENT, 7)
        self.assertTrue(_should_show_promo(ANNOUNCEMENT, 'results', True, until))
        self.assertTrue(_should_show_promo(until - timedelta(seconds=1), 'results', True, until))

    def test_results_hidden_after_window(self):
        until = _promo_visible_until(ANNOUNCEMENT, 7)
        self.assertFalse(_should_show_promo(until, 'results', True, until))

    def test_zero_days_hides_results_immediately(self):
        until = _promo_visible_until(ANNOUNCEMENT, 0)
        self.assertFalse(_should_show_promo(ANNOUNCEMENT, 'results', True, until))


class CompetitionStatusPromoTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('competition-status')

    def _status_at(self, now, settings_data):
        set_config('competition_settings', settings_data)
        cache.clear()
        with patch('competition.views.timezone.now', return_value=now):
            return self.client.get(self.url).json()

    def test_results_banner_visible_within_configured_days(self):
        data = self._status_at(ANNOUNCEMENT + timedelta(days=2), {
            'launch_date': '01-06-2026',
            'result_announcement_date': '01-07-2026',
            'results_banner_days': 3,
        })
        self.assertEqual(data['phase'], 'results')
        self.assertTrue(data['show_promo'])
        self.assertEqual(
            data['promo_visible_until'], (ANNOUNCEMENT + timedelta(days=3)).isoformat(),
        )

    def test_results_banner_hidden_after_configured_days(self):
        data = self._status_at(ANNOUNCEMENT + timedelta(days=4), {
            'launch_date': '01-06-2026',
            'result_announcement_date': '01-07-2026',
            'results_banner_days': 3,
        })
        self.assertEqual(data['phase'], 'results')
        self.assertFalse(data['show_promo'])

    def test_promo_disabled_hides_during_voting(self):
        data = self._status_at(ANNOUNCEMENT - timedelta(days=5), {
            'launch_date': '01-06-2026',
            'result_announcement_date': '01-07-2026',
            'promo_enabled': False,
        })
        self.assertEqual(data['phase'], 'voting')
        self.assertFalse(data['show_promo'])

    def test_voting_without_announcement_date_shows_promo(self):
        data = self._status_at(ANNOUNCEMENT, {'launch_date': '01-06-2026'})
        self.assertEqual(data['phase'], 'voting')
        self.assertTrue(data['show_promo'])
        self.assertIsNone(data['promo_visible_until'])
