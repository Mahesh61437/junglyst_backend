"""
End-to-end integration tests for the full order lifecycle:

  Checkout → Payment Success → Sub-order Assignment → Seller Confirmation
  → Packaging Photo → Shipment Details → Shipment Booking (Shiprocket)
  → AWB / Label → Notifications → Emails

All external API calls are mocked. Celery tasks run eagerly (in-process).
Run with:
    python manage.py test orders.tests --verbosity=2
"""

import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.core import mail
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from cart.models import Cart, CartItem
from core.models import Category, Product, ProductVariant
from notifications.models import AppNotification
from orders.models import Order, SubOrder, OrderItem
from payments.models import Payment, PaymentGatewaySettings
from sellers.models import SellerProfile
from shipping.models import ShippingAddress, Shipment

User = get_user_model()

# ── Fake external-API responses ───────────────────────────────────────────────

FAKE_RAZORPAY_ORDER = {"id": "rzp_ord_TEST123", "amount": 100000, "currency": "INR"}
FAKE_RAZORPAY_PAYMENT_ID = "pay_TEST456"
FAKE_RAZORPAY_SIGNATURE = "valid_signature"

FAKE_SHIPROCKET_SERVICEABILITY = {
    "status": True,
    "data": [
        {"courier_id": "42", "courier_name": "Delhivery", "courier_charges": 75, "etd": 3},
    ],
}
FAKE_SHIPROCKET_SHIPMENT = {
    "status": True,
    "data": {
        "shipment_id": "SR_SHIP_999",
        "order_id": "SR_ORD_999",
        "awb_number": "SR1234567890",
        "courier_name": "Delhivery",
        "status": "booked",
        "label": "https://cdn.shiprocket.in/label/SR_SHIP_999.pdf",
        "manifest": "https://cdn.shiprocket.in/manifest/SR_SHIP_999.pdf",
    },
}


# ── Test-wide settings override ───────────────────────────────────────────────

TEST_SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    # Skip Firebase push — not needed in tests
    FCM_SERVER_KEY="test-key",
)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures helper
# ─────────────────────────────────────────────────────────────────────────────

