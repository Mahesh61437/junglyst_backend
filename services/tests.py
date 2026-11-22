"""
Quote-based services: validation, quote request, admin/seller quote editing,
pay request, payment into a seller SubOrder, and permissions.

Run with (SQLite + local cache, never the .env services):
    DB_HOST= REDIS_URL= REDISHOST= python manage.py test services
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIClient

from notifications.models import AppNotification
from orders.models import Order, OrderStatus, OrderType, PaymentStatus, SellerSettlement
from payments.models import PaymentGateway
from sellers.models import SellerExcludedState, SellerProfile

from . import validators
from .models import (
    AdditionalCharge, ChargeStatus, FormFieldType, PayoutStatus, QuoteEvent, QuoteRequest, QuoteStatus,
    ServiceFormField, ServiceGroup, ServiceOffering,
)
from .workflow import ServiceOrderHandler, expire_overdue_pay_requests, send_pay_by_reminders

User = get_user_model()

TEST_SETTINGS = dict(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
FAKE_GATEWAY = {
    'gateway': PaymentGateway.RAZORPAY, 'razorpay_order_id': 'rzp_svc_1',
    'razorpay_key_id': 'rzp_test', 'amount': Decimal('5200.00'), 'currency': 'INR',
}
REQUEST_URL = '/api/services/offerings/custom-terrarium/request/'


def make_user(email, **extra):
    return User.objects.create_user(email=email, username=email.split('@')[0], password='Pass@123', **extra)


def make_seller(email, store_name):
    seller = make_user(email, role='grower', is_verified_seller=True)
    SellerProfile.objects.create(user=seller, store_name=store_name, slug=store_name.lower().replace(' ', '-'))
    return seller


def valid_request_payload(**overrides):
    payload = {
        'contact_name': 'Asha Rao', 'contact_phone': '+91 98765 43210', 'contact_email': 'Asha@Example.com',
        'state': 'Karnataka', 'budget_min': '3000', 'budget_max': '6000',
        'answers': {'size': 'Medium', 'theme': ['Jungle'], 'depth_cm': '30'},
    }
    payload.update(overrides)
    return payload


class ServiceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('admin@junglyst.test', is_superuser=True, is_staff=True, role='admin')
        cls.seller = make_seller('seller@junglyst.test', 'Moss Studio')
        cls.group = ServiceGroup.objects.create(name='Terrariums', slug='terrariums', home_sort_order=1)
        cls.offering = ServiceOffering.objects.create(
            group=cls.group, name='Custom terrarium', slug='custom-terrarium', suggested_min_budget=Decimal('3000'),
        )
        ServiceFormField.objects.bulk_create([
            ServiceFormField(offering=cls.offering, key='size', label='Size', field_type=FormFieldType.SELECT,
                             required=True, choices=['Small', 'Medium', 'Large'], sort_order=1),
            ServiceFormField(offering=cls.offering, key='theme', label='Theme', field_type=FormFieldType.MULTISELECT,
                             choices=['Jungle', 'Desert'], sort_order=2),
            ServiceFormField(offering=cls.offering, key='depth_cm', label='Depth', field_type=FormFieldType.NUMBER,
                             min_value=Decimal('10'), max_value=Decimal('100'), sort_order=3),
        ])

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.client = APIClient()

    def make_request(self, **overrides):
        payload = valid_request_payload(**overrides)
        response = self.client.post(REQUEST_URL, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return QuoteRequest.objects.get(reference=response.data['reference'])

    def priced_request(self, seller=None, user=None):
        quote = self.make_request()
        quote.assigned_seller = seller or self.seller
        quote.item_price = Decimal('5000')
        quote.shipping_fee = Decimal('200')
        quote.status = QuoteStatus.IN_REVIEW
        quote.user = user
        quote.save()
        return quote


# ── Validators ───────────────────────────────────────────────────────────────

class ValidatorTests(TestCase):
    def test_phone_is_normalised_to_ten_digits(self):
        for raw in ('9876543210', '+91 98765-43210', '09876543210', '919876543210'):
            self.assertEqual(validators.normalise_phone(raw), '9876543210')

    def test_phone_rejects_bad_numbers(self):
        for raw in ('12345', '5876543210', '98765432101', 'abcdefghij', ''):
            with self.assertRaises(serializers.ValidationError):
                validators.normalise_phone(raw)

    def test_person_name_rules(self):
        self.assertEqual(validators.validate_person_name('  Asha   Rao '), 'Asha Rao')
        for bad in ('A', '', '12345', '<script>'):
            with self.assertRaises(serializers.ValidationError):
                validators.validate_person_name(bad)

    def test_state_accepts_name_or_code_and_rejects_unknown(self):
        self.assertEqual(validators.validate_state('Karnataka'), 'KA')
        self.assertEqual(validators.validate_state('ka'), 'KA')
        with self.assertRaises(serializers.ValidationError):
            validators.validate_state('Atlantis')

    def test_pincode_format_zone_and_state_match(self):
        self.assertEqual(validators.validate_pincode('560001', 'KA'), '560001')
        for bad_pin, state in (('56001', 'KA'), ('056001', 'KA'), ('abcdef', 'KA'),
                               ('744101', 'KA'),   # blocked zone
                               ('110001', 'KA')):  # Delhi pincode, Karnataka selected
            with self.assertRaises(serializers.ValidationError, msg=bad_pin):
                validators.validate_pincode(bad_pin, state)

    def test_budget_rules(self):
        validators.validate_budget(Decimal('1000'), Decimal('1000'))
        for low, high in ((Decimal('0'), Decimal('10')), (Decimal('500'), Decimal('100')),
                          (Decimal('10'), Decimal('99999999'))):
            with self.assertRaises(serializers.ValidationError):
                validators.validate_budget(low, high)

    @override_settings(FIREBASE_STORAGE_BUCKET='junglyst_test')
    def test_reference_images_must_come_from_our_bucket(self):
        good = 'https://storage.googleapis.com/junglyst_test/dev/assets/service-reference/a.png'
        self.assertEqual(validators.validate_reference_images([good]), [good])
        for bad in (['http://example.com/a.jpg'], ['javascript:alert(1)'], [good] * 6, 'nope',
                    ['https://evil.example.com/pixel.png'],                                   # someone else's server
                    ['https://storage.googleapis.com/other_bucket/a.png'],                      # someone else's bucket
                    ['https://storage.googleapis.com/junglyst_test.evil.com/a.png']):           # look-alike host trick
            with self.assertRaises(serializers.ValidationError, msg=str(bad)):
                validators.validate_reference_images(bad)

    @override_settings(FIREBASE_STORAGE_BUCKET='')
    def test_without_a_configured_bucket_no_link_is_accepted(self):
        with self.assertRaises(serializers.ValidationError):
            validators.validate_reference_images(['https://storage.googleapis.com/anything/a.png'])

    def test_inclusions_are_cleaned_and_validated(self):
        cleaned = validators.validate_inclusions([{'label': ' Glass  jar ', 'amount': '1200'}, {'label': 'Moss'}])
        self.assertEqual(cleaned, [{'label': 'Glass jar', 'amount': '1200.00'}, {'label': 'Moss', 'amount': None}])
        for bad in ([{'label': ''}], [{'label': 'x', 'amount': '-5'}], [{'label': 'x', 'amount': '1.234'}],
                    [{'label': 'x', 'amount': 'abc'}], ['text']):
            with self.assertRaises(serializers.ValidationError):
                validators.validate_inclusions(bad)

    def test_form_field_definition_needs_choices_for_selects(self):
        with self.assertRaises(serializers.ValidationError):
            validators.validate_form_field_definition(FormFieldType.SELECT, [], None, None)
        with self.assertRaises(serializers.ValidationError):
            validators.validate_form_field_definition(FormFieldType.SELECT, ['a', 'a'], None, None)
        with self.assertRaises(serializers.ValidationError):
            validators.validate_form_field_definition(FormFieldType.NUMBER, [], Decimal('5'), Decimal('1'))


# ── Public catalogue ─────────────────────────────────────────────────────────

class CatalogueTests(ServiceTestCase):
    def test_groups_list_shows_only_active_groups_and_offerings(self):
        hidden_group = ServiceGroup.objects.create(name='Hidden', slug='hidden', is_active=False)
        ServiceOffering.objects.create(group=hidden_group, name='Secret', slug='secret')
        ServiceOffering.objects.create(group=self.group, name='Retired', slug='retired', is_active=False)
        response = self.client.get('/api/services/groups/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([g['slug'] for g in response.data], ['terrariums'])
        self.assertEqual([o['slug'] for o in response.data[0]['offerings']], ['custom-terrarium'])

    def test_home_filter_respects_show_on_home(self):
        other = ServiceGroup.objects.create(name='Care', slug='care', show_on_home=False)
        ServiceOffering.objects.create(group=other, name='Plant care', slug='plant-care')
        self.assertEqual(len(self.client.get('/api/services/groups/').data), 2)
        home = self.client.get('/api/services/groups/?home=1')
        self.assertEqual([g['slug'] for g in home.data], ['terrariums'])

    def test_group_list_query_count_is_constant(self):
        for index in range(4):
            group = ServiceGroup.objects.create(name=f'G{index}', slug=f'g{index}')
            ServiceOffering.objects.create(group=group, name=f'O{index}', slug=f'o{index}')
        with CaptureQueriesContext(connection) as queries:
            self.client.get('/api/services/groups/')
        self.assertLessEqual(len(queries), 3)

    def test_offering_detail_includes_form_fields_in_order(self):
        response = self.client.get('/api/services/offerings/custom-terrarium/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([f['key'] for f in response.data['form_fields']], ['size', 'theme', 'depth_cm'])

    def test_inactive_offering_is_hidden(self):
        ServiceOffering.objects.filter(pk=self.offering.pk).update(is_active=False)
        self.assertEqual(self.client.get('/api/services/offerings/custom-terrarium/').status_code, 404)


# ── Quote request ────────────────────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class QuoteRequestTests(ServiceTestCase):
    def test_valid_request_is_saved_normalised_and_snapshotted(self):
        with self.captureOnCommitCallbacks(execute=True):
            quote = self.make_request()
        self.assertEqual(quote.contact_phone, '9876543210')
        self.assertEqual(quote.contact_email, 'asha@example.com')
        self.assertEqual(quote.state_code, 'KA')
        self.assertEqual(quote.status, QuoteStatus.SUBMITTED)
        self.assertEqual(quote.answers['size'], 'Medium')
        self.assertEqual([f['key'] for f in quote.form_snapshot], ['size', 'theme', 'depth_cm'])
        self.assertRegex(quote.reference, r'^JNG-\d{4}-[0-9A-F]{5}$')

    def test_super_admins_are_emailed_with_budget_and_requirements(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_request()
        to_admin = [m for m in mail.outbox if self.admin.email in m.to]
        self.assertEqual(len(to_admin), 1)
        self.assertIn('₹3,000', to_admin[0].subject)
        self.assertIn('Medium', to_admin[0].body)
        self.assertIn('asha@example.com', to_admin[0].body)
        self.assertTrue(any('asha@example.com' in m.to for m in mail.outbox))
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title__startswith='New quote request').exists())

    def test_missing_budget_state_or_required_answer_is_rejected(self):
        for field in ('budget_min', 'budget_max', 'state', 'contact_phone', 'contact_email', 'contact_name'):
            payload = valid_request_payload()
            payload.pop(field)
            response = self.client.post(REQUEST_URL, payload, format='json')
            self.assertEqual(response.status_code, 400, field)
            self.assertIn(field, response.data)
        response = self.client.post(REQUEST_URL, valid_request_payload(answers={}), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('size', response.data['answers'])
        self.assertEqual(QuoteRequest.objects.count(), 0)

    def test_invalid_values_are_rejected(self):
        cases = [
            {'contact_phone': '12345'}, {'contact_email': 'not-an-email'}, {'state': 'Atlantis'},
            {'budget_min': '0'}, {'budget_min': '5000', 'budget_max': '1000'}, {'budget_max': '99999999'},
            {'answers': {'size': 'Huge'}}, {'answers': {'size': 'Small', 'depth_cm': '5'}},
            {'answers': {'size': 'Small', 'unknown': 'x'}}, {'answers': {'size': 'Small', 'theme': ['Space']}},
            {'reference_images': ['http://insecure.example/a.jpg']},
        ]
        for overrides in cases:
            cache.clear()  # rejected attempts still count towards the throttle
            response = self.client.post(REQUEST_URL, valid_request_payload(**overrides), format='json')
            self.assertEqual(response.status_code, 400, overrides)

    def test_request_form_is_throttled(self):
        limit = 30  # settings: 'service_quote': '30/hour'
        statuses = [self.client.post(REQUEST_URL, valid_request_payload(), format='json').status_code for _ in range(limit + 1)]
        self.assertEqual(statuses[:limit], [201] * limit)
        self.assertEqual(statuses[limit], 429)

    @override_settings(FIREBASE_STORAGE_BUCKET='junglyst_test')
    def test_request_accepts_our_photos_and_rejects_outside_links(self):
        ours = 'https://storage.googleapis.com/junglyst_test/dev/assets/service-reference/a.png'
        quote = self.make_request(reference_images=[ours])
        self.assertEqual(quote.reference_images, [ours])
        cache.clear()
        outside = self.client.post(REQUEST_URL, valid_request_payload(reference_images=['https://tracker.example.com/p.png']), format='json')
        self.assertEqual(outside.status_code, 400)
        self.assertIn('reference_images', outside.data)

    def test_honeypot_blocks_bots(self):
        response = self.client.post(REQUEST_URL, valid_request_payload(website='http://spam.example'), format='json')
        self.assertEqual(response.status_code, 400)

    def test_state_restricted_offering_rejects_other_states(self):
        ServiceOffering.objects.filter(pk=self.offering.pk).update(allowed_states=['TN'])
        response = self.client.post(REQUEST_URL, valid_request_payload(), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('state', response.data)
        self.assertEqual(
            self.client.post(REQUEST_URL, valid_request_payload(state='TN'), format='json').status_code, 201,
        )

    def test_signed_in_customer_is_linked(self):
        customer = make_user('asha@example.com')
        self.client.force_authenticate(customer)
        self.assertEqual(self.make_request().user, customer)

    def test_form_edits_do_not_change_old_requests(self):
        quote = self.make_request()
        ServiceFormField.objects.filter(offering=self.offering, key='size').update(label='Changed')
        quote.refresh_from_db()
        self.assertEqual(quote.form_snapshot[0]['label'], 'Size')


# ── Admin + seller quote handling ────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class QuoteWorkflowTests(ServiceTestCase):
    def admin_client(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        return client

    def seller_client(self, seller=None):
        client = APIClient()
        client.force_authenticate(seller or self.seller)
        return client

    def test_admin_assigns_seller_and_seller_is_notified(self):
        quote = self.make_request()
        response = self.admin_client().post(
            f'/api/services/admin/requests/{quote.id}/assign/', {'seller_id': str(self.seller.id)}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        self.assertEqual(quote.assigned_seller, self.seller)
        self.assertEqual(quote.status, QuoteStatus.IN_REVIEW)
        self.assertTrue(AppNotification.objects.filter(user=self.seller, title__contains=quote.reference).exists())

    def test_platform_store_with_admin_role_can_be_assigned_and_use_seller_tab(self):
        store = make_user('store@junglyst.test', role='admin', is_verified_seller=True)
        SellerProfile.objects.create(user=store, store_name='Platform Store', slug='platform-store')
        quote = self.make_request()
        listed = [s['id'] for s in self.admin_client().get('/api/services/admin/sellers/').data]
        self.assertIn(str(store.id), listed)
        response = self.admin_client().post(
            f'/api/services/admin/requests/{quote.id}/assign/', {'seller_id': str(store.id)}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.seller_client(store).get('/api/services/seller/requests/').data['count'], 1)

    def test_unverified_or_inactive_stores_cannot_be_assigned(self):
        unverified = make_seller('unverified@junglyst.test', 'Unverified Studio')
        User.objects.filter(pk=unverified.pk).update(is_verified_seller=False)
        paused = make_seller('paused@junglyst.test', 'Paused Studio')
        SellerProfile.objects.filter(user=paused).update(is_active=False)
        quote = self.make_request()
        listed = [s['id'] for s in self.admin_client().get('/api/services/admin/sellers/').data]
        for store in (unverified, paused):
            self.assertNotIn(str(store.id), listed)
            response = self.admin_client().post(
                f'/api/services/admin/requests/{quote.id}/assign/', {'seller_id': str(store.id)}, format='json',
            )
            self.assertEqual(response.status_code, 400)

    def test_cannot_assign_a_non_seller(self):
        quote = self.make_request()
        buyer = make_user('buyer@example.com')
        response = self.admin_client().post(
            f'/api/services/admin/requests/{quote.id}/assign/', {'seller_id': str(buyer.id)}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_and_seller_can_both_fill_quote_details(self):
        quote = self.make_request()
        quote.assigned_seller = self.seller
        quote.save()
        details = {
            'item_price': '4800.00', 'shipping_fee': '250', 'shape': 'Cube', 'length_cm': '30', 'width_cm': '30',
            'height_cm': '35', 'quote_notes': 'Ready in 10 days',
            'inclusions': [{'label': 'Glass container', 'amount': '1200'}, {'label': 'Live moss'}],
        }
        by_seller = self.seller_client().patch(f'/api/services/seller/requests/{quote.id}/', details, format='json')
        self.assertEqual(by_seller.status_code, 200, by_seller.data)
        self.assertEqual(str(by_seller.data['quote']['total_amount']), '5050.00')
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title__startswith='Quote updated').exists())
        by_admin = self.admin_client().patch(
            f'/api/services/admin/requests/{quote.id}/', {'shipping_fee': '300', 'admin_notes': 'Call first'}, format='json',
        )
        self.assertEqual(by_admin.status_code, 200, by_admin.data)
        self.assertEqual(str(by_admin.data['quote']['total_amount']), '5100.00')
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.IN_REVIEW)
        self.assertEqual(quote.admin_notes, 'Call first')
        actions = list(QuoteEvent.objects.filter(quote_request=quote).values_list('action', flat=True))
        self.assertEqual(actions.count('quote_edited'), 2)

    def test_invalid_quote_figures_are_rejected(self):
        quote = self.priced_request()
        for bad in ({'item_price': '0'}, {'item_price': '-5'}, {'item_price': '12.345'}, {'item_price': '99999999999'},
                    {'shipping_fee': '-1'}, {'length_cm': '0'}, {'height_cm': '5000'},
                    {'inclusions': [{'label': ''}]}):
            response = self.admin_client().patch(f'/api/services/admin/requests/{quote.id}/', bad, format='json')
            self.assertEqual(response.status_code, 400, bad)

    def test_seller_cannot_set_admin_notes_or_see_history(self):
        quote = self.priced_request()
        self.seller_client().patch(
            f'/api/services/seller/requests/{quote.id}/', {'admin_notes': 'hack', 'quote_notes': 'ok'}, format='json',
        )
        quote.refresh_from_db()
        self.assertEqual(quote.admin_notes, '')
        detail = self.seller_client().get(f'/api/services/seller/requests/{quote.id}/')
        self.assertNotIn('admin_notes', detail.data)
        self.assertNotIn('events', detail.data)
        for hidden in ('contact_phone', 'contact_email'):
            self.assertNotIn(hidden, detail.data)
            self.assertNotIn(hidden, self.seller_client().get('/api/services/seller/requests/').data['results'][0])

    def test_seller_only_sees_requests_assigned_to_them(self):
        quote = self.priced_request()
        other = make_seller('other@junglyst.test', 'Other Studio')
        self.assertEqual(self.seller_client(other).get(f'/api/services/seller/requests/{quote.id}/').status_code, 404)
        self.assertEqual(self.seller_client(other).get('/api/services/seller/requests/').data['count'], 0)
        denied = self.seller_client(other).patch(f'/api/services/seller/requests/{quote.id}/', {'quote_notes': 'x'}, format='json')
        self.assertEqual(denied.status_code, 404)

    def test_send_pay_request_requires_seller_and_price(self):
        quote = self.make_request()
        url = f'/api/services/admin/requests/{quote.id}/send-pay-request/'
        self.assertEqual(self.admin_client().post(url).status_code, 400)  # no seller
        quote.assigned_seller = self.seller
        quote.save()
        self.assertEqual(self.admin_client().post(url).status_code, 400)  # no price

    def test_send_pay_request_locks_quote_and_notifies_customer(self):
        customer = make_user('asha@example.com')
        quote = self.priced_request(user=customer)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client().post(f'/api/services/admin/requests/{quote.id}/send-pay-request/')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.AWAITING_PAYMENT)
        self.assertEqual(quote.commission_percent, Decimal('15.00'))
        self.assertTrue(any('5200.00' in m.body and 'asha@example.com' in m.to for m in mail.outbox))
        self.assertTrue(AppNotification.objects.filter(user=customer, title='Your quote is ready').exists())
        # seller can no longer edit; sending twice fails
        denied = self.seller_client().patch(f'/api/services/seller/requests/{quote.id}/', {'quote_notes': 'late'}, format='json')
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(self.admin_client().post(f'/api/services/admin/requests/{quote.id}/send-pay-request/').status_code, 400)

    def test_admin_can_decline_open_request(self):
        quote = self.make_request()
        response = self.admin_client().post(f'/api/services/admin/requests/{quote.id}/decline/', {'reason': 'Out of scope'}, format='json')
        self.assertEqual(response.status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.DECLINED)

    def test_admin_can_record_a_phone_request(self):
        payload = valid_request_payload(offering='custom-terrarium')
        response = self.admin_client().post('/api/services/admin/requests/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(QuoteRequest.objects.filter(reference=response.data['reference']).exists())

    def test_admin_summary_counts_requests_needing_attention(self):
        self.make_request()
        self.make_request()
        self.priced_request()  # in review
        response = self.admin_client().get('/api/services/admin/summary/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {key: response.data[key] for key in ('new_requests', 'in_review', 'awaiting_payment')},
            {'new_requests': 2, 'in_review': 1, 'awaiting_payment': 0},
        )

    def test_admin_queue_filters_and_is_query_bounded(self):
        for _ in range(3):
            self.priced_request()
        self.make_request()
        client = self.admin_client()
        self.assertEqual(client.get('/api/services/admin/requests/?status=in_review').data['count'], 3)
        self.assertEqual(client.get('/api/services/admin/requests/?q=Asha').data['count'], 4)
        with CaptureQueriesContext(connection) as queries:
            client.get('/api/services/admin/requests/')
        self.assertLessEqual(len(queries), 6)


# ── Permissions ──────────────────────────────────────────────────────────────

class PermissionTests(ServiceTestCase):
    ADMIN_URLS = (
        '/api/services/admin/groups/', '/api/services/admin/offerings/', '/api/services/admin/sellers/',
        '/api/services/admin/requests/', '/api/services/admin/summary/',
    )

    def test_admin_endpoints_deny_anonymous_buyers_and_sellers(self):
        buyer = make_user('buyer@example.com')
        for url in self.ADMIN_URLS:
            self.assertEqual(self.client.get(url).status_code, 401, url)
            for user in (buyer, self.seller):
                client = APIClient()
                client.force_authenticate(user)
                self.assertEqual(client.get(url).status_code, 403, url)

    def test_seller_endpoints_deny_buyers_and_anonymous(self):
        buyer = make_user('buyer@example.com')
        self.assertEqual(self.client.get('/api/services/seller/requests/').status_code, 401)
        client = APIClient()
        client.force_authenticate(buyer)
        self.assertEqual(client.get('/api/services/seller/requests/').status_code, 403)

    def test_admin_actions_deny_seller(self):
        quote = self.priced_request()
        client = APIClient()
        client.force_authenticate(self.seller)
        for action in ('assign', 'send-pay-request', 'decline'):
            self.assertEqual(client.post(f'/api/services/admin/requests/{quote.id}/{action}/', {}, format='json').status_code, 403)

    def test_customer_cannot_see_or_pay_someone_elses_quote(self):
        quote = self.priced_request(user=make_user('asha@example.com'))
        stranger = make_user('stranger@example.com')
        client = APIClient()
        client.force_authenticate(stranger)
        self.assertEqual(client.get(f'/api/services/my-requests/{quote.reference}/').status_code, 404)
        self.assertEqual(client.post(f'/api/services/my-requests/{quote.reference}/pay/', {}, format='json').status_code, 404)

    def test_customer_list_returns_every_request_not_just_the_first_page(self):
        customer = make_user('many@example.com')
        for _ in range(23):
            self.make_request(contact_email='many@example.com')
        QuoteRequest.objects.filter(contact_email='many@example.com').update(user=customer)
        client = APIClient()
        client.force_authenticate(customer)
        listing = client.get('/api/services/my-requests/')
        self.assertEqual(listing.status_code, 200)
        self.assertIsInstance(listing.data, list)
        self.assertEqual(len(listing.data), 23)

    def test_customer_does_not_see_quote_figures_before_pay_request(self):
        customer = make_user('asha@example.com')
        quote = self.priced_request(user=customer)
        client = APIClient()
        client.force_authenticate(customer)
        detail = client.get(f'/api/services/my-requests/{quote.reference}/')
        self.assertIsNone(detail.data['quote'])
        self.assertNotIn('assigned_seller', detail.data)
        self.assertNotIn('admin_notes', detail.data)


# ── Pay + fulfilment ─────────────────────────────────────────────────────────

PAY_ADDRESS = {
    'full_name': 'Asha Rao', 'phone': '9876543210', 'address_line1': '12 Lake Road', 'city': 'Bengaluru',
    'state': 'Karnataka', 'pincode': '560001',
}


@override_settings(**TEST_SETTINGS)
class PaymentTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.quote = self.priced_request(user=self.customer)
        self.quote.status = QuoteStatus.AWAITING_PAYMENT
        self.quote.commission_percent = Decimal('15.00')
        self.quote.save()
        self.client.force_authenticate(self.customer)
        self.pay_url = f'/api/services/my-requests/{self.quote.reference}/pay/'

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_pay_creates_service_order_and_seller_suborder(self, gateway):
        response = self.client.post(self.pay_url, PAY_ADDRESS, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.quote.refresh_from_db()
        order = self.quote.order
        self.assertEqual(order.order_type, OrderType.SERVICE)
        self.assertEqual(order.total_amount, Decimal('5200.00'))
        self.assertEqual(order.shipping_address['pincode'], '560001')
        self.assertEqual(order.shipping_address['state'], 'Karnataka')
        self.assertFalse(order.sub_orders.exists())  # the seller's shipment appears only once paid
        with self.captureOnCommitCallbacks(execute=True):
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()
        sub_order = order.sub_orders.get()
        self.assertEqual(sub_order.seller, self.seller)
        # 5000 item price − 15% commission + 200 shipping
        self.assertEqual(sub_order.seller_total, Decimal('4450.00'))
        self.assertEqual(order.items.get().product_name, 'Custom terrarium')
        self.assertEqual(order.order_number, self.quote.reference)  # one ID from request to delivery
        self.assertEqual(sub_order.sub_order_number, f'{self.quote.reference}-A')
        gateway.assert_called_once()

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_order_gets_a_fresh_number_if_the_reference_is_taken(self, gateway):
        Order.objects.create(order_number=self.quote.reference, shipping_address={}, subtotal=1, total_amount=1)
        self.assertEqual(self.client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 201)
        self.quote.refresh_from_db()
        self.assertNotEqual(self.quote.order.order_number, self.quote.reference)
        self.assertTrue(self.quote.order.order_number.startswith('JNG-'))

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_client_cannot_change_the_amount(self, gateway):
        self.client.post(self.pay_url, {**PAY_ADDRESS, 'total_amount': '1', 'item_price': '1'}, format='json')
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.order.total_amount, Decimal('5200.00'))

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_address_validation(self, gateway):
        cases = [
            {'pincode': '12345'}, {'pincode': '056001'}, {'pincode': '110001'},  # wrong state
            {'pincode': '744101'}, {'phone': '123'}, {'full_name': ''}, {'address_line1': 'abc'},
            {'city': ''}, {'state': 'Nowhere'},
        ]
        for overrides in cases:
            response = self.client.post(self.pay_url, {**PAY_ADDRESS, **overrides}, format='json')
            self.assertEqual(response.status_code, 400, overrides)
        gateway.assert_not_called()
        self.quote.refresh_from_db()
        self.assertIsNone(self.quote.order)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_seller_who_does_not_ship_to_state_blocks_payment(self, gateway):
        SellerExcludedState.objects.create(seller=self.seller.seller_profile, state_code='KA')
        response = self.client.post(self.pay_url, PAY_ADDRESS, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('state', response.data)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_cannot_pay_before_pay_request_or_twice(self, gateway):
        self.quote.status = QuoteStatus.IN_REVIEW
        self.quote.save()
        self.assertEqual(self.client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 400)
        self.quote.status = QuoteStatus.AWAITING_PAYMENT
        self.quote.save()
        self.assertEqual(self.client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 201)
        # retry before any gateway order was opened reuses the same order
        self.assertEqual(self.client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 201)
        self.assertEqual(Order.objects.filter(order_type=OrderType.SERVICE).count(), 1)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_guest_request_is_claimed_only_by_matching_email(self, gateway):
        QuoteRequest.objects.filter(pk=self.quote.pk).update(user=None)
        stranger = make_user('stranger@example.com')
        client = APIClient()
        client.force_authenticate(stranger)
        self.assertEqual(client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 404)
        self.assertEqual(self.client.post(self.pay_url, PAY_ADDRESS, format='json').status_code, 201)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.user, self.customer)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_paid_order_marks_request_paid_and_notifies_seller(self, gateway):
        self.client.post(self.pay_url, PAY_ADDRESS, format='json')
        self.quote.refresh_from_db()
        order = self.quote.order
        with self.captureOnCommitCallbacks(execute=True):
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.PAID)
        self.assertIsNotNone(self.quote.paid_at)
        self.assertTrue(AppNotification.objects.filter(user=self.seller, title__startswith='Custom order paid').exists())
        # fulfilment handler is idempotent
        ServiceOrderHandler().on_paid(order)
        self.assertEqual(AppNotification.objects.filter(user=self.seller, title__startswith='Custom order paid').count(), 1)
        # fields are locked after payment
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        locked = admin_client.patch(f'/api/services/admin/requests/{self.quote.id}/', {'item_price': '1'}, format='json')
        self.assertEqual(locked.status_code, 400)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_my_orders_summary_describes_the_service(self, gateway):
        self.client.post(self.pay_url, PAY_ADDRESS, format='json')
        self.quote.refresh_from_db()
        summary = ServiceOrderHandler().summarize(Order.objects.select_related('service_quote__offering').get(pk=self.quote.order_id))
        self.assertEqual(summary['title'], 'Custom terrarium')
        self.assertEqual(summary['reference'], self.quote.reference)


# ── Admin catalogue configuration ────────────────────────────────────────────

class AdminCatalogueTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_admin_creates_group_and_offering_with_form(self):
        group = self.client.post('/api/services/admin/groups/', {
            'name': 'Maintenance', 'slug': 'maintenance', 'show_on_home': True, 'home_sort_order': 2,
        }, format='json')
        self.assertEqual(group.status_code, 201, group.data)
        offering = self.client.post('/api/services/admin/offerings/', {
            'group': group.data['id'], 'name': 'Monthly care', 'slug': 'monthly-care', 'allowed_states': ['KA', 'KA', 'TN'],
            'form_fields': [
                {'key': 'tank', 'label': 'Tank size', 'field_type': 'select', 'choices': ['S', 'M'], 'required': True},
                {'key': 'notes', 'label': 'Notes', 'field_type': 'textarea'},
            ],
        }, format='json')
        self.assertEqual(offering.status_code, 201, offering.data)
        self.assertEqual(offering.data['allowed_states'], ['KA', 'TN'])
        self.assertEqual(len(offering.data['form_fields']), 2)
        public = self.client.get('/api/services/offerings/monthly-care/')
        self.assertEqual(public.status_code, 200)

    def test_broken_form_definitions_are_rejected(self):
        base = {'group': self.group.id, 'name': 'X', 'slug': 'x'}
        for fields in (
            [{'key': 'a', 'label': 'A', 'field_type': 'select', 'choices': []}],
            [{'key': 'a', 'label': 'A', 'field_type': 'text'}, {'key': 'a', 'label': 'B', 'field_type': 'text'}],
            [{'key': 'Bad Key', 'label': 'A', 'field_type': 'text'}],
        ):
            response = self.client.post('/api/services/admin/offerings/', {**base, 'form_fields': fields}, format='json')
            self.assertEqual(response.status_code, 400, fields)
        bad_states = self.client.post('/api/services/admin/offerings/', {**base, 'allowed_states': ['ZZ']}, format='json')
        self.assertEqual(bad_states.status_code, 400)
        over = self.client.post('/api/services/admin/offerings/', {**base, 'commission_percent': '150'}, format='json')
        self.assertEqual(over.status_code, 400)

    def test_updating_form_replaces_questions_but_keeps_old_requests_intact(self):
        quote = ServiceTestCase.make_request(self)
        response = self.client.patch(f'/api/services/admin/offerings/{self.offering.id}/', {
            'form_fields': [{'key': 'colour', 'label': 'Colour', 'field_type': 'text'}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([f['key'] for f in response.data['form_fields']], ['colour'])
        quote.refresh_from_db()
        self.assertEqual(quote.answers['size'], 'Medium')


class SeedCommandTests(TestCase):
    def test_seed_creates_group_offering_and_questions_once(self):
        call_command('seed_services')
        group = ServiceGroup.objects.get(slug='terrariums')
        offering = ServiceOffering.objects.get(slug='custom-terrarium')
        self.assertEqual(offering.group, group)
        question_count = offering.form_fields.count()
        self.assertGreater(question_count, 0)
        # re-running keeps admin edits instead of overwriting them
        ServiceOffering.objects.filter(pk=offering.pk).update(name='Renamed by admin')
        call_command('seed_services')
        offering.refresh_from_db()
        self.assertEqual(offering.name, 'Renamed by admin')
        self.assertEqual(offering.form_fields.count(), question_count)
        self.assertEqual(ServiceGroup.objects.count(), 1)


# ── Additional payments ──────────────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class AdditionalChargeTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.quote = self.priced_request(user=self.customer)
        self.quote.status = QuoteStatus.AWAITING_PAYMENT
        self.quote.commission_percent = Decimal('15.00')
        self.quote.save()
        self.customer_client = APIClient()
        self.customer_client.force_authenticate(self.customer)
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            self.customer_client.post(f'/api/services/my-requests/{self.quote.reference}/pay/', PAY_ADDRESS, format='json')
        self.quote.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            order = self.quote.order
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()
        self.quote.refresh_from_db()
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.seller_client = APIClient()
        self.seller_client.force_authenticate(self.seller)

    def request_charge(self, client, base, amount='1000', description='Larger glass container'):
        return client.post(f'{base}/{self.quote.id}/charges/', {'description': description, 'amount': amount}, format='json')

    def admin_charge(self, **kwargs):
        response = self.request_charge(self.admin_client, '/api/services/admin/requests', **kwargs)
        self.assertEqual(response.status_code, 200, response.data)
        return AdditionalCharge.objects.filter(quote_request=self.quote).latest('created_at')

    def pay_charge(self, charge, client=None):
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            return (client or self.customer_client).post(
                f'/api/services/my-requests/{self.quote.reference}/charges/{charge.id}/pay/', format='json',
            )

    def capture(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            order.refresh_from_db()
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()

    def test_admin_requests_extra_payment_and_customer_is_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            charge = self.admin_charge()
        self.assertEqual(charge.status, ChargeStatus.REQUESTED)
        self.assertEqual(charge.amount, Decimal('1000'))
        self.assertEqual(charge.seller_payout, Decimal('850.00'))
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Additional payment requested').exists())
        self.assertTrue(any('Larger glass container' in m.body and 'asha@example.com' in m.to for m in mail.outbox))
        mine = self.customer_client.get(f'/api/services/my-requests/{self.quote.reference}/')
        self.assertEqual(mine.data['additional_charges'][0]['description'], 'Larger glass container')

    def test_assigned_seller_can_request_and_admin_is_notified(self):
        response = self.request_charge(self.seller_client, '/api/services/seller/requests')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['additional_charges']), 1)
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title__startswith='Additional payment requested').exists())

    def test_other_seller_buyer_and_anonymous_cannot_request(self):
        other = make_seller('other@junglyst.test', 'Other Studio')
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(self.request_charge(client, '/api/services/seller/requests').status_code, 404)
        client.force_authenticate(self.customer)
        self.assertEqual(self.request_charge(client, '/api/services/seller/requests').status_code, 403)
        self.assertEqual(self.request_charge(client, '/api/services/admin/requests').status_code, 403)
        self.assertEqual(self.request_charge(APIClient(), '/api/services/admin/requests').status_code, 401)
        self.assertEqual(AdditionalCharge.objects.count(), 0)

    def test_extra_payment_needs_first_payment_and_valid_values(self):
        self.quote.status = QuoteStatus.AWAITING_PAYMENT
        self.quote.save()
        self.assertEqual(self.request_charge(self.admin_client, '/api/services/admin/requests').status_code, 400)
        self.quote.status = QuoteStatus.PAID
        self.quote.save()
        bad_values = (('0', 'x extra'), ('-5', 'x extra'), ('1.234', 'x extra'), ('99999999999', 'x extra'),
                      ('500', ''), ('500', 'ab'))
        for amount, description in bad_values:
            response = self.request_charge(self.admin_client, '/api/services/admin/requests', amount=amount, description=description)
            self.assertEqual(response.status_code, 400, (amount, description))
        self.assertEqual(AdditionalCharge.objects.count(), 0)

    def test_customer_pays_extra_through_a_separate_order(self):
        charge = self.admin_charge()
        response = self.pay_charge(charge)
        self.assertEqual(response.status_code, 201, response.data)
        charge.refresh_from_db()
        self.assertEqual(charge.order.order_type, OrderType.SERVICE)
        self.assertEqual(charge.order.total_amount, Decimal('1000'))
        self.assertEqual(charge.order.shipping_address['pincode'], '560001')
        self.assertNotEqual(charge.order_id, self.quote.order_id)
        self.assertEqual(charge.order.order_number, f'{self.quote.reference}-E1')
        self.assertEqual(self.pay_charge(charge).status_code, 201)  # retry reuses the order
        self.assertEqual(Order.objects.filter(additional_charge__isnull=False).count(), 1)

    def test_paid_extra_adds_to_seller_payout_and_pending_settlement(self):
        charge = self.admin_charge()
        self.pay_charge(charge)
        charge.refresh_from_db()
        sub_order = self.quote.order.sub_orders.get()
        SellerSettlement.objects.create(sub_order=sub_order, seller=self.seller, amount=sub_order.seller_total)
        self.capture(charge.order)
        charge.refresh_from_db()
        sub_order.refresh_from_db()
        self.assertEqual(charge.status, ChargeStatus.PAID)
        self.assertEqual(sub_order.seller_total, Decimal('5300.00'))  # 4450 + 850
        self.assertEqual(sub_order.settlement.amount, Decimal('5300.00'))
        self.assertTrue(AppNotification.objects.filter(user=self.seller, title__startswith='Additional payment received').exists())
        ServiceOrderHandler().on_paid(charge.order)  # idempotent
        sub_order.refresh_from_db()
        self.assertEqual(sub_order.seller_total, Decimal('5300.00'))

    def test_extra_after_settlement_paid_asks_admin_to_transfer(self):
        charge = self.admin_charge()
        self.pay_charge(charge)
        charge.refresh_from_db()
        sub_order = self.quote.order.sub_orders.get()
        settlement = SellerSettlement.objects.create(sub_order=sub_order, seller=self.seller, amount=sub_order.seller_total)
        settlement.mark_settled(self.admin)
        self.capture(charge.order)
        settlement.refresh_from_db()
        self.assertEqual(settlement.amount, Decimal('4450.00'))
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title__startswith='Pay the seller extra').exists())

    def test_only_the_owner_can_pay_and_cancelled_requests_cannot_be_paid(self):
        charge = self.admin_charge()
        stranger = make_user('stranger@example.com')
        client = APIClient()
        client.force_authenticate(stranger)
        self.assertEqual(self.pay_charge(charge, client).status_code, 404)
        cancelled = self.admin_client.post(f'/api/services/admin/requests/{self.quote.id}/charges/{charge.id}/cancel/')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(self.pay_charge(charge).status_code, 400)

    def test_seller_can_cancel_but_not_a_paid_request(self):
        paid_charge = self.admin_charge()
        self.pay_charge(paid_charge)
        paid_charge.refresh_from_db()
        self.capture(paid_charge.order)
        url = f'/api/services/seller/requests/{self.quote.id}/charges/{paid_charge.id}/cancel/'
        self.assertEqual(self.seller_client.post(url).status_code, 400)
        open_charge = self.admin_charge(amount='200', description='Extra moss')
        url = f'/api/services/seller/requests/{self.quote.id}/charges/{open_charge.id}/cancel/'
        self.assertEqual(self.seller_client.post(url).status_code, 200)

    def test_charge_order_summary_and_staff_detail(self):
        charge = self.admin_charge()
        self.pay_charge(charge)
        charge.refresh_from_db()
        order = Order.objects.select_related('additional_charge__quote_request').get(pk=charge.order_id)
        self.assertEqual(ServiceOrderHandler().summarize(order)['title'], 'Additional payment')
        detail = self.admin_client.get(f'/api/services/admin/requests/{self.quote.id}/')
        self.assertEqual(len(detail.data['additional_charges']), 1)
        seller_detail = self.seller_client.get(f'/api/services/seller/requests/{self.quote.id}/')
        self.assertEqual(len(seller_detail.data['additional_charges']), 1)


# ── Delivery and list sections ───────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class DeliveryAndSectionTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def paid_quote(self):
        quote = self.priced_request(user=self.customer)
        quote.status = QuoteStatus.AWAITING_PAYMENT
        quote.commission_percent = Decimal('15.00')
        quote.save()
        client = APIClient()
        client.force_authenticate(self.customer)
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            client.post(f'/api/services/my-requests/{quote.reference}/pay/', PAY_ADDRESS, format='json')
        quote.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            order = quote.order
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()
        quote.refresh_from_db()
        return quote

    def test_admin_marks_paid_order_delivered(self):
        quote = self.paid_quote()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/mark-delivered/')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        sub_order = quote.order.sub_orders.get()
        self.assertEqual(quote.status, QuoteStatus.DELIVERED)
        self.assertEqual(sub_order.status, 'delivered')
        self.assertEqual(sub_order.settlement.amount, sub_order.seller_total)
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Your custom order was delivered').exists())
        self.assertEqual(QuoteEvent.objects.filter(quote_request=quote, action='delivered').count(), 1)

    def test_only_paid_orders_can_be_marked_delivered_and_only_by_admin(self):
        unpaid = self.priced_request()
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{unpaid.id}/mark-delivered/').status_code, 400)
        quote = self.paid_quote()
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        self.assertEqual(seller_client.post(f'/api/services/admin/requests/{quote.id}/mark-delivered/').status_code, 403)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.PAID)

    def test_sub_order_delivered_by_seller_or_courier_closes_the_request(self):
        quote = self.paid_quote()
        sub_order = quote.order.sub_orders.get()
        with self.captureOnCommitCallbacks(execute=True):
            sub_order.status = 'delivered'
            sub_order.save()
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.DELIVERED)
        with self.captureOnCommitCallbacks(execute=True):
            sub_order.save()  # saving again changes nothing
        self.assertEqual(QuoteEvent.objects.filter(quote_request=quote, action='delivered').count(), 1)

    def test_admin_list_is_split_into_sections(self):
        active = self.make_request()
        declined = self.make_request()
        QuoteRequest.objects.filter(pk=declined.pk).update(status=QuoteStatus.DECLINED)
        delivered = self.make_request()
        QuoteRequest.objects.filter(pk=delivered.pk).update(status=QuoteStatus.DELIVERED)
        ids = lambda response: {row['id'] for row in response.data['results']}
        self.assertEqual(ids(self.admin_client.get('/api/services/admin/requests/')), {str(active.id)})
        self.assertEqual(ids(self.admin_client.get('/api/services/admin/requests/?section=closed')), {str(declined.id)})
        self.assertEqual(ids(self.admin_client.get('/api/services/admin/requests/?section=delivered')), {str(delivered.id)})
        summary = self.admin_client.get('/api/services/admin/summary/').data
        self.assertEqual((summary['section_active'], summary['section_delivered'], summary['section_closed']), (1, 1, 1))

    def test_seller_never_sees_closed_requests(self):
        open_quote = self.priced_request()
        declined = self.priced_request()
        QuoteRequest.objects.filter(pk=declined.pk).update(status=QuoteStatus.DECLINED)
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        for url in ('/api/services/seller/requests/', '/api/services/seller/requests/?section=closed'):
            listed = {row['id'] for row in seller_client.get(url).data['results']}
            self.assertEqual(listed, {str(open_quote.id)}, url)


# ── Delivery states ──────────────────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class DeliveryStateTests(ServiceTestCase):
    def test_group_states_apply_to_its_services_and_are_shown(self):
        ServiceGroup.objects.filter(pk=self.group.pk).update(allowed_states=['KA', 'TN'])
        detail = self.client.get('/api/services/offerings/custom-terrarium/').data
        self.assertEqual(detail['delivery_states'], ['KA', 'TN'])
        self.assertEqual(detail['delivery_state_names'], ['Karnataka', 'Tamil Nadu'])
        card = self.client.get('/api/services/groups/').data[0]['offerings'][0]
        self.assertEqual(card['delivery_state_names'], ['Karnataka', 'Tamil Nadu'])
        rejected = self.client.post(REQUEST_URL, valid_request_payload(state='MH'), format='json')
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('Karnataka, Tamil Nadu', str(rejected.data['state']))
        self.assertEqual(self.client.post(REQUEST_URL, valid_request_payload(state='TN'), format='json').status_code, 201)

    def test_service_states_override_the_group(self):
        ServiceGroup.objects.filter(pk=self.group.pk).update(allowed_states=['KA', 'TN'])
        ServiceOffering.objects.filter(pk=self.offering.pk).update(allowed_states=['MH'])
        self.assertEqual(self.client.post(REQUEST_URL, valid_request_payload(state='KA'), format='json').status_code, 400)
        self.assertEqual(self.client.post(REQUEST_URL, valid_request_payload(state='MH'), format='json').status_code, 201)

    def test_no_states_configured_means_all_of_india(self):
        detail = self.client.get('/api/services/offerings/custom-terrarium/').data
        self.assertEqual(detail['delivery_states'], [])
        self.assertEqual(self.client.post(REQUEST_URL, valid_request_payload(state='AS'), format='json').status_code, 201)

    @patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY)
    def test_delivery_address_must_be_in_a_delivery_state(self, gateway):
        customer = make_user('asha@example.com')
        quote = self.priced_request(user=customer)
        quote.status = QuoteStatus.AWAITING_PAYMENT
        quote.commission_percent = Decimal('15.00')
        quote.save()
        ServiceGroup.objects.filter(pk=self.group.pk).update(allowed_states=['TN'])
        client = APIClient()
        client.force_authenticate(customer)
        url = f'/api/services/my-requests/{quote.reference}/pay/'
        response = client.post(url, PAY_ADDRESS, format='json')  # Karnataka address
        self.assertEqual(response.status_code, 400)
        self.assertIn('Tamil Nadu', str(response.data['state']))
        mine = client.get(f'/api/services/my-requests/{quote.reference}/').data
        self.assertEqual(mine['delivery_states'], ['TN'])
        chennai = {**PAY_ADDRESS, 'city': 'Chennai', 'state': 'Tamil Nadu', 'pincode': '600001'}
        self.assertEqual(client.post(url, chennai, format='json').status_code, 201)

    def test_admin_configures_group_states(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        url = f'/api/services/admin/groups/{self.group.id}/'
        ok = client.patch(url, {'allowed_states': ['KA', 'KA', 'TN']}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(ok.data['allowed_states'], ['KA', 'TN'])
        self.assertEqual(client.patch(url, {'allowed_states': ['ZZ']}, format='json').status_code, 400)
        self.assertEqual(client.patch(url, {'allowed_states': 'KA'}, format='json').status_code, 400)


# ── Pay-by deadline, cancellation, reference uploads ─────────────────────────

def _png_bytes():
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (4, 4), (10, 120, 80)).save(buffer, format='PNG')
    return buffer.getvalue()


@override_settings(**TEST_SETTINGS)
class PayByAndCancelTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.customer_client = APIClient()
        self.customer_client.force_authenticate(self.customer)

    def sent_quote(self):
        quote = self.priced_request(user=self.customer)
        response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/send-pay-request/')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        return quote

    def pay(self, quote):
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            return self.customer_client.post(f'/api/services/my-requests/{quote.reference}/pay/', PAY_ADDRESS, format='json')

    def capture(self, quote):
        quote.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            order = quote.order
            order.is_paid = True
            order.payment_status = PaymentStatus.COMPLETED  # as the gateway capture sets it
            order.status = OrderStatus.CONFIRMED
            order.save()
        quote.refresh_from_db()

    def test_pay_request_gives_three_days(self):
        quote = self.sent_quote()
        remaining = quote.pay_by - timezone.now()
        self.assertTrue(timedelta(days=2, hours=23) < remaining <= timedelta(days=3))
        self.assertIsNotNone(self.customer_client.get(f'/api/services/my-requests/{quote.reference}/').data['pay_by'])

    def test_overdue_quotes_expire_and_cannot_be_paid(self):
        quote = self.sent_quote()
        QuoteRequest.objects.filter(pk=quote.pk).update(pay_by=timezone.now() - timedelta(minutes=1))
        response = self.pay(quote)
        self.assertEqual(response.status_code, 400)
        self.assertIn('expired', response.data['error'])
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.EXPIRED)
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Your quote has expired').exists())

    def test_expiry_task_skips_quotes_still_in_time_and_paid_ones(self):
        on_time = self.sent_quote()
        late = self.sent_quote()
        QuoteRequest.objects.filter(pk=late.pk).update(pay_by=timezone.now() - timedelta(hours=1))
        self.assertEqual(expire_overdue_pay_requests(), 1)
        on_time.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual((on_time.status, late.status), (QuoteStatus.AWAITING_PAYMENT, QuoteStatus.EXPIRED))
        self.assertEqual(expire_overdue_pay_requests(), 0)

    def test_admin_renews_an_expired_quote(self):
        quote = self.sent_quote()
        QuoteRequest.objects.filter(pk=quote.pk).update(status=QuoteStatus.EXPIRED, pay_by=timezone.now() - timedelta(days=1))
        response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/renew/')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.AWAITING_PAYMENT)
        self.assertGreater(quote.pay_by, timezone.now() + timedelta(days=2))
        self.assertEqual(self.pay(quote).status_code, 201)

    def test_admin_cancels_a_paid_order_and_refund_is_manual(self):
        quote = self.sent_quote()
        self.pay(quote)
        self.capture(quote)
        sub_order = quote.order.sub_orders.get()
        SellerSettlement.objects.create(sub_order=sub_order, seller=self.seller, amount=sub_order.seller_total)
        response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/cancel/', {'reason': 'Out of stock'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        sub_order.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.CANCELLED)
        self.assertEqual(quote.cancel_reason, 'Out of stock')
        self.assertEqual(sub_order.status, 'cancelled')
        self.assertEqual(Order.objects.get(pk=quote.order_id).status, OrderStatus.CANCELLED)
        self.assertFalse(SellerSettlement.objects.filter(sub_order=sub_order).exists())
        event = QuoteEvent.objects.get(quote_request=quote, action='cancelled')
        self.assertEqual(event.changes['refund_due'], '5200.00')
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Your custom order was cancelled').exists())
        self.assertTrue(AppNotification.objects.filter(user=self.seller, title__startswith='Custom order cancelled').exists())

    def test_lists_show_the_final_amount_once_paid_including_paid_extras(self):
        quote = self.sent_quote()
        admin_list = lambda: next(r for r in self.admin_client.get('/api/services/admin/requests/').data['results'] if r['id'] == str(quote.id))
        self.assertIsNone(admin_list()['final_amount'])  # unpaid: only the quote amount exists
        self.pay(quote)
        self.capture(quote)
        self.assertEqual(str(admin_list()['final_amount']), '5200.00')
        self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/', {'description': 'Moss', 'amount': '300'}, format='json')
        self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/', {'description': 'Unpaid extra', 'amount': '999'}, format='json')
        first = AdditionalCharge.objects.filter(quote_request=quote).earliest('created_at')
        AdditionalCharge.objects.filter(pk=first.pk).update(status=ChargeStatus.PAID)
        self.assertEqual(str(admin_list()['final_amount']), '5500.00')  # 5200 + the 300 that was paid, not the 999 asked
        mine = self.customer_client.get(f'/api/services/my-requests/{quote.reference}/').data
        self.assertEqual(str(mine['final_amount']), '5500.00')
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        self.assertEqual(str(seller_client.get(f'/api/services/seller/requests/{quote.id}/').data['final_amount']), '5500.00')

    def test_paid_custom_order_shows_in_regular_admin_and_seller_order_lists(self):
        quote = self.sent_quote()
        self.pay(quote)
        self.capture(quote)
        dashboard = self.admin_client.get('/api/analytics/super-admin/dashboard/').data
        rows = [row for row in dashboard['orders']['all'] if row['master_order_number'] == quote.reference]
        self.assertTrue(rows, 'paid custom order missing from the super admin order list')
        self.assertEqual(rows[0]['order_type'], 'service')
        self.assertEqual(dashboard['overall_analytics']['custom_orders'], 1)
        status_manager = self.admin_client.get('/api/orders/admin/sub-orders/').data
        self.assertIn(f'{quote.reference}-A', [row['sub_order_number'] for row in status_manager])
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        seller_orders = seller_client.get('/api/orders/seller/sub-orders/')
        self.assertEqual(seller_orders.status_code, 200)
        self.assertIn(f'{quote.reference}-A', str(seller_orders.data))

    def test_cannot_cancel_delivered_or_by_non_admin(self):
        quote = self.sent_quote()
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        self.assertEqual(seller_client.post(f'/api/services/admin/requests/{quote.id}/cancel/').status_code, 403)
        self.pay(quote)
        self.capture(quote)
        quote.order.sub_orders.update(status='delivered')
        response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/cancel/')
        self.assertEqual(response.status_code, 400)

    def test_cancelling_an_unpaid_quote_closes_it(self):
        quote = self.sent_quote()
        response = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/cancel/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.CANCELLED)
        self.assertEqual(self.pay(quote).status_code, 400)
        listed = {row['id'] for row in self.admin_client.get('/api/services/admin/requests/?section=closed').data['results']}
        self.assertIn(str(quote.id), listed)


@override_settings(**TEST_SETTINGS)
class ReferenceUploadTests(ServiceTestCase):
    URL = '/api/services/uploads/'

    def upload(self, content, name='photo.png', content_type='image/png'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(self.URL, {'image': SimpleUploadedFile(name, content, content_type=content_type)}, format='multipart')

    @patch('core.storage.upload_to_firebase', return_value='https://storage.example.com/ref.png')
    def test_guest_can_upload_a_real_image(self, uploader):
        response = self.upload(_png_bytes(), name='holiday.JPG', content_type='image/jpeg')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['url'], 'https://storage.example.com/ref.png')
        stored = uploader.call_args[0][0]
        self.assertEqual((stored.name, stored.content_type), ('reference.png', 'image/png'))  # from the bytes, not the name

    @patch('core.storage.upload_to_firebase', return_value='https://storage.example.com/x.png')
    def test_rejects_non_images_and_large_files(self, uploader):
        self.assertEqual(self.upload(b'<script>alert(1)</script>', name='evil.png').status_code, 400)
        self.assertEqual(self.client.post(self.URL, {}, format='multipart').status_code, 400)
        big = _png_bytes() + b'0' * (5 * 1024 * 1024)
        self.assertEqual(self.upload(big).status_code, 400)
        uploader.assert_not_called()

    @patch('core.storage.upload_to_firebase', side_effect=RuntimeError('bucket down'))
    def test_storage_failure_is_reported_without_details(self, uploader):
        response = self.upload(_png_bytes())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('bucket', response.data['error'])


# ── Payment retries and late / duplicate captures ────────────────────────────

_rzp_counter = {'n': 0}


def _fake_razorpay_order(receipt, amount_inr, currency='INR'):
    _rzp_counter['n'] += 1
    return {'id': f'order_test_{_rzp_counter["n"]}'}


@override_settings(**TEST_SETTINGS)
@patch('payments.checkout.create_razorpay_order', side_effect=_fake_razorpay_order)
class PaymentAttemptTests(ServiceTestCase):
    """Uses the real gateway start (one Payment per Order), with only Razorpay's API faked."""

    def setUp(self):
        super().setUp()
        from payments.models import PaymentGatewaySettings
        settings_row = PaymentGatewaySettings.get_solo()
        settings_row.active_gateway = PaymentGateway.RAZORPAY
        settings_row.save()
        self.customer = make_user('asha@example.com')
        self.quote = self.priced_request(user=self.customer)
        self.quote.status = QuoteStatus.AWAITING_PAYMENT
        self.quote.commission_percent = Decimal('15.00')
        self.quote.pay_by = timezone.now() + timedelta(days=3)
        self.quote.save()
        self.client.force_authenticate(self.customer)
        self.pay_url = f'/api/services/my-requests/{self.quote.reference}/pay/'

    def pay(self):
        cache.clear()
        return self.client.post(self.pay_url, PAY_ADDRESS, format='json')

    def capture(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            order.refresh_from_db()
            order.is_paid = True
            order.payment_status = PaymentStatus.COMPLETED
            order.status = OrderStatus.CONFIRMED
            order.save()

    def test_retry_after_closing_the_gateway_creates_a_new_order_instead_of_crashing(self, _rzp):
        self.assertEqual(self.pay().status_code, 201)
        first = Order.objects.get(order_number=self.quote.reference)
        retry = self.pay()
        self.assertEqual(retry.status_code, 201, retry.data)
        self.quote.refresh_from_db()
        self.assertNotEqual(self.quote.order_id, first.id)
        self.assertEqual(self.quote.order.order_number, f'{self.quote.reference}-2')
        self.assertEqual(self.quote.payment_attempts.count(), 2)

    def test_order_cancelled_from_my_orders_is_not_reused(self, _rzp):
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            self.pay()
            self.quote.refresh_from_db()
            first = self.quote.order
            self.assertEqual(self.client.post(f'/api/orders/{first.id}/cancel/').status_code, 200)
            self.assertEqual(self.pay().status_code, 201)
        self.quote.refresh_from_db()
        self.assertNotEqual(self.quote.order_id, first.id)
        self.assertEqual(self.quote.status, QuoteStatus.AWAITING_PAYMENT)

    def test_seller_shipment_is_created_only_when_paid(self, _rzp):
        self.pay()
        self.quote.refresh_from_db()
        order = self.quote.order
        self.assertFalse(order.sub_orders.exists())
        self.capture(order)
        sub_order = order.sub_orders.get()
        self.assertEqual((sub_order.status, sub_order.seller_total), ('placed', Decimal('4450.00')))
        self.assertEqual(order.items.get().sub_order, sub_order)

    def test_customer_sees_paid_custom_order_in_my_orders_and_its_detail(self, _rzp):
        self.pay()
        self.quote.refresh_from_db()
        order = self.quote.order
        self.capture(order)
        listing = self.client.get('/api/orders/')
        self.assertEqual(listing.status_code, 200)
        self.assertIn(order.order_number, str(listing.data))
        detail = self.client.get(f'/api/orders/{order.id}/')
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data['booking']['title'], 'Custom terrarium')

    def test_late_capture_on_an_older_attempt_still_counts(self, _rzp):
        self.pay()
        first = Order.objects.get(order_number=self.quote.reference)
        self.pay()  # customer retried; first gateway order completes anyway
        self.capture(first)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.PAID)
        self.assertEqual(self.quote.order_id, first.id)
        self.assertTrue(first.sub_orders.exists())

    def test_second_capture_is_flagged_for_refund_not_double_counted(self, _rzp):
        self.pay()
        first = Order.objects.get(order_number=self.quote.reference)
        self.pay()
        second = Order.objects.get(order_number=f'{self.quote.reference}-2')
        self.capture(first)
        self.capture(second)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.order_id, first.id)
        self.assertFalse(second.sub_orders.exists())
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title=f'Refund needed: {self.quote.reference}').exists())

    def test_delivered_order_stays_delivered_when_saved_again(self, _rzp):
        self.pay()
        self.quote.refresh_from_db()
        order = self.quote.order
        self.capture(order)
        with self.captureOnCommitCallbacks(execute=True):
            sub_order = order.sub_orders.get()
            sub_order.status = 'delivered'
            sub_order.save()
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DELIVERED)
        with self.captureOnCommitCallbacks(execute=True):
            order.refresh_from_db()
            order.status = OrderStatus.DELIVERED
            order.save()  # e.g. a status update or payment reconcile
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DELIVERED)

    def test_payment_after_cancellation_keeps_it_cancelled_and_asks_for_refund(self, _rzp):
        self.pay()
        self.quote.refresh_from_db()
        order = self.quote.order
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        self.assertEqual(admin_client.post(f'/api/services/admin/requests/{self.quote.id}/cancel/', {}, format='json').status_code, 200)
        self.capture(order)
        self.quote.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.CANCELLED)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertFalse(order.sub_orders.exists())
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title=f'Refund needed: {self.quote.reference}').exists())

    def test_extra_payment_retry_and_payment_after_cancel(self, _rzp):
        self.pay()
        self.quote.refresh_from_db()
        self.capture(self.quote.order)
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        admin_client.post(f'/api/services/admin/requests/{self.quote.id}/charges/', {'description': 'Bigger glass', 'amount': '900'}, format='json')
        charge = AdditionalCharge.objects.get(quote_request=self.quote)
        url = f'/api/services/my-requests/{self.quote.reference}/charges/{charge.id}/pay/'
        cache.clear()
        self.assertEqual(self.client.post(url).status_code, 201)
        cache.clear()
        self.assertEqual(self.client.post(url).status_code, 201)  # retry: new order, no crash
        charge.refresh_from_db()
        self.assertEqual(charge.order.order_number, f'{self.quote.reference}-E2')
        admin_client.post(f'/api/services/admin/requests/{self.quote.id}/cancel/', {}, format='json')
        self.capture(charge.order)
        charge.refresh_from_db()
        sub_order = self.quote.order.sub_orders.get()
        self.assertNotEqual(charge.status, ChargeStatus.PAID)
        self.assertEqual(sub_order.seller_total, Decimal('4450.00'))  # seller not credited


