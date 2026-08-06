from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status

from core.models import Product, ProductVariant
from orders.models import Order, OrderItem
from analytics.services import pct_change, parse_date_range

User = get_user_model()


class _Req:
    """Minimal stand-in for a DRF request for unit-testing helpers."""
    def __init__(self, **params):
        self.query_params = params


class AnalyticsHelperTests(APITestCase):
    def test_pct_change(self):
        self.assertEqual(pct_change(150, 100), 50.0)
        self.assertEqual(pct_change(50, 100), -50.0)
        self.assertIsNone(pct_change(0, 0))      # no baseline, no activity → "—"
        self.assertEqual(pct_change(10, 0), 100.0)  # new activity vs nothing

    def test_parse_date_range_period_and_prev_window(self):
        rng = parse_date_range(_Req(period='30d'))
        self.assertIsNotNone(rng['start'])
        self.assertIsNotNone(rng['prev_start'])
        # Previous window ends right before the current one starts.
        self.assertLess(rng['prev_end'], rng['start'])
        # Equal length (±2s for the microsecond boundary).
        cur_span = (rng['end'] - rng['start']).total_seconds()
        prev_span = (rng['prev_end'] - rng['prev_start']).total_seconds()
        self.assertAlmostEqual(cur_span, prev_span, delta=2)


class GrowerDashboardRevenueTests(APITestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            email="rev@example.com", username="rev_seller", password="Password123", role="grower"
        )
        self.product = Product.objects.create(
            name="Revenue Plant", description="d", seller=self.seller, is_active=True, is_draft=False
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, name="Standard", stock=100, price=500.0, commission_rate=10.0
        )
        self.url = reverse('grower_dashboard')

    def _make_order(self, *, qty, unit_price, when, order_status='delivered', n=0):
        order = Order.objects.create(
            order_number=f"JNG-T-{timezone.now().timestamp()}-{n}",
            shipping_address={}, subtotal=unit_price * qty,
            total_amount=unit_price * qty, status=order_status,
        )
        # created_at is auto_now_add — override directly to place it in a window.
        Order.objects.filter(pk=order.pk).update(created_at=when)
        OrderItem.objects.create(
            order=order, product=self.product, variant=self.variant,
            product_name=self.product.name, variant_name="Standard",
            unit_price=unit_price, gst_percentage=18, quantity=qty, seller=self.seller,
        )
        return order

    def test_revenue_multiplies_by_quantity(self):
        # One delivered order, 3 units @ ₹500 → revenue must be 1500, not 500.
        self._make_order(qty=3, unit_price=500, when=timezone.now() - timedelta(days=1))
        self.client.force_authenticate(self.seller)
        res = self.client.get(self.url, {'period': '30d'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        m = res.data['metrics']
        self.assertEqual(m['total_revenue'], 1500.0)
        self.assertEqual(m['total_items_sold'], 3)
        self.assertEqual(m['total_orders'], 1)

    def test_cancelled_and_pending_excluded(self):
        now = timezone.now()
        self._make_order(qty=2, unit_price=100, when=now - timedelta(days=1), order_status='delivered', n=1)
        self._make_order(qty=5, unit_price=100, when=now - timedelta(days=1), order_status='cancelled', n=2)
        self._make_order(qty=9, unit_price=100, when=now - timedelta(days=1), order_status='pending', n=3)
        self.client.force_authenticate(self.seller)
        res = self.client.get(self.url, {'period': '30d'})
        m = res.data['metrics']
        # Only the delivered order's 2 units × ₹100 count.
        self.assertEqual(m['total_revenue'], 200.0)
        self.assertEqual(m['total_items_sold'], 2)

    def test_date_window_and_trend(self):
        now = timezone.now()
        # Current 30d window: ₹300 (3 × 100). Previous 30d window: ₹100 (1 × 100).
        self._make_order(qty=3, unit_price=100, when=now - timedelta(days=2), n=10)
        self._make_order(qty=1, unit_price=100, when=now - timedelta(days=40), n=11)
        self.client.force_authenticate(self.seller)
        res = self.client.get(self.url, {'period': '30d'})
        m = res.data['metrics']
        self.assertEqual(m['total_revenue'], 300.0)
        # Trend vs previous window (100 → 300) = +200%.
        self.assertEqual(m['trends']['revenue'], 200.0)
        self.assertIn('net_earnings', m)
        self.assertIn('net_settlement', m['net_earnings'])
        self.assertTrue(m['net_earnings']['gross_sales'] > 0)

    def test_csv_export(self):
        self._make_order(qty=2, unit_price=250, when=timezone.now() - timedelta(days=1), n=20)
        self.client.force_authenticate(self.seller)
        res = self.client.get(reverse('seller_sales_report_csv'), {'period': '30d'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'text/csv')
        body = res.content.decode()
        self.assertIn('Net Settlement', body)
        self.assertIn('Gross Sales', body)
