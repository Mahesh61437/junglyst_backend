"""
Workshop bookings: listing, seat holds, payment lifecycle via the shared
Order/Payment pipeline, admin configuration, seller visibility and money.

Run with (SQLite + local cache, never the .env services):
    DB_HOST= REDIS_URL= REDISHOST= python manage.py test workshops
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import Product, ProductVariant
from notifications.models import AppNotification
from orders.bookings import BookingStatus, split_commission
from orders.models import Order, OrderStatus, OrderType
from payments.models import Payment, PaymentGateway
from sellers.models import SellerProfile

from .models import (
    SEAT_HOLD_MINUTES, Workshop, WorkshopBooking, WorkshopOption, WorkshopPayout, WorkshopSession,
    WorkshopStatus,
)
from .services import BookingError, BookingRequest, create_pending_booking, workshop_financials

User = get_user_model()

TEST_SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
FAKE_GATEWAY = {
    "gateway": PaymentGateway.RAZORPAY, "razorpay_order_id": "rzp_ws_1",
    "razorpay_key_id": "rzp_test", "amount": Decimal("1798.00"), "currency": "INR",
}


def make_seller(email, store_name):
    seller = User.objects.create_user(
        email=email, username=email.split('@')[0], password="Pass@123",
        role="grower", is_verified_seller=True,
    )
    SellerProfile.objects.create(user=seller, store_name=store_name, slug=store_name.lower().replace(' ', '-'))
    return seller


def make_workshop(seller, *, capacity=10, days_ahead=7, **overrides):
    fields = dict(
        title="Terrarium Basics", seller=seller, status=WorkshopStatus.PUBLISHED,
        commission_percent=Decimal("20.00"), venue_name="Green Studio", city="Kochi",
        highlights=["3 inch round bowl", "Welcome drink", "Take home your art"],
    )
    fields.update(overrides)
    workshop = Workshop.objects.create(**fields)
    option = WorkshopOption.objects.create(workshop=workshop, name="Round bowl · 3 inch", shape="Round",
                                           size="3 inch", price=Decimal("899.00"))
    session = WorkshopSession.objects.create(
        workshop=workshop, date=timezone.localdate() + timedelta(days=days_ahead),
        start_time=time(10, 0), end_time=time(12, 0), capacity=capacity,
    )
    return workshop, option, session


def booking_request(workshop, option, session, participants=2, user=None):
    return BookingRequest(
        workshop=workshop, session_id=session.id, option_id=option.id, participants=participants,
        attendee_name="Asha", attendee_phone="9876543210", attendee_email="asha@example.com", user=user,
    )


@override_settings(**TEST_SETTINGS)
class CommissionMathTests(TestCase):
    def test_split_commission_rounds_to_paise(self):
        self.assertEqual(split_commission(Decimal("1798.00"), Decimal("20")), (Decimal("359.60"), Decimal("1438.40")))
        self.assertEqual(split_commission(Decimal("999.99"), Decimal("12.5")), (Decimal("125.00"), Decimal("874.99")))

    def test_zero_commission_pays_seller_everything(self):
        self.assertEqual(split_commission(Decimal("500"), Decimal("0")), (Decimal("0.00"), Decimal("500")))


@override_settings(**TEST_SETTINGS)
class BookingCreationTests(TestCase):
    def setUp(self):
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.workshop, self.option, self.session = make_workshop(self.seller, capacity=4)

    def test_pending_booking_snapshots_price_and_commission(self):
        booking = create_pending_booking(booking_request(self.workshop, self.option, self.session))
        self.assertEqual(booking.status, BookingStatus.PENDING_PAYMENT)
        self.assertEqual(booking.total_amount, Decimal("1798.00"))
        self.assertEqual(booking.commission_amount, Decimal("359.60"))
        self.assertEqual(booking.seller_payout, Decimal("1438.40"))
        self.assertEqual(booking.seller, self.seller)
        order = booking.order
        self.assertEqual(order.order_type, OrderType.WORKSHOP)
        self.assertEqual(order.total_amount, Decimal("1798.00"))
        self.assertEqual(order.guest_email, "asha@example.com")
        self.assertFalse(order.items.exists())

    def test_rejects_more_seats_than_left(self):
        create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=3))
        with self.assertRaisesMessage(BookingError, "Only 1 seat(s) left"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=2))

    def test_full_session_message(self):
        create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=4))
        with self.assertRaisesMessage(BookingError, "fully booked"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=1))

    def test_expired_unpaid_hold_releases_seats(self):
        held = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=4))
        WorkshopBooking.objects.filter(pk=held.pk).update(
            created_at=timezone.now() - timedelta(minutes=SEAT_HOLD_MINUTES + 1))
        booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=4))
        self.assertEqual(booking.participants, 4)

    def test_failed_payment_releases_seats(self):
        held = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=4))
        with self.captureOnCommitCallbacks(execute=True):
            held.order.status = OrderStatus.FAILED
            held.order.save()
        held.refresh_from_db()
        self.assertEqual(held.status, BookingStatus.PAYMENT_FAILED)
        create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=4))

    def test_max_seats_per_booking_enforced(self):
        self.workshop.max_seats_per_booking = 2
        self.workshop.save()
        with self.assertRaisesMessage(BookingError, "between 1 and 2"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=3))

    def test_registration_window_enforced(self):
        self.workshop.registration_closes_at = timezone.now() - timedelta(hours=1)
        self.workshop.save()
        with self.assertRaisesMessage(BookingError, "closed"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session))
        self.workshop.registration_closes_at = None
        self.workshop.registration_opens_at = timezone.now() + timedelta(days=1)
        self.workshop.save()
        with self.assertRaisesMessage(BookingError, "closed"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session))

    def test_inactive_option_or_session_from_other_workshop_rejected(self):
        other, other_option, other_session = make_workshop(self.seller, title="Aquascape 101")
        with self.assertRaisesMessage(BookingError, "option"):
            create_pending_booking(booking_request(self.workshop, other_option, self.session))
        with self.assertRaisesMessage(BookingError, "session"):
            create_pending_booking(booking_request(self.workshop, self.option, other_session))

    def test_past_session_rejected(self):
        self.session.date = timezone.localdate() - timedelta(days=1)
        self.session.save()
        with self.assertRaisesMessage(BookingError, "already started"):
            create_pending_booking(booking_request(self.workshop, self.option, self.session))


@override_settings(**TEST_SETTINGS)
class BookingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.workshop, self.option, self.session = make_workshop(self.seller)
        self.url = f"/api/workshops/{self.workshop.slug}/book/"
        self.payload = {
            "session_id": self.session.id, "option_id": self.option.id, "participants": 2,
            "attendee_name": "Asha", "attendee_phone": "9876543210", "attendee_email": "asha@example.com",
        }

    @patch("workshops.views.start_gateway_payment", return_value=FAKE_GATEWAY)
    def test_guest_can_book_and_gets_gateway_payload(self, _gateway):
        res = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["razorpay_order_id"], "rzp_ws_1")
        self.assertEqual(res.data["order"]["order_type"], "workshop")
        self.assertEqual(res.data["order"]["booking"]["title"], "Terrarium Basics")
        self.assertEqual(res.data["booking"]["status"], BookingStatus.PENDING_PAYMENT)

    def test_invalid_phone_rejected(self):
        res = self.client.post(self.url, {**self.payload, "attendee_phone": "12ab"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("attendee_phone", res.data)

    def test_draft_workshop_not_bookable_or_listed(self):
        self.workshop.status = WorkshopStatus.DRAFT
        self.workshop.save()
        self.assertEqual(self.client.post(self.url, self.payload, format="json").status_code, 404)
        self.assertEqual(self.client.get(f"/api/workshops/{self.workshop.slug}/").status_code, 404)
        self.assertEqual(self.client.get("/api/workshops/").data["count"], 0)

    @patch("workshops.views.start_gateway_payment")
    def test_gateway_failure_marks_order_failed_and_frees_seats(self, gateway):
        from payments.checkout import GatewayUnavailable
        gateway.side_effect = GatewayUnavailable("Payment gateway is temporarily unavailable.")
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(res.status_code, 400)
        booking = WorkshopBooking.objects.get()
        self.assertEqual(booking.status, BookingStatus.PAYMENT_FAILED)

    def test_public_list_and_detail(self):
        WorkshopSession.objects.create(workshop=self.workshop, date=timezone.localdate() - timedelta(days=2),
                                       start_time=time(10), end_time=time(12), capacity=5)
        res = self.client.get("/api/workshops/")
        self.assertEqual(res.status_code, 200)
        card = res.data["results"][0]
        self.assertEqual(card["price_from"], "899.00")
        self.assertEqual(card["seats_left"], 10)
        self.assertEqual(card["upcoming_session_count"], 1)
        self.assertEqual(card["organiser"]["name"], "Moss Lab")
        detail = self.client.get(f"/api/workshops/{self.workshop.slug}/").data
        self.assertEqual(len(detail["sessions"]), 1, "past sessions must not be offered")
        self.assertEqual(detail["highlights"][0], "3 inch round bowl")

    def test_list_query_count_is_constant(self):
        for i in range(3):
            make_workshop(self.seller, title=f"Workshop {i}")
        with self.assertNumQueries(4):  # count + workshops + prefetched sessions + co-hosts
            self.client.get("/api/workshops/")

    @patch("workshops.views.start_gateway_payment", return_value=FAKE_GATEWAY)
    def test_guest_lookup_requires_matching_email(self, _gateway):
        number = self.client.post(self.url, self.payload, format="json").data["order"]["order_number"]
        lookup = f"/api/workshops/bookings/by-order/{number}/"
        self.assertEqual(self.client.get(lookup).status_code, 404)
        self.assertEqual(self.client.get(lookup, {"email": "other@example.com"}).status_code, 404)
        self.assertEqual(self.client.get(lookup, {"email": "ASHA@example.com"}).status_code, 200)


@override_settings(**TEST_SETTINGS)
class PaymentLifecycleTests(TestCase):
    """Workshop orders ride the product payment pipeline unchanged."""

    def setUp(self):
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.buyer = User.objects.create_user(email="buyer@test.com", username="buyer", password="Pass@123")
        self.workshop, self.option, self.session = make_workshop(self.seller)
        self.booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, user=self.buyer))
        self.payment = Payment.objects.create(
            order=self.booking.order, gateway=PaymentGateway.RAZORPAY,
            razorpay_order_id="rzp_ws_1", amount=self.booking.total_amount,
        )

    @patch("workshops.tasks.send_workshop_booking_emails_task.delay")
    @patch("payments.tasks.cancel_payment_checks")
    def test_capture_confirms_booking_and_keeps_cart(self, _cancel, email_task):
        product = Product.objects.create(name="Java Moss", seller=self.seller, is_active=True)
        variant = ProductVariant.objects.create(product=product, name="Std", base_price=100, price=110, stock=5)
        cart = Cart.objects.create(user=self.buyer)
        CartItem.objects.create(cart=cart, product=product, variant=variant, quantity=1)

        from payments.tasks import _capture_payment
        with self.captureOnCommitCallbacks(execute=True):
            _capture_payment(self.payment, "pay_1")

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(self.booking.confirmed_at)
        self.assertEqual(CartItem.objects.filter(cart=cart).count(), 1, "a workshop order must not clear the cart")
        self.assertTrue(AppNotification.objects.filter(user=self.seller, title="New workshop booking").exists())
        self.assertTrue(AppNotification.objects.filter(user=self.buyer, title="Workshop booked!").exists())
        self.assertFalse(AppNotification.objects.filter(title="Order Placed!").exists())
        email_task.assert_called_once_with(str(self.booking.id))

    @patch("workshops.tasks.send_workshop_booking_emails_task.delay")
    def test_confirmation_is_idempotent(self, email_task):
        order = self.booking.order
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                order.is_paid = True
                order.status = OrderStatus.CONFIRMED
                order.save()
        self.assertEqual(email_task.call_count, 1)
        self.assertEqual(AppNotification.objects.filter(user=self.seller).count(), 1)

    @patch("workshops.tasks.send_workshop_booking_emails_task.delay")
    def test_late_capture_after_failure_still_confirms(self, _email):
        order = self.booking.order
        with self.captureOnCommitCallbacks(execute=True):
            order.status = OrderStatus.FAILED
            order.save()
        with self.captureOnCommitCallbacks(execute=True):
            order.is_paid = True
            order.status = OrderStatus.CONFIRMED
            order.save()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_emails_go_to_attendee_organiser_and_admins(self):
        from django.core import mail
        User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        with self.captureOnCommitCallbacks(execute=True):
            self.booking.order.is_paid = True
            self.booking.order.save()
        recipients = sorted(r for m in mail.outbox for r in m.to)
        self.assertEqual(recipients, ["admin@junglyst.com", "asha@example.com", "org@test.com"])

    def test_buyer_order_list_shows_booking_summary(self):
        client = APIClient()
        client.force_authenticate(self.buyer)
        res = client.get("/api/orders/")
        order = res.data["results"][0] if "results" in res.data else res.data[0]
        self.assertEqual(order["order_type"], "workshop")
        self.assertEqual(order["booking"]["title"], "Terrarium Basics")
        self.assertEqual(order["booking"]["participants"], 2)


@override_settings(**TEST_SETTINGS)
class AdminWorkshopApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        day = (timezone.localdate() + timedelta(days=10)).isoformat()
        self.payload = {
            "title": "Aquarium Nano Build", "category": "aquarium", "seller": str(self.seller.id),
            "status": "published", "commission_percent": "15.00", "city": "Kochi",
            "map_url": "https://maps.google.com/?q=kochi",
            "highlights": ["5 litre cube tank", "  ", "Plants & substrate included"],
            "images": ["https://cdn.example.com/a.jpg"],
            "options": [{"name": "Cube 20cm", "shape": "Cube", "size": "20 cm", "price": "2499.00"}],
            "session_inputs": [{"date": day, "start_time": "10:00", "end_time": "13:00", "capacity": 8}],
        }

    def test_non_superusers_are_denied(self):
        for user in (None, self.seller, User.objects.create_user(email="s@x.com", username="s", password="x", is_staff=True)):
            client = APIClient()
            if user:
                client.force_authenticate(user)
            for url in ("/api/workshops/admin/workshops/", "/api/workshops/admin/bookings/",
                        "/api/workshops/admin/financials/", "/api/workshops/admin/organisers/"):
                self.assertIn(client.get(url).status_code, (401, 403), url)
            self.assertIn(client.post("/api/workshops/admin/workshops/", self.payload, format="json").status_code, (401, 403))

    def test_create_with_options_sessions_and_clean_highlights(self):
        res = self.client.post("/api/workshops/admin/workshops/", self.payload, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        workshop = Workshop.objects.get()
        self.assertEqual(workshop.highlights, ["5 litre cube tank", "Plants & substrate included"])
        self.assertEqual(workshop.created_by, self.admin)
        self.assertEqual(workshop.options.count(), 1)
        self.assertEqual(workshop.sessions.get().capacity, 8)
        self.assertEqual(workshop.slug, "aquarium-nano-build")

    def test_cannot_publish_without_sessions(self):
        res = self.client.post("/api/workshops/admin/workshops/", {**self.payload, "session_inputs": []}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_rejects_unverified_organiser_and_bad_dates(self):
        nobody = User.objects.create_user(email="n@x.com", username="n", password="x")
        self.assertEqual(self.client.post("/api/workshops/admin/workshops/", {**self.payload, "seller": str(nobody.id)},
                                          format="json").status_code, 400)
        bad = {**self.payload, "registration_opens_at": "2026-12-10T10:00:00Z", "registration_closes_at": "2026-12-01T10:00:00Z"}
        self.assertEqual(self.client.post("/api/workshops/admin/workshops/", bad, format="json").status_code, 400)
        bad_session = {**self.payload, "session_inputs": [{**self.payload["session_inputs"][0], "end_time": "09:00"}]}
        self.assertEqual(self.client.post("/api/workshops/admin/workshops/", bad_session, format="json").status_code, 400)

    def test_update_syncs_children_and_keeps_booked_ones(self):
        workshop, option, session = make_workshop(self.seller)
        create_pending_booking(booking_request(workshop, option, session))
        spare = WorkshopOption.objects.create(workshop=workshop, name="Spare", price=Decimal("500"))
        res = self.client.patch(f"/api/workshops/admin/workshops/{workshop.id}/", {
            "options": [{"name": "Hex bowl", "price": "1099.00"}], "session_inputs": [],
            "status": "draft",
        }, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        option.refresh_from_db()
        session.refresh_from_db()
        self.assertFalse(option.is_active, "booked option is deactivated, not deleted")
        self.assertFalse(session.is_active)
        self.assertFalse(WorkshopOption.objects.filter(pk=spare.pk).exists())
        self.assertTrue(workshop.options.filter(name="Hex bowl").exists())

    def test_delete_refused_with_paid_bookings(self):
        workshop, option, session = make_workshop(self.seller)
        booking = create_pending_booking(booking_request(workshop, option, session))
        WorkshopBooking.objects.filter(pk=booking.pk).update(status=BookingStatus.CONFIRMED)
        self.assertEqual(self.client.delete(f"/api/workshops/admin/workshops/{workshop.id}/").status_code, 400)

    def test_bookings_list_filters_and_status_update(self):
        workshop, option, session = make_workshop(self.seller)
        booking = create_pending_booking(booking_request(workshop, option, session))
        res = self.client.get("/api/workshops/admin/bookings/", {"search": "9876543210"})
        self.assertEqual(res.data["count"], 1)
        row = res.data["results"][0]
        self.assertEqual(row["organiser_name"], "Moss Lab")
        self.assertEqual(row["attendee_phone"], "9876543210")
        self.assertEqual(self.client.get("/api/workshops/admin/bookings/", {"status": "paid"}).data["count"], 0)

        url = f"/api/workshops/admin/bookings/{booking.id}/"
        self.assertEqual(self.client.patch(url, {"status": "attended"}, format="json").status_code, 400,
                         "unpaid bookings can't be marked attended")
        WorkshopBooking.objects.filter(pk=booking.pk).update(status=BookingStatus.CONFIRMED)
        res = self.client.patch(url, {"status": "cancelled", "admin_note": "Refunded via UPI"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["status"], "cancelled")
        self.assertIsNotNone(res.data["cancelled_at"])


@override_settings(**TEST_SETTINGS)
class SellerAndFinancialsTests(TestCase):
    def setUp(self):
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.other_seller = make_seller("other@test.com", "Reef Co")
        self.workshop, self.option, self.session = make_workshop(self.seller, capacity=20)
        past = WorkshopSession.objects.create(workshop=self.workshop, date=timezone.localdate() - timedelta(days=3),
                                              start_time=time(10), end_time=time(12), capacity=10)
        self.future_booking = self._paid_booking(self.session, participants=2)   # 1798, payout 1438.40
        self.past_booking = self._paid_booking(past, participants=1)              # 899, payout 719.20

    def _paid_booking(self, session, participants):
        booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants))
        WorkshopBooking.objects.filter(pk=booking.pk).update(
            status=BookingStatus.CONFIRMED, session=session, session_date=session.date)
        return booking

    def test_financials_split_earned_and_upcoming(self):
        WorkshopPayout.objects.create(workshop=self.workshop, seller=self.seller, amount=Decimal("500"))
        fin = workshop_financials(self.workshop)
        self.assertEqual(fin["totals"]["gross"], Decimal("2697.00"))
        self.assertEqual(fin["totals"]["commission"], Decimal("539.40"))
        self.assertEqual(fin["totals"]["seller_payout"], Decimal("2157.60"))
        self.assertEqual(fin["earned"]["seller_payout"], Decimal("719.20"))
        self.assertEqual(fin["paid_out"], Decimal("500"))
        self.assertEqual(fin["balance_due"], Decimal("219.20"))
        self.assertEqual(fin["capacity"], 30)
        self.assertEqual(fin["totals"]["seats"], 3)

    def test_seller_sees_only_their_paid_bookings(self):
        create_pending_booking(booking_request(self.workshop, self.option, self.session))  # unpaid: hidden
        client = APIClient()
        client.force_authenticate(self.seller)
        res = client.get("/api/workshops/seller/bookings/")
        self.assertEqual(res.data["count"], 2)
        self.assertIn("seller_payout", res.data["results"][0])
        upcoming = client.get("/api/workshops/seller/bookings/", {"when": "upcoming"}).data
        self.assertEqual(upcoming["count"], 1)

        client.force_authenticate(self.other_seller)
        self.assertEqual(client.get("/api/workshops/seller/bookings/").data["count"], 0)
        self.assertEqual(client.get("/api/workshops/seller/summary/").data, [])

    def test_seller_summary_and_admin_overview(self):
        client = APIClient()
        client.force_authenticate(self.seller)
        summary = client.get("/api/workshops/seller/summary/").data[0]
        self.assertEqual(summary["gross"], Decimal("2697.00"))
        self.assertEqual(summary["balance_due"], Decimal("719.20"))

        admin = User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        client.force_authenticate(admin)
        res = client.post("/api/workshops/admin/payouts/", {
            "workshop": str(self.workshop.id), "amount": "719.20", "reference": "UTR123",
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        payout = WorkshopPayout.objects.get()
        self.assertEqual(payout.seller, self.seller)
        self.assertEqual(payout.recorded_by, admin)
        row = client.get("/api/workshops/admin/financials/").data[0]
        self.assertEqual(row["balance_due"], Decimal("0.00"))
        self.assertEqual(row["pending_settlement"], Decimal("1438.40"))
        detail = client.get(f"/api/workshops/admin/workshops/{self.workshop.id}/financials/").data
        self.assertEqual(len(detail["payouts"]), 1)
        self.assertEqual(len(detail["sessions"]), 2)

    def test_unpaid_bookings_excluded_from_revenue(self):
        create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=5))
        self.assertEqual(workshop_financials(self.workshop)["totals"]["bookings"], 2)


@override_settings(**TEST_SETTINGS)
class ReleaseAbandonedHoldTests(TestCase):
    """Closing the payment window frees the seats now, unless money was taken."""

    def setUp(self):
        self.seller = make_seller("org@test.com", "Moss Lab")
        self.workshop, self.option, self.session = make_workshop(self.seller, capacity=2)
        self.booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=2))
        Payment.objects.create(order=self.booking.order, gateway=PaymentGateway.RAZORPAY,
                               razorpay_order_id="rzp_ws_hold", amount=self.booking.total_amount)
        self.url = f"/api/workshops/bookings/by-order/{self.booking.order.order_number}/release/"
        self.client = APIClient()

    @patch("payments.tasks.cancel_payment_checks")
    @patch("payments.tasks._razorpay_gateway_status", return_value=("pending", None, None))
    def test_unpaid_hold_is_released_immediately(self, _status, _cancel):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.url, {"email": "asha@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["status"], BookingStatus.PAYMENT_FAILED)
        self.booking.order.refresh_from_db()
        self.assertEqual(self.booking.order.status, OrderStatus.FAILED)
        self.assertEqual(Payment.objects.get(order=self.booking.order).status, "cancelled")
        # Both seats are bookable again straight away, well inside the hold window.
        create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=2))

    @patch("workshops.tasks.send_workshop_booking_emails_task.delay")
    @patch("payments.tasks.cancel_payment_checks")
    @patch("payments.tasks._razorpay_gateway_status", return_value=("captured", "pay_real", {}))
    def test_paid_at_gateway_is_confirmed_not_released(self, _status, _cancel, _email):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.url, {"email": "asha@example.com"}, format="json")
        self.assertEqual(res.data["status"], BookingStatus.CONFIRMED)

    def test_requires_matching_email_for_guests(self):
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 404)
        self.assertEqual(self.client.post(self.url, {"email": "other@example.com"}, format="json").status_code, 404)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.PENDING_PAYMENT)

    def test_confirmed_booking_is_never_released(self):
        WorkshopBooking.objects.filter(pk=self.booking.pk).update(status=BookingStatus.CONFIRMED)
        res = self.client.post(self.url, {"email": "asha@example.com"}, format="json")
        self.assertEqual(res.data["status"], BookingStatus.CONFIRMED)

    def test_admin_seats_booked_ignores_unpaid_holds(self):
        admin = User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        self.client.force_authenticate(admin)
        row = self.client.get("/api/workshops/admin/workshops/").data[0]
        self.assertEqual(row["seats_booked"], 0)


@override_settings(**TEST_SETTINGS)
class OrganiserChangeTests(TestCase):
    """Bookings stay with the organiser they were made under."""

    def setUp(self):
        self.first = make_seller("first@test.com", "Plants Co")
        self.second = make_seller("second@test.com", "Green Water")
        self.workshop, self.option, self.session = make_workshop(self.first, capacity=10)
        self.old = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=2))
        self.workshop.seller = self.second
        self.workshop.save()
        self.new = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=1))
        WorkshopBooking.objects.update(status=BookingStatus.CONFIRMED)

    def _summary(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.get("/api/workshops/seller/summary/").data, client.get("/api/workshops/seller/bookings/").data

    def test_each_seller_sees_only_their_own_bookings_and_money(self):
        summary, bookings = self._summary(self.second)
        self.assertEqual(bookings["count"], 1)
        self.assertEqual(summary[0]["seats_sold"], 1)
        self.assertEqual(summary[0]["gross"], Decimal("899.00"))

        summary, bookings = self._summary(self.first)
        self.assertEqual(bookings["count"], 1, "former organiser keeps their paid booking")
        self.assertEqual(len(summary), 1, "former organiser still sees the workshop")
        self.assertEqual(summary[0]["seats_sold"], 2)
        self.assertEqual(summary[0]["gross"], Decimal("1798.00"))

    def test_payouts_are_counted_per_seller(self):
        WorkshopPayout.objects.create(workshop=self.workshop, seller=self.first, amount=Decimal("100"))
        self.assertEqual(self._summary(self.first)[0][0]["paid_out"], Decimal("100"))
        self.assertEqual(self._summary(self.second)[0][0]["paid_out"], 0)


from .models import WorkshopOrganiser  # noqa: E402
from .services import allocate_by_share  # noqa: E402


class AllocationMathTests(TestCase):
    def test_shares_always_sum_to_the_paisa(self):
        for total, shares in [("1000", ["60", "40"]), ("899", ["33.33", "33.33", "33.34"]), ("0.05", ["50", "50"]),
                              ("1798", ["70", "30"]), ("100", ["100"])]:
            parts = allocate_by_share(Decimal(total), [Decimal(x) for x in shares])
            self.assertEqual(sum(parts), Decimal(total), (total, shares, parts))

    def test_remainder_goes_to_largest_fraction(self):
        self.assertEqual(allocate_by_share(Decimal("10"), [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]),
                         [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")])


def add_cohosts(workshop, *terms):
    WorkshopOrganiser.objects.filter(workshop=workshop).delete()
    for i, (seller, share, commission) in enumerate(terms):
        WorkshopOrganiser.objects.create(workshop=workshop, seller=seller, share_percent=Decimal(share),
                                         commission_percent=Decimal(commission), sort_order=i)


@override_settings(**TEST_SETTINGS)
class CoHostBookingTests(TestCase):
    def setUp(self):
        self.a = make_seller("a@test.com", "Seller A")
        self.b = make_seller("b@test.com", "Seller B")
        self.workshop, self.option, self.session = make_workshop(self.a, capacity=20)
        self.option.price = Decimal("1000")
        self.option.save()
        add_cohosts(self.workshop, (self.a, "60", "20"), (self.b, "40", "15"))

    def _confirmed_booking(self, participants=1):
        booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants))
        WorkshopBooking.objects.filter(pk=booking.pk).update(status=BookingStatus.CONFIRMED)
        booking.refresh_from_db()
        return booking

    def test_booking_is_split_per_cohost(self):
        booking = self._confirmed_booking()
        splits = {s.seller_id: s for s in booking.splits.all()}
        self.assertEqual(splits[self.a.id].gross_amount, Decimal("600.00"))
        self.assertEqual(splits[self.a.id].commission_amount, Decimal("120.00"))
        self.assertEqual(splits[self.a.id].seller_payout, Decimal("480.00"))
        self.assertEqual(splits[self.b.id].gross_amount, Decimal("400.00"))
        self.assertEqual(splits[self.b.id].commission_amount, Decimal("60.00"))
        self.assertEqual(splits[self.b.id].seller_payout, Decimal("340.00"))
        self.assertEqual(booking.commission_amount, Decimal("180.00"))
        self.assertEqual(booking.seller_payout, Decimal("820.00"))
        self.assertEqual(booking.commission_percent, Decimal("18.00"))

    def test_each_cohost_sees_the_booking_with_their_own_payout(self):
        self._confirmed_booking()
        for seller, payout, share in [(self.a, "480.00", "60.00"), (self.b, "340.00", "40.00")]:
            client = APIClient()
            client.force_authenticate(seller)
            row = client.get("/api/workshops/seller/bookings/").data["results"][0]
            self.assertEqual(str(row["seller_payout"]), payout)
            self.assertEqual(str(row["share_percent"]), share)
            summary = client.get("/api/workshops/seller/summary/").data[0]
            self.assertEqual(Decimal(summary["seller_payout"]), Decimal(payout))

    @patch("workshops.tasks.send_workshop_booking_emails_task.delay")
    def test_every_cohost_is_notified_and_emailed(self, _email):
        booking = create_pending_booking(booking_request(self.workshop, self.option, self.session))
        with self.captureOnCommitCallbacks(execute=True):
            booking.order.is_paid = True
            booking.order.save()
        self.assertEqual(AppNotification.objects.filter(title="New workshop booking").count(), 2)
        from django.core import mail
        from .tasks import send_workshop_booking_emails_task
        send_workshop_booking_emails_task(str(booking.id))
        recipients = {r for m in mail.outbox for r in m.to}
        self.assertTrue({"a@test.com", "b@test.com"} <= recipients)

    def test_settlement_is_tracked_per_seller(self):
        booking = self._confirmed_booking()
        WorkshopBooking.objects.filter(pk=booking.pk).update(session_date=timezone.localdate() - timedelta(days=1))
        WorkshopPayout.objects.create(workshop=self.workshop, seller=self.a, amount=Decimal("600"))  # A overpaid
        fin = workshop_financials(self.workshop)
        sellers = {s["seller"]: s for s in fin["sellers"]}
        self.assertEqual(sellers[self.a.id]["balance_due"], Decimal("-120.00"))
        self.assertEqual(sellers[self.b.id]["balance_due"], Decimal("340.00"))
        self.assertEqual(fin["balance_due"], Decimal("340.00"), "an advance to A must not hide what B is owed")

        admin = User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        client = APIClient()
        client.force_authenticate(admin)
        self.assertEqual(client.get("/api/workshops/admin/financials/").data[0]["balance_due"], Decimal("340.00"))
        outsider = make_seller("c@test.com", "Seller C")
        res = client.post("/api/workshops/admin/payouts/", {
            "workshop": str(self.workshop.id), "seller": str(outsider.id), "amount": "10"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = client.post("/api/workshops/admin/payouts/", {
            "workshop": str(self.workshop.id), "seller": str(self.b.id), "amount": "340"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(WorkshopPayout.objects.get(seller=self.b).amount, Decimal("340"))


@override_settings(**TEST_SETTINGS)
class CoHostAdminEditTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="admin@junglyst.com", username="admin", password="x")
        self.a = make_seller("a@test.com", "Seller A")
        self.b = make_seller("b@test.com", "Seller B")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.workshop, self.option, self.session = make_workshop(self.a, capacity=10)
        self.option.price = Decimal("1000")
        self.option.save()
        add_cohosts(self.workshop, (self.a, "100", "20"))
        self.booking = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=1))
        WorkshopBooking.objects.filter(pk=self.booking.pk).update(status=BookingStatus.CONFIRMED)
        self.url = f"/api/workshops/admin/workshops/{self.workshop.id}/"
        self.new_terms = [
            {"seller": str(self.a.id), "share_percent": "50", "commission_percent": "20"},
            {"seller": str(self.b.id), "share_percent": "50", "commission_percent": "10"},
        ]

    def _payouts(self):
        return {s.seller_id: s.seller_payout for s in WorkshopBooking.objects.get(pk=self.booking.pk).splits.all()}

    def test_detail_reports_counts_for_the_save_prompt(self):
        data = self.client.get(self.url).data
        self.assertEqual(data["paid_booking_count"], 1)
        self.assertEqual(data["recalculable_booking_count"], 1)
        self.assertEqual(data["organisers"][0]["seller_name"], "Seller A")

    def test_new_bookings_only_keeps_paid_split(self):
        res = self.client.patch(self.url, {"organisers": self.new_terms}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["recalculated_count"], 0)
        self.assertEqual(self._payouts(), {self.a.id: Decimal("800.00")})
        new = create_pending_booking(booking_request(self.workshop, self.option, self.session, participants=1))
        self.assertEqual({s.seller_id: s.seller_payout for s in new.splits.all()},
                         {self.a.id: Decimal("400.00"), self.b.id: Decimal("450.00")})
        self.workshop.refresh_from_db()
        self.assertEqual(self.workshop.seller, self.a, "lead organiser follows the first co-host")

    def test_recalculate_unsettled_resplits_paid_bookings(self):
        res = self.client.patch(self.url, {"organisers": self.new_terms, "recalculate_unsettled": True}, format="json")
        self.assertEqual(res.data["recalculated_count"], 1)
        self.assertEqual(self._payouts(), {self.a.id: Decimal("400.00"), self.b.id: Decimal("450.00")})
        booking = WorkshopBooking.objects.get(pk=self.booking.pk)
        self.assertEqual(booking.seller_payout, Decimal("850.00"))
        self.assertEqual(booking.total_amount, Decimal("1000.00"), "the amount the buyer paid never changes")

    def test_settled_past_sessions_are_not_recalculated(self):
        WorkshopBooking.objects.filter(pk=self.booking.pk).update(session_date=timezone.localdate() - timedelta(days=2))
        WorkshopPayout.objects.create(workshop=self.workshop, seller=self.a, amount=Decimal("800"))
        self.assertEqual(self.client.get(self.url).data["recalculable_booking_count"], 0)
        res = self.client.patch(self.url, {"organisers": self.new_terms, "recalculate_unsettled": True}, format="json")
        self.assertEqual(res.data["recalculated_count"], 0)
        self.assertEqual(self._payouts(), {self.a.id: Decimal("800.00")})

    def test_unchanged_terms_do_not_recalculate(self):
        same = [{"seller": str(self.a.id), "share_percent": "100", "commission_percent": "20"}]
        res = self.client.patch(self.url, {"organisers": same, "recalculate_unsettled": True}, format="json")
        self.assertEqual(res.data["recalculated_count"], 0)

    def test_share_validation(self):
        cases = [
            ([{**self.new_terms[0], "share_percent": "60"}, self.new_terms[1]], "add up to 100"),
            ([self.new_terms[0], {**self.new_terms[1], "seller": str(self.a.id)}], "only once"),
            ([], "at least one"),
        ]
        for terms, message in cases:
            res = self.client.patch(self.url, {"organisers": terms}, format="json")
            self.assertEqual(res.status_code, 400)
            self.assertIn(message, str(res.data["organisers"]))

    def test_create_with_cohosts(self):
        day = (timezone.localdate() + timedelta(days=5)).isoformat()
        res = self.client.post("/api/workshops/admin/workshops/", {
            "title": "Co-hosted Aquascape", "organisers": self.new_terms,
            "options": [{"name": "Nano", "price": "1500"}],
            "session_inputs": [{"date": day, "start_time": "10:00", "end_time": "12:00", "capacity": 5}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        created = Workshop.objects.get(title="Co-hosted Aquascape")
        self.assertEqual(created.organisers.count(), 2)
        self.assertEqual(created.seller, self.a)

    def test_public_detail_lists_all_cohosts(self):
        add_cohosts(self.workshop, (self.a, "50", "20"), (self.b, "50", "10"))
        data = APIClient().get(f"/api/workshops/{self.workshop.slug}/").data
        self.assertEqual([o["name"] for o in data["organisers"]], ["Seller A", "Seller B"])

    def _session_payload(self, **changes):
        base = {"id": self.session.id, "date": self.session.date.isoformat(), "start_time": "10:00",
                "end_time": "12:00", "capacity": 10}
        return {"session_inputs": [{**base, **changes}]}

    @patch("workshops.tasks.send_session_rescheduled_task.delay")
    def test_rescheduling_updates_bookings_and_notifies(self, notify):
        new_day = (timezone.localdate() + timedelta(days=9)).isoformat()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.patch(self.url, self._session_payload(date=new_day, start_time="15:00", end_time="17:00"),
                                    format="json")
        self.assertEqual(res.status_code, 200, res.content)
        booking = WorkshopBooking.objects.get(pk=self.booking.pk)
        self.assertEqual(booking.session_date.isoformat(), new_day)
        self.assertEqual(booking.session_start, time(15, 0))
        notify.assert_called_once()

    def test_capacity_cannot_drop_below_seats_sold(self):
        WorkshopBooking.objects.filter(pk=self.booking.pk).update(participants=3)
        res = self.client.patch(self.url, self._session_payload(capacity=2), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("3 seats sold", str(res.data))
        self.session.refresh_from_db()
        self.assertEqual(self.session.capacity, 10)
        res = self.client.patch(self.url, self._session_payload(capacity=3), format="json")
        self.assertEqual(res.status_code, 200, res.content)