# ── Customer cancellation, reminders, repricing, extra payouts ───────────────

@override_settings(**TEST_SETTINGS)
class Round9Tests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.customer_client = APIClient()
        self.customer_client.force_authenticate(self.customer)

    def sent_quote(self):
        quote = self.priced_request(user=self.customer)
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{quote.id}/send-pay-request/').status_code, 200)
        quote.refresh_from_db()
        return quote

    def pay(self, quote):
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            return self.customer_client.post(f'/api/services/my-requests/{quote.reference}/pay/', PAY_ADDRESS, format='json')

    def capture(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            order.refresh_from_db()
            order.is_paid = True
            order.payment_status = PaymentStatus.COMPLETED
            order.status = OrderStatus.CONFIRMED
            order.save()

    def paid_quote(self):
        quote = self.sent_quote()
        self.pay(quote)
        quote.refresh_from_db()
        self.capture(quote.order)
        quote.refresh_from_db()
        return quote

    def cancel_url(self, quote):
        return f'/api/services/my-requests/{quote.reference}/cancel/'

    # ── customer cancel ──
    def test_customer_cancels_an_unpaid_quote_immediately(self):
        quote = self.sent_quote()
        self.pay(quote)  # opened the payment window
        quote.refresh_from_db()
        open_order = quote.order
        response = self.customer_client.post(self.cancel_url(quote), {'reason': 'Changed my mind'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['outcome'], 'cancelled')
        quote.refresh_from_db()
        open_order.refresh_from_db()
        self.assertEqual((quote.status, quote.cancel_reason), (QuoteStatus.CANCELLED, 'Changed my mind'))
        self.assertIsNone(response.data['request']['paid_at'])  # never paid: no refund wording in the UI
        self.assertEqual(open_order.status, OrderStatus.CANCELLED)
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title=f'Customer cancelled: {quote.reference}').exists())

    def test_cancel_permissions_and_closed_states(self):
        quote = self.sent_quote()
        stranger = make_user('stranger@example.com')
        client = APIClient()
        client.force_authenticate(stranger)
        self.assertEqual(client.post(self.cancel_url(quote), {}, format='json').status_code, 404)
        self.assertEqual(APIClient().post(self.cancel_url(quote), {}, format='json').status_code, 401)
        QuoteRequest.objects.filter(pk=quote.pk).update(status=QuoteStatus.DELIVERED)
        self.assertEqual(self.customer_client.post(self.cancel_url(quote), {}, format='json').status_code, 400)
        self.assertFalse(self.customer_client.get(f'/api/services/my-requests/{quote.reference}/').data['can_cancel'])

    def test_a_paid_order_cannot_be_cancelled_by_the_customer(self):
        quote = self.paid_quote()
        mine = self.customer_client.get(f'/api/services/my-requests/{quote.reference}/').data
        self.assertFalse(mine['can_cancel'])
        response = self.customer_client.post(self.cancel_url(quote), {'reason': 'Moving house'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('call us', response.data['error'])
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.PAID)
        self.assertEqual(quote.order.sub_orders.get().status, 'placed')
        # the team can still cancel it (refund is manual)
        admin_cancel = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/cancel/', {'reason': 'Agreed by phone'}, format='json')
        self.assertEqual(admin_cancel.status_code, 200)

    # ── reminders and the email countdown ──
    def test_reminder_goes_out_once_in_the_last_24_hours(self):
        quote = self.sent_quote()
        self.assertEqual(send_pay_by_reminders(), 0)  # 3 days left: too early
        QuoteRequest.objects.filter(pk=quote.pk).update(pay_by=timezone.now() + timedelta(hours=20, minutes=5))
        mail.outbox.clear()
        self.assertEqual(send_pay_by_reminders(), 1)
        self.assertEqual(send_pay_by_reminders(), 0)  # never twice for the same date
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Your quote expires soon').exists())
        reminder = [m for m in mail.outbox if 'expires in' in m.subject]
        self.assertEqual(len(reminder), 1)
        self.assertIn('20 hours', reminder[0].subject)
        self.assertIn('Please pay within 20 hours', reminder[0].body)

    def test_reminder_skips_paid_expired_and_renewed_quotes_get_a_new_one(self):
        quote = self.sent_quote()
        QuoteRequest.objects.filter(pk=quote.pk).update(pay_by=timezone.now() + timedelta(hours=5))
        send_pay_by_reminders()
        QuoteRequest.objects.filter(pk=quote.pk).update(status=QuoteStatus.EXPIRED, pay_by=timezone.now() - timedelta(hours=1))
        self.assertEqual(send_pay_by_reminders(), 0)
        self.admin_client.post(f'/api/services/admin/requests/{quote.id}/renew/')
        quote.refresh_from_db()
        self.assertIsNone(quote.pay_by_reminder_sent_at)
        QuoteRequest.objects.filter(pk=quote.pk).update(pay_by=timezone.now() + timedelta(hours=3))
        self.assertEqual(send_pay_by_reminders(), 1)

    def test_pay_request_email_shows_the_countdown(self):
        quote = self.priced_request(user=self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            self.admin_client.post(f'/api/services/admin/requests/{quote.id}/send-pay-request/')
        body = [m for m in mail.outbox if 'ready to pay' in m.subject][0].body
        self.assertIn('Please pay within 3 days', body)

    # ── price change after the payment window opened ──
    def test_admin_can_reprice_after_the_customer_opened_the_payment_window(self):
        quote = self.sent_quote()
        self.pay(quote)
        quote.refresh_from_db()
        stale = quote.order
        QuoteRequest.objects.filter(pk=quote.pk).update(pay_by=timezone.now() + timedelta(hours=2))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin_client.patch(f'/api/services/admin/requests/{quote.id}/', {'item_price': '4500'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        quote.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(quote.total_amount, Decimal('4700.00'))
        self.assertIsNone(quote.order)
        self.assertEqual(stale.status, OrderStatus.CANCELLED)
        self.assertGreater(quote.pay_by, timezone.now() + timedelta(days=2))
        self.assertTrue(AppNotification.objects.filter(user=self.customer, title='Your quote was updated').exists())
        # paying again uses the new amount
        self.assertEqual(self.pay(quote).status_code, 201)
        quote.refresh_from_db()
        self.assertEqual(quote.order.total_amount, Decimal('4700.00'))

    def test_payment_for_the_old_price_is_refunded_not_accepted(self):
        quote = self.sent_quote()
        self.pay(quote)
        quote.refresh_from_db()
        stale = quote.order
        self.admin_client.patch(f'/api/services/admin/requests/{quote.id}/', {'item_price': '4500'}, format='json')
        self.capture(stale)  # the customer's old payment completes anyway
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.AWAITING_PAYMENT)
        self.assertFalse(stale.sub_orders.exists())
        self.assertTrue(AppNotification.objects.filter(user=self.admin, title=f'Refund needed: {quote.reference}').exists())

    def test_a_paid_quote_cannot_be_repriced_and_shipping_only_changes_also_apply(self):
        quote = self.paid_quote()
        locked = self.admin_client.patch(f'/api/services/admin/requests/{quote.id}/', {'item_price': '1'}, format='json')
        self.assertEqual(locked.status_code, 400)
        fresh = self.sent_quote()
        changed = self.admin_client.patch(f'/api/services/admin/requests/{fresh.id}/', {'shipping_fee': '350'}, format='json')
        self.assertEqual(changed.status_code, 200)
        fresh.refresh_from_db()
        self.assertEqual(fresh.total_amount, Decimal('5350.00'))

    def test_notes_only_edit_does_not_reset_the_deadline(self):
        quote = self.sent_quote()
        before = quote.pay_by
        self.admin_client.patch(f'/api/services/admin/requests/{quote.id}/', {'quote_notes': 'Packed with care'}, format='json')
        quote.refresh_from_db()
        self.assertEqual(quote.pay_by, before)

    # ── extra payment after the seller payout ──
    def test_extra_payment_after_payout_is_tracked_until_the_admin_records_it(self):
        quote = self.paid_quote()
        sub_order = quote.order.sub_orders.get()
        settlement = SellerSettlement.objects.create(sub_order=sub_order, seller=self.seller, amount=sub_order.seller_total)
        settlement.mark_settled(self.admin)
        self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/', {'description': 'Bigger glass', 'amount': '1000'}, format='json')
        charge = AdditionalCharge.objects.get(quote_request=quote)
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            self.customer_client.post(f'/api/services/my-requests/{quote.reference}/charges/{charge.id}/pay/')
        charge.refresh_from_db()
        self.capture(charge.order)
        charge.refresh_from_db()
        self.assertEqual(charge.payout_status, PayoutStatus.MANUAL_DUE)
        detail = self.admin_client.get(f'/api/services/admin/requests/{quote.id}/').data
        row = detail['additional_charges'][0]
        self.assertEqual((row['payout_status'], row['seller_payout']), ('manual_due', '850.00'))
        done = self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/{charge.id}/payout-done/')
        self.assertEqual(done.status_code, 200, done.data)
        charge.refresh_from_db()
        self.assertEqual(charge.payout_status, PayoutStatus.MANUAL_DONE)
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/{charge.id}/payout-done/').status_code, 400)

    def test_extra_payment_before_payout_is_credited_and_needs_nothing_manual(self):
        quote = self.paid_quote()
        self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/', {'description': 'Moss', 'amount': '200'}, format='json')
        charge = AdditionalCharge.objects.get(quote_request=quote)
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            self.customer_client.post(f'/api/services/my-requests/{quote.reference}/charges/{charge.id}/pay/')
        charge.refresh_from_db()
        self.capture(charge.order)
        charge.refresh_from_db()
        self.assertEqual(charge.payout_status, PayoutStatus.CREDITED)
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{quote.id}/charges/{charge.id}/payout-done/').status_code, 400)
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        self.assertEqual(seller_client.post(f'/api/services/admin/requests/{quote.id}/charges/{charge.id}/payout-done/').status_code, 403)


@override_settings(**TEST_SETTINGS)
class ListQueryCountTests(ServiceTestCase):
    """Lists must cost the same number of queries however many requests they hold (no N+1)."""

    def make_many(self, customer, count):
        for _ in range(count):
            quote = self.priced_request(user=customer)
            quote.status = QuoteStatus.PAID
            quote.save()
            AdditionalCharge.objects.create(
                quote_request=quote, description='Extra', amount=Decimal('100'), commission_percent=Decimal('15'),
                seller_payout=Decimal('85'),
            )

    def queries_for(self, client, url):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(client.get(url).status_code, 200)
        return len(queries)

    def test_customer_seller_and_admin_lists_do_not_grow_with_rows(self):
        customer = make_user('asha@example.com')
        customer_client = APIClient()
        customer_client.force_authenticate(customer)
        seller_client = APIClient()
        seller_client.force_authenticate(self.seller)
        admin_client = APIClient()
        admin_client.force_authenticate(self.admin)
        urls = {
            'customer': (customer_client, '/api/services/my-requests/'),
            'seller': (seller_client, '/api/services/seller/requests/'),
            'admin': (admin_client, '/api/services/admin/requests/'),
            'groups': (self.client, '/api/services/groups/'),
        }
        self.make_many(customer, 2)
        few = {name: self.queries_for(*args) for name, args in urls.items()}
        self.make_many(customer, 6)
        many = {name: self.queries_for(*args) for name, args in urls.items()}
        self.assertEqual(few, many, f'queries grew with the number of requests: {few} -> {many}')


# ── Admin actions after the customer opened the payment window ───────────────

@override_settings(**TEST_SETTINGS)
class AfterPaymentWindowOpenedTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.customer = make_user('asha@example.com')
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.customer_client = APIClient()
        self.customer_client.force_authenticate(self.customer)
        self.other_seller = make_seller('second@junglyst.test', 'Second Studio')
        self.quote = self.priced_request(user=self.customer)
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{self.quote.id}/send-pay-request/').status_code, 200)
        self.quote.refresh_from_db()
        cache.clear()
        with patch('services.views.start_gateway_payment', return_value=FAKE_GATEWAY):
            self.assertEqual(self.customer_client.post(
                f'/api/services/my-requests/{self.quote.reference}/pay/', PAY_ADDRESS, format='json').status_code, 201)
        self.quote.refresh_from_db()  # the customer opened the gateway but has not paid

    def capture(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            order.refresh_from_db()
            order.is_paid = True
            order.payment_status = PaymentStatus.COMPLETED
            order.status = OrderStatus.CONFIRMED
            order.save()

    def test_admin_can_still_reassign_the_seller_before_anything_is_paid(self):
        response = self.admin_client.post(
            f'/api/services/admin/requests/{self.quote.id}/assign/', {'seller_id': str(self.other_seller.id)}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.assigned_seller, self.other_seller)

    def test_the_shipment_goes_to_the_seller_assigned_at_payment_time(self):
        self.admin_client.post(
            f'/api/services/admin/requests/{self.quote.id}/assign/', {'seller_id': str(self.other_seller.id)}, format='json')
        self.capture(self.quote.order)
        order = Order.objects.get(pk=self.quote.order_id)
        sub_order = order.sub_orders.get()
        self.assertEqual(sub_order.seller, self.other_seller)
        self.assertEqual(order.items.get().seller, self.other_seller)

    def test_admin_can_decline_a_quote_the_customer_never_paid(self):
        response = self.admin_client.post(f'/api/services/admin/requests/{self.quote.id}/decline/', {'reason': 'Cannot make this'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DECLINED)

    def test_nothing_can_be_reassigned_or_declined_once_paid(self):
        self.capture(self.quote.order)
        reassign = self.admin_client.post(
            f'/api/services/admin/requests/{self.quote.id}/assign/', {'seller_id': str(self.other_seller.id)}, format='json')
        self.assertEqual(reassign.status_code, 400)
        self.assertEqual(self.admin_client.post(f'/api/services/admin/requests/{self.quote.id}/decline/', {}, format='json').status_code, 400)
        self.quote.refresh_from_db()
        self.assertEqual((self.quote.status, self.quote.assigned_seller), (QuoteStatus.PAID, self.seller))