def _make_fixtures():
    """Create and return all objects needed for the order flow."""

    # ── Users ─────────────────────────────────────────────────────────────────
    admin = User.objects.create_superuser(
        email="admin@junglyst.com",
        username="admin",
        password="Admin@123",
    )
    buyer = User.objects.create_user(
        email="buyer@test.com",
        username="buyer1",
        password="Pass@123",
        role="collector",
    )
    seller = User.objects.create_user(
        email="seller@test.com",
        username="seller1",
        password="Pass@123",
        role="grower",
    )

    # ── Seller profile (required for shipment payload + shipping-day check) ─
    # NOTE: there is no auto-create signal for SellerProfile, so we have to
    # create the row here. Using .filter().update() (the old approach) silently
    # no-ops when the row doesn't exist, and the checkout view then rejects
    # the cart with "seller is on a break" because seller_profile is None.
    # shiprocket_pickup_location is set so ShiprocketService.ensure_seller_pickup_location
    # short-circuits without trying to call the real Shiprocket auth API in tests.
    SellerProfile.objects.create(
        user=seller,
        store_name="Test Nursery",
        slug="test-nursery",
        location_city="Bengaluru",
        location_state="Karnataka",
        location_pincode="560001",
        pickup_address="123 Green Street, Bengaluru",
        shiprocket_pickup_location="Test Nursery",
    )

    # ── Category + Product + Variant ──────────────────────────────────────
    category = Category.objects.create(name="Aquatic Plants", shipping_type="plant")
    product = Product.objects.create(
        name="Monstera Deliciosa",
        seller=seller,
        is_active=True,
        is_draft=False,
    )
    product.categories.add(category)
    variant = ProductVariant.objects.create(
        product=product,
        name="Standard",
        base_price=Decimal("350.00"),
        price=Decimal("499.00"),
        stock=10,
        weight=Decimal("0.5"),
        length=Decimal("20.0"),
        width=Decimal("15.0"),
        height=Decimal("25.0"),
        packed_weight_grams=600,
    )

    # ── Shipping address ───────────────────────────────────────────────────
    address = ShippingAddress.objects.create(
        user=buyer,
        full_name="Test Buyer",
        phone="9876543210",
        address_line1="456 Buyer Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
    )

    # ── Cart ───────────────────────────────────────────────────────────────
    cart = Cart.objects.create(user=buyer)
    CartItem.objects.create(cart=cart, product=product, variant=variant, quantity=2)

    # ── Payment gateway settings ───────────────────────────────────────────
    PaymentGatewaySettings.objects.update_or_create(
        pk=1,
        defaults={"active_gateway": "razorpay"},
    )

    return dict(
        admin=admin,
        buyer=buyer,
        seller=seller,
        variant=variant,
        address=address,
        cart=cart,
        product=product,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Main test class
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(**TEST_SETTINGS)
class OrderLifecycleTest(TestCase):
    """
    Sequential integration test — each phase asserts on real DB state.
    External services (Razorpay, Shiprocket) are mocked.
    """

    # ── One-time setup ─────────────────────────────────────────────────────

    @classmethod
    def setUpTestData(cls):
        cls.fx = _make_fixtures()

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.fx["buyer"])
        mail.outbox = []

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 1 — CHECKOUT
    # ═══════════════════════════════════════════════════════════════════════

    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_01_checkout_creates_order_suborders_and_payment(self, mock_schedule, mock_rzp):
        """POST /api/orders/checkout/ must create Order + SubOrder + Payment."""
        payload = {
            "cart_id": str(self.fx["cart"].id),
            "address_id": str(self.fx["address"].id),
        }
        resp = self.client.post("/api/orders/checkout/", payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        data = resp.data

        # Gateway response fields present
        self.assertIn("razorpay_order_id", data)
        self.assertEqual(data["razorpay_order_id"], FAKE_RAZORPAY_ORDER["id"])

        # Order created in DB
        order = Order.objects.get(user=self.fx["buyer"])
        self.assertEqual(order.status, "pending")
        self.assertFalse(order.is_paid)

        # One sub-order per seller
        sub_orders = SubOrder.objects.filter(order=order)
        self.assertEqual(sub_orders.count(), 1)

        sub = sub_orders.first()
        self.assertEqual(sub.seller, self.fx["seller"])
        self.assertEqual(sub.status, "pending")

        # OrderItems linked correctly
        items = OrderItem.objects.filter(order=order)
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.first().variant_id, self.fx["variant"].id)
        self.assertEqual(items.first().quantity, 2)

        # Payment record created
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.gateway, "razorpay")
        self.assertEqual(payment.razorpay_order_id, FAKE_RAZORPAY_ORDER["id"])
        self.assertEqual(payment.status, "created")

        # schedule_payment_checks was called (reconciliation tasks queued)
        mock_schedule.assert_called_once_with(payment.id)

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 2 — PAYMENT VERIFICATION
    # ═══════════════════════════════════════════════════════════════════════

    def _do_checkout(self):
        """Helper: checkout and return the created order + payment."""
        with patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER), \
             patch("payments.tasks.schedule_payment_checks"):
            self.client.post("/api/orders/checkout/", {
                "cart_id": str(self.fx["cart"].id),
                "address_id": str(self.fx["address"].id),
            }, format="json")
        order = Order.objects.filter(user=self.fx["buyer"]).order_by("-created_at").first()
        payment = Payment.objects.get(order=order)
        return order, payment

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_02_payment_verification_confirms_order(self, mock_schedule, mock_rzp, mock_verify):
        """POST /api/orders/checkout/verify/ — order confirmed, sub-orders placed."""
        order, payment = self._do_checkout()

        verify_payload = {
            "gateway": "razorpay",
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }
        resp = self.client.post("/api/orders/checkout/verify/", verify_payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        # Order confirmed
        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.payment_status, "completed")

        # Payment captured
        payment.refresh_from_db()
        self.assertEqual(payment.status, "captured")
        self.assertIsNotNone(payment.paid_at)

        # Sub-orders set to 'placed'
        sub = SubOrder.objects.get(order=order)
        self.assertEqual(sub.status, "placed")
        self.assertIsNotNone(sub.dispatch_deadline)

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_03_stock_deducted_on_payment(self, mock_schedule, mock_rzp, mock_verify):
        """Stock must be decremented atomically after payment capture."""
        initial_stock = self.fx["variant"].stock  # 10
        self._do_checkout()

        self.client.post("/api/orders/checkout/verify/", {
            "gateway": "razorpay",
            "razorpay_order_id": Payment.objects.latest("id").razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }, format="json")

        self.fx["variant"].refresh_from_db()
        self.assertEqual(self.fx["variant"].stock, initial_stock - 2)

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_04_confirmation_emails_sent(self, mock_schedule, mock_rzp, mock_verify):
        """3 emails must be sent: buyer, seller, admin."""
        order, payment = self._do_checkout()

        self.client.post("/api/orders/checkout/verify/", {
            "gateway": "razorpay",
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }, format="json")

        # At minimum: one email to buyer, one to seller, one to admin
        recipients = [m.to[0] for m in mail.outbox]
        self.assertIn(self.fx["buyer"].email, recipients, "Buyer email missing")
        self.assertIn(self.fx["seller"].email, recipients, "Seller email missing")
        self.assertIn(self.fx["admin"].email, recipients, "Admin email missing")

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_05_notifications_created_on_payment(self, mock_schedule, mock_rzp, mock_verify):
        """Buyer and seller must each receive an in-app AppNotification."""
        order, payment = self._do_checkout()

        self.client.post("/api/orders/checkout/verify/", {
            "gateway": "razorpay",
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }, format="json")

        self.assertTrue(
            AppNotification.objects.filter(user=self.fx["buyer"]).exists(),
            "Buyer notification missing",
        )
        self.assertTrue(
            AppNotification.objects.filter(user=self.fx["seller"]).exists(),
            "Seller notification missing",
        )

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_06_cart_cleared_after_payment(self, mock_schedule, mock_rzp, mock_verify):
        """Cart items must be gone after successful payment."""
        order, payment = self._do_checkout()

        self.client.post("/api/orders/checkout/verify/", {
            "gateway": "razorpay",
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }, format="json")

        self.assertEqual(CartItem.objects.filter(cart=self.fx["cart"]).count(), 0)

    @patch("orders.views.verify_razorpay_signature", return_value=True)
    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_07_payment_is_idempotent(self, mock_schedule, mock_rzp, mock_verify):
        """Calling verify twice for same payment must not error or double-deduct stock."""
        order, payment = self._do_checkout()
        payload = {
            "gateway": "razorpay",
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
            "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
        }
        self.client.post("/api/orders/checkout/verify/", payload, format="json")
        stock_after_first = ProductVariant.objects.get(pk=self.fx["variant"].pk).stock

        resp2 = self.client.post("/api/orders/checkout/verify/", payload, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        stock_after_second = ProductVariant.objects.get(pk=self.fx["variant"].pk).stock
        self.assertEqual(stock_after_first, stock_after_second, "Stock double-deducted!")

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 3 — SELLER FLOW
    # ═══════════════════════════════════════════════════════════════════════

    def _placed_sub_order(self):
        """Return a sub-order that is in 'placed' state (post-payment)."""
        with patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER), \
             patch("payments.tasks.schedule_payment_checks"), \
             patch("orders.views.verify_razorpay_signature", return_value=True):

            self.client.post("/api/orders/checkout/", {
                "cart_id": str(self.fx["cart"].id),
                "address_id": str(self.fx["address"].id),
            }, format="json")

            payment = Payment.objects.latest("id")
            self.client.post("/api/orders/checkout/verify/", {
                "gateway": "razorpay",
                "razorpay_order_id": payment.razorpay_order_id,
                "razorpay_payment_id": FAKE_RAZORPAY_PAYMENT_ID,
                "razorpay_signature": FAKE_RAZORPAY_SIGNATURE,
            }, format="json")

        return SubOrder.objects.latest("id")

    # Confirm now also books the courier in the same step, so the request body
    # must carry the seller's approximate package weight + L×B×H. Photos remain
    # optional at this stage — they're collected later at Ship Now.
    APPROX_DIMS = {
        "actual_weight_grams": 620,
        "actual_length_cm": 22,
        "actual_breadth_cm": 16,
        "actual_height_cm": 28,
    }

    @patch("shipping.services.ShiprocketService.check_serviceability",
           return_value=FAKE_SHIPROCKET_SERVICEABILITY)
    @patch("shipping.services.ShiprocketService.create_shipment",
           return_value=FAKE_SHIPROCKET_SHIPMENT)
    def test_08_seller_confirms_suborder(self, mock_ship, mock_svc):
        """
        POST confirm/ with approx dims must:
          - validate the body (weight + L×B×H required),
          - move the sub-order from 'placed' to 'confirmed' with a 48h deadline,
          - immediately queue the courier-booking task so the label is generated
            up-front,
          - notify the buyer.
        """
        sub = self._placed_sub_order()
        self.assertEqual(sub.status, "placed")

        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        sub.refresh_from_db()
        # Status is 'booked' once Celery (eager mode) finishes the booking task.
        self.assertIn(sub.status, ("confirmed", "booked"))
        self.assertIsNotNone(sub.confirmed_at)
        self.assertIsNotNone(sub.dispatch_deadline)
        # Approx dims persisted on the sub-order
        self.assertEqual(sub.actual_weight_grams, self.APPROX_DIMS["actual_weight_grams"])
        self.assertEqual(sub.actual_length_cm, self.APPROX_DIMS["actual_length_cm"])

        # Booking task ran (Celery eager) — AWB written and shipment created
        self.assertEqual(sub.awb_number, FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"])

        # Buyer notified
        self.assertTrue(
            AppNotification.objects.filter(user=self.fx["buyer"]).exists()
        )

    def test_08b_confirm_rejects_missing_dims(self):
        """Confirm without weight/dimensions must return 400 — no silent default booking."""
        sub = self._placed_sub_order()
        self.client.force_authenticate(user=self.fx["seller"])
        # No body at all
        resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/confirm/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "placed")

        # Only weight, no L/B/H
        resp2 = self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            {"actual_weight_grams": 500}, format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST, resp2.data)

    def test_09_upload_packaging_photo(self):
        """POST upload-photo/ → URL appended to packaging_photos. Status transitions
        confirmed→packing if still in legacy state; booked stays booked because the
        courier is already assigned and photos are just a pre-pickup gate now."""
        sub = self._placed_sub_order()
        sub.status = "confirmed"
        sub.confirmed_at = timezone.now()
        sub.save()

        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/upload-photo/",
            {"photo_url": "https://storage.googleapis.com/junglyst/pack1.jpg"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        sub.refresh_from_db()
        self.assertIn(
            "https://storage.googleapis.com/junglyst/pack1.jpg",
            sub.packaging_photos,
        )
        self.assertEqual(sub.status, "packing")

    def test_10_update_shipment_details(self):
        """PATCH shipment-details/ → weight & dimensions saved on sub-order."""
        sub = self._placed_sub_order()
        sub.status = "packing"
        sub.save()

        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.patch(
            f"/api/orders/seller/sub-orders/{sub.id}/shipment-details/",
            {
                "actual_weight_grams": 620,
                "actual_length_cm": 22,
                "actual_breadth_cm": 16,
                "actual_height_cm": 28,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        sub.refresh_from_db()
        self.assertEqual(sub.actual_weight_grams, 620)
        self.assertEqual(sub.actual_length_cm, 22)

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 4 — SHIPMENT CREATION (Shiprocket)
    # ═══════════════════════════════════════════════════════════════════════

    def _booked_sub_order(self):
        """
        Return a sub-order in the new 'booked' state — courier assigned, label
        generated, ready for the seller to upload the packaging photo and click
        Ship Now. Skips the actual booking task and directly stamps the AWB +
        shipment row so individual tests don't all pay for the Shiprocket
        mock-roundtrip when they only want to exercise the pickup-scheduling
        path.
        """
        sub = self._placed_sub_order()
        sub.status = "booked"
        sub.confirmed_at = timezone.now()
        sub.actual_weight_grams = 620
        sub.actual_length_cm = 22
        sub.actual_breadth_cm = 16
        sub.actual_height_cm = 28
        sub.awb_number = FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"]
        sub.courier_name = FAKE_SHIPROCKET_SHIPMENT["data"]["courier_name"]
        sub.save()
        Shipment.objects.create(
            order=sub.order,
            seller=self.fx["seller"],
            awb_number=sub.awb_number,
            courier_name=sub.courier_name,
            nimbuspost_id=FAKE_SHIPROCKET_SHIPMENT["data"]["shipment_id"],
            label_url=FAKE_SHIPROCKET_SHIPMENT["data"]["label"],
            status="booked",
        )
        return sub

    def _ready_to_ship_sub_order(self):
        """Booked sub-order + packaging photo uploaded → eligible for Ship Now."""
        sub = self._booked_sub_order()
        sub.packaging_photos = ["https://storage.googleapis.com/junglyst/pack1.jpg"]
        sub.save()
        return sub

    @patch("shipping.services.ShiprocketService.check_serviceability",
           return_value=FAKE_SHIPROCKET_SERVICEABILITY)
    @patch("shipping.services.ShiprocketService.create_shipment",
           return_value=FAKE_SHIPROCKET_SHIPMENT)
    def test_11_confirm_creates_shipment_with_label(self, mock_ship, mock_svc):
        """
        Under the new flow the Shipment record + AWB + label are created at
        Confirm time, not Ship Now. POST confirm/ with approx dims must trigger
        ShiprocketService.create_shipment and persist a Shipment with label_url.
        """
        sub = self._placed_sub_order()

        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        shipment = Shipment.objects.get(order=sub.order, seller=self.fx["seller"])
        self.assertEqual(shipment.awb_number, FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"])
        self.assertEqual(shipment.courier_name, FAKE_SHIPROCKET_SHIPMENT["data"]["courier_name"])
        self.assertIsNotNone(shipment.label_url)
        # Shiprocket was called with request_auto_pickup="No" so pickup is deferred
        call_payload = mock_ship.call_args[0][0]
        self.assertEqual(call_payload.get("request_auto_pickup"), "No")

    @patch("shipping.services.ShiprocketService.check_serviceability",
           return_value=FAKE_SHIPROCKET_SERVICEABILITY)
    @patch("shipping.services.ShiprocketService.create_shipment",
           return_value=FAKE_SHIPROCKET_SHIPMENT)
    def test_12_awb_written_to_suborder_and_master_order(self, mock_ship, mock_svc):
        """AWB number must propagate from Shipment → SubOrder → Order at Confirm."""
        sub = self._placed_sub_order()
        order = sub.order

        self.client.force_authenticate(user=self.fx["seller"])
        self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )

        sub.refresh_from_db()
        order.refresh_from_db()

        expected_awb = FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"]
        self.assertEqual(sub.awb_number, expected_awb)
        self.assertEqual(order.awb_number, expected_awb)
        self.assertEqual(order.courier_name, FAKE_SHIPROCKET_SHIPMENT["data"]["courier_name"])

    @patch("shipping.services.ShiprocketService.check_serviceability",
           return_value=FAKE_SHIPROCKET_SERVICEABILITY)
    @patch("shipping.services.ShiprocketService.create_shipment",
           return_value=FAKE_SHIPROCKET_SHIPMENT)
    def test_13_buyer_notified_at_booking_and_pickup(self, mock_ship, mock_svc):
        """Buyer gets an in-app notification when the courier is booked at Confirm."""
        sub = self._placed_sub_order()

        AppNotification.objects.filter(user=self.fx["buyer"]).delete()  # clean slate

        self.client.force_authenticate(user=self.fx["seller"])
        self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )

        notifs = AppNotification.objects.filter(user=self.fx["buyer"])
        self.assertTrue(notifs.exists(), "No notification sent to buyer at Confirm")
        # At least one mentions the AWB — either the "confirmed" or the "shipped" message
        awb = FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"].lower()
        joined = " ".join(n.message.lower() for n in notifs)
        self.assertTrue(
            awb in joined or "confirmed" in joined or "way" in joined,
            f"Unexpected notification messages: {[n.message for n in notifs]}",
        )

    @patch("shipping.services.ShiprocketService.check_serviceability",
           return_value=FAKE_SHIPROCKET_SERVICEABILITY)
    @patch("shipping.services.ShiprocketService.create_shipment",
           return_value=FAKE_SHIPROCKET_SHIPMENT)
    def test_14_suborder_awb_written_after_booking(self, mock_ship, mock_svc):
        """AWB stamped on sub-order at Confirm; status moves placed → booked."""
        sub = self._placed_sub_order()

        self.client.force_authenticate(user=self.fx["seller"])
        self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )

        sub.refresh_from_db()
        self.assertEqual(
            sub.awb_number,
            FAKE_SHIPROCKET_SHIPMENT["data"]["awb_number"],
        )
        self.assertEqual(sub.status, "booked")

    # ── Ship Now (schedule pickup) path ────────────────────────────────────

    def test_14b_ship_now_schedules_pickup_when_already_booked(self):
        """
        Standard new-flow Ship Now path: AWB already exists (set at Confirm),
        seller has uploaded photo + actuals. POST ship/ should call
        schedule_pickup_task (which calls ShiprocketService.request_pickup)
        rather than re-booking the shipment.
        """
        sub = self._ready_to_ship_sub_order()

        self.client.force_authenticate(user=self.fx["seller"])
        with patch(
            "shipping.services.ShiprocketService.request_pickup",
            return_value={"status": True, "data": {}},
        ) as mock_pickup, patch(
            "shipping.services.ShiprocketService.create_shipment"
        ) as mock_create:
            resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/ship/")
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            mock_pickup.assert_called_once()
            mock_create.assert_not_called()  # AWB already exists, do not re-book

    def test_14c_ship_now_blocked_without_photo(self):
        """Booked status alone isn't enough — photo + actuals are still required."""
        sub = self._booked_sub_order()  # no packaging_photos
        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/ship/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)

    # ── Refresh label endpoint ─────────────────────────────────────────────

    def test_14d_refresh_label_returns_url_when_available(self):
        """POST /refresh-label/ → re-fetches label by AWB and persists it."""
        sub = self._booked_sub_order()
        # Wipe label_url to simulate Shiprocket initially returning empty
        Shipment.objects.filter(order=sub.order, seller=self.fx["seller"]).update(label_url=None)

        self.client.force_authenticate(user=self.fx["seller"])
        with patch(
            "shipping.services.ShiprocketService.generate_label",
            return_value={"status": True, "data": "https://cdn.shiprocket.in/label/late.pdf"},
        ) as mock_label:
            resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/refresh-label/")
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            mock_label.assert_called_once_with([sub.awb_number])

        shipment = Shipment.objects.get(order=sub.order, seller=self.fx["seller"])
        self.assertEqual(shipment.label_url, "https://cdn.shiprocket.in/label/late.pdf")

    def test_14e_refresh_label_returns_202_when_not_ready(self):
        """When Shiprocket has not generated the PDF yet, the endpoint signals 'come back later'."""
        sub = self._booked_sub_order()
        Shipment.objects.filter(order=sub.order, seller=self.fx["seller"]).update(label_url=None)

        self.client.force_authenticate(user=self.fx["seller"])
        with patch(
            "shipping.services.ShiprocketService.generate_label",
            return_value={"status": False, "data": ""},
        ):
            resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/refresh-label/")
            self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED, resp.data)

    def test_14f_refresh_label_rejects_when_no_awb(self):
        """Calling refresh-label before booking finishes is a bad request — no AWB to look up."""
        sub = self._placed_sub_order()
        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/refresh-label/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)

    # ═══════════════════════════════════════════════════════════════════════
    # EDGE CASES
    # ═══════════════════════════════════════════════════════════════════════

    def test_15_ship_blocked_without_photo(self):
        """Legacy `packing` orders (confirmed before the new flow shipped) must
        still be blocked from Ship Now if there is no packaging photo."""
        sub = self._placed_sub_order()
        sub.status = "packing"
        sub.actual_weight_grams = 500
        sub.actual_length_cm = 20
        sub.actual_breadth_cm = 15
        sub.actual_height_cm = 25
        # packaging_photos intentionally empty
        sub.save()

        self.client.force_authenticate(user=self.fx["seller"])
        resp = self.client.post(f"/api/orders/seller/sub-orders/{sub.id}/ship/")
        self.assertIn(resp.status_code, [400, 422], resp.data)

    def test_16_wrong_seller_cannot_confirm_other_sellers_suborder(self):
        """A different seller must not be able to confirm another seller's sub-order.
        We pass valid dims so any rejection is purely on the ownership check."""
        sub = self._placed_sub_order()

        other_seller = User.objects.create_user(
            email="other@test.com",
            username="otherseller",
            password="Pass@123",
            role="grower",
        )
        self.client.force_authenticate(user=other_seller)
        resp = self.client.post(
            f"/api/orders/seller/sub-orders/{sub.id}/confirm/",
            self.APPROX_DIMS, format="json",
        )
        self.assertIn(resp.status_code, [403, 404], resp.data)

    @patch("orders.views.create_razorpay_order", return_value=FAKE_RAZORPAY_ORDER)
    @patch("payments.tasks.schedule_payment_checks")
    def test_17_checkout_fails_if_stock_insufficient(self, mock_schedule, mock_rzp):
        """Checkout must fail when requested quantity exceeds available stock."""
        self.fx["variant"].stock = 1
        self.fx["variant"].save()

        # Cart has quantity=2 but stock is 1
        resp = self.client.post("/api/orders/checkout/", {
            "cart_id": str(self.fx["cart"].id),
            "address_id": str(self.fx["address"].id),
        }, format="json")

        self.assertIn(resp.status_code, [400, 422], resp.data)

        # Restore
        self.fx["variant"].stock = 10
        self.fx["variant"].save()
