from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Product, ProductVariant
from orders.models import Order, OrderItem, SubOrder

User = get_user_model()


# Product.save() fires a cache-invalidation signal; use a local cache so the
# suite does not need a live Redis.
@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class SuperAdminDashboardTests(APITestCase):
    """Covers the admin order stream: every sub-order status is reachable,
    revenue only counts completed payments, and the date filter scopes both."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="sa@example.com", username="sa", password="Password123", role="admin",
        )
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save()
        self.seller = User.objects.create_user(
            email="s1@example.com", username="s1", password="Password123", role="grower",
        )
        self.product = Product.objects.create(
            name="P", description="d", seller=self.seller, is_active=True, is_draft=False,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, name="Std", stock=100, price=500, commission_rate=10,
        )
        self.url = reverse('super_admin_dashboard')
        self.n = 0

    def _order(self, *, payment_status, sub_status, amount=1000, when=None):
        self.n += 1
        o = Order.objects.create(
            order_number=f"JNG-T-{self.n:05d}",
            shipping_address={}, subtotal=amount, total_amount=amount,
            status='confirmed', payment_status=payment_status,
        )
        if when:
            # created_at is auto_now_add — override directly to place it in a window.
            Order.objects.filter(pk=o.pk).update(created_at=when)
        so = SubOrder.objects.create(
            order=o, sub_order_number=f"JNG-T-{self.n:05d}-A", seller=self.seller,
            status=sub_status, subtotal=amount, seller_total=amount,
        )
        if when:
            SubOrder.objects.filter(pk=so.pk).update(created_at=when)
        OrderItem.objects.create(
            order=o, sub_order=so, product=self.product, variant=self.variant,
            product_name="P", variant_name="Std", unit_price=amount, gst_percentage=18,
            quantity=1, seller=self.seller,
        )
        return o, so

    def test_all_statuses_returned_and_revenue_excludes_unpaid(self):
        self._order(payment_status='completed', sub_status='booked', amount=1000)
        self._order(payment_status='completed', sub_status='out_for_delivery', amount=2000)
        self._order(payment_status='completed', sub_status='delivery_failed', amount=3000)
        self._order(payment_status='pending', sub_status='pending', amount=9999)
        self._order(payment_status='failed', sub_status='cancelled', amount=8888)

        self.client.force_authenticate(self.admin)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        d = res.data

        # Revenue counts only completed payments: 1000 + 2000 + 3000
        self.assertEqual(float(d['overall_analytics']['revenue_this_month']), 6000.0)
        self.assertEqual(d['overall_analytics']['orders_this_month'], 3)
        self.assertEqual(d['overall_analytics']['unpaid_orders'], 2)
        self.assertEqual(float(d['overall_analytics']['unpaid_value']), 9999 + 8888)

        # Statuses the old three-bucket split dropped are now present.
        main = {o['status'] for o in d['orders']['all']}
        self.assertEqual(main, {'booked', 'out_for_delivery', 'delivery_failed'})
        self.assertEqual(len(d['orders']['unpaid']), 2)
        self.assertEqual(d['status_counts']['booked'], 1)
        self.assertEqual(d['status_counts']['delivery_failed'], 1)
        self.assertEqual(len(d['status_choices']), 13)

        # Seller revenue also respects the payment filter.
        seller_row = next(s for s in d['sellers'] if s['id'] == self.seller.id)
        self.assertEqual(float(seller_row['total_revenue']), 6000.0)
        self.assertEqual(seller_row['total_orders'], 3)

    def test_date_filter(self):
        old = timezone.now() - timedelta(days=120)
        self._order(payment_status='completed', sub_status='delivered', amount=500, when=old)
        self._order(payment_status='completed', sub_status='delivered', amount=700)

        self.client.force_authenticate(self.admin)

        res = self.client.get(self.url, {'period': '30d'})
        self.assertEqual(float(res.data['overall_analytics']['revenue_this_month']), 700.0)
        self.assertEqual(len(res.data['orders']['all']), 1)

        res = self.client.get(self.url, {'period': 'all'})
        self.assertEqual(float(res.data['overall_analytics']['revenue_this_month']), 1200.0)
        self.assertEqual(len(res.data['orders']['all']), 2)

        start = (timezone.now() - timedelta(days=125)).strftime('%Y-%m-%d')
        end = (timezone.now() - timedelta(days=115)).strftime('%Y-%m-%d')
        res = self.client.get(self.url, {'start': start, 'end': end})
        self.assertEqual(float(res.data['overall_analytics']['revenue_this_month']), 500.0)
        self.assertEqual(len(res.data['orders']['all']), 1)
