"""
Coupon engine + checkout integration tests.
Run with:
    python manage.py test coupons --verbosity=2
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import Category, Product, ProductVariant
from orders.models import Order, SubOrder, OrderItem
from payments.models import PaymentGatewaySettings
from sellers.models import SellerProfile
from shipping.models import ShippingAddress

from .engine import evaluate_coupon, CouponError, _allocate
from .models import Coupon, CouponRedemption, DiscountType, FundedBy

User = get_user_model()

FAKE_RZP = {"id": "rzp_ord_COUPON", "amount": 1, "currency": "INR"}


def _seller(username, store):
    u = User.objects.create_user(email=f"{username}@test.com", username=username,
                                 password="Pass@123", role="grower", is_verified_seller=True,
                                 price_is_buyer_final=True)
    SellerProfile.objects.create(user=u, store_name=store, slug=username,
                                 location_pincode="560001", pickup_address="x",
                                 shiprocket_pickup_location=store)
    return u


def _product(seller, name, category, price, stock=10):
    p = Product.objects.create(name=name, seller=seller, is_active=True, is_draft=False)
    p.categories.add(category)
    v = ProductVariant.objects.create(
        product=p, name="Std", base_price=price, price=price, stock=stock,
        weight=Decimal("0.5"), length=Decimal("10"), width=Decimal("10"), height=Decimal("10"),
        packed_weight_grams=200,
    )
    return p, v


class _Fixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.buyer = User.objects.create_user(email="buyer@test.com", username="buyer",
                                             password="Pass@123", role="collector")
        cls.seller_a = _seller("sellera", "Aqua Store A")
        cls.seller_b = _seller("sellerb", "Aqua Store B")
        cls.plants = Category.objects.create(name="Aquatic Plants", slug="aquatic-plants", shipping_type="plant", gst_percentage=Decimal("5"))
        cls.fish = Category.objects.create(name="Fish", slug="fish", shipping_type="plant", gst_percentage=Decimal("0"))

        cls.a_plant, cls.a_plant_v = _product(cls.seller_a, "A Plant", cls.plants, Decimal("400.00"))
        cls.a_fish, cls.a_fish_v = _product(cls.seller_a, "A Fish", cls.fish, Decimal("300.00"))
        cls.b_plant, cls.b_plant_v = _product(cls.seller_b, "B Plant", cls.plants, Decimal("500.00"))

        cls.coupon = Coupon.objects.create(
            code="aplants10", name="10% off Store A plants",
            discount_type=DiscountType.PERCENT, discount_value=Decimal("10"),
            funded_by=FundedBy.SELLER, seller=cls.seller_a, min_cart_value=Decimal("1000"),
        )
        cls.coupon.categories.add(cls.plants)

        cls.address = ShippingAddress.objects.create(
            user=cls.buyer, full_name="B", phone="9876543210", address_line1="x",
            city="Mumbai", state="MH", pincode="400001",
        )
        PaymentGatewaySettings.objects.update_or_create(pk=1, defaults={"active_gateway": "razorpay"})

    def _cart(self, *lines):
        cart = Cart.objects.create(user=self.buyer)
        for variant, qty in lines:
            CartItem.objects.create(cart=cart, product=variant.product, variant=variant, quantity=qty)
        return cart, list(cart.items.select_related('product', 'variant').prefetch_related('product__categories'))


class EngineTests(_Fixtures):

    def test_code_is_case_insensitive_and_stored_upper(self):
        self.assertEqual(self.coupon.code, "APLANTS10")
        _, items = self._cart((self.a_plant_v, 3))
        self.assertEqual(evaluate_coupon("  aplants10 ", items).coupon, self.coupon)

    def test_discount_only_on_matching_seller_and_category(self):
        # A plant 400×2 = 800 (eligible), A fish 300 (wrong category), B plant 500 (wrong seller)
        _, items = self._cart((self.a_plant_v, 2), (self.a_fish_v, 1), (self.b_plant_v, 1))
        r = evaluate_coupon("APLANTS10", items)
        self.assertEqual(r.cart_subtotal, Decimal("1600.00"))
        self.assertEqual(r.eligible_subtotal, Decimal("800.00"))
        self.assertEqual(r.discount_amount, Decimal("80.00"))
        by_variant = {it.variant_id: d for it, d in zip(items, r.item_discounts)}
        self.assertEqual(by_variant[self.a_plant_v.id], Decimal("80.00"))
        self.assertEqual(by_variant[self.a_fish_v.id], Decimal("0"))
        self.assertEqual(by_variant[self.b_plant_v.id], Decimal("0"))

    def test_one_matching_item_is_enough(self):
        # only 1 eligible item, rest of the cart carries it past min value
        _, items = self._cart((self.a_plant_v, 1), (self.b_plant_v, 2))
        r = evaluate_coupon("APLANTS10", items)
        self.assertEqual(r.discount_amount, Decimal("40.00"))

    def test_min_cart_value_uses_whole_cart(self):
        _, items = self._cart((self.a_plant_v, 1), (self.a_fish_v, 1))   # 700 < 1000
        with self.assertRaises(CouponError) as cm:
            evaluate_coupon("APLANTS10", items)
        self.assertIn("300.00 more", cm.exception.message)

    def test_no_matching_item_rejected(self):
        _, items = self._cart((self.b_plant_v, 3))   # 1500, but wrong seller
        with self.assertRaises(CouponError) as cm:
            evaluate_coupon("APLANTS10", items)
        self.assertIn("Aqua Store A", cm.exception.message)
        self.assertIn("Aquatic Plants", cm.exception.message)

    def test_max_discount_cap_and_fixed_type(self):
        self.coupon.max_discount_amount = Decimal("50")
        self.coupon.save()
        _, items = self._cart((self.a_plant_v, 3))   # 1200 → 10% = 120 → capped 50
        self.assertEqual(evaluate_coupon("APLANTS10", items).discount_amount, Decimal("50.00"))

        fixed = Coupon.objects.create(code="FLAT150", name="flat", discount_type=DiscountType.FIXED,
                                      discount_value=Decimal("150"), min_cart_value=0)
        self.assertEqual(evaluate_coupon("FLAT150", items).discount_amount, Decimal("150.00"))
        # fixed never exceeds eligible subtotal
        _, small = self._cart((self.a_fish_v, 1))
        self.assertEqual(evaluate_coupon("FLAT150", small).discount_amount, Decimal("150.00"))
        big = Coupon.objects.create(code="FLAT9999", name="flat", discount_type=DiscountType.FIXED,
                                    discount_value=Decimal("9999"), min_cart_value=0)
        self.assertEqual(evaluate_coupon("FLAT9999", small).discount_amount, Decimal("300.00"))

    def test_validity_window_and_inactive(self):
        _, items = self._cart((self.a_plant_v, 3))
        self.coupon.is_active = False
        self.coupon.save()
        with self.assertRaises(CouponError):
            evaluate_coupon("APLANTS10", items)
        self.coupon.is_active = True
        self.coupon.valid_until = timezone.now() - timedelta(days=1)
        self.coupon.save()
        with self.assertRaisesMessage(CouponError, "expired"):
            evaluate_coupon("APLANTS10", items)
        with self.assertRaises(CouponError):
            evaluate_coupon("NOPE", items)

    def test_usage_limits_count_only_paid_uncancelled_orders(self):
        _, items = self._cart((self.a_plant_v, 3))
        self.coupon.usage_limit_total = 1
        self.coupon.usage_limit_per_user = 1
        self.coupon.save()

        def mk_order(status, paid):
            o = Order.objects.create(order_number=f"JNG-T-{status}{paid}", user=self.buyer,
                                     shipping_address={}, subtotal=1, total_amount=1,
                                     coupon=self.coupon, coupon_code="APLANTS10",
                                     discount_amount=10, status=status, is_paid=paid)
            return o

        mk_order("pending", False)          # not paid → no redemption
        self.assertEqual(CouponRedemption.objects.count(), 0)
        evaluate_coupon("APLANTS10", items, user=self.buyer)   # still allowed

        paid = mk_order("confirmed", True)  # signal records redemption
        self.assertEqual(CouponRedemption.objects.count(), 1)
        with self.assertRaisesMessage(CouponError, "usage limit"):
            evaluate_coupon("APLANTS10", items, user=self.buyer)

        paid.status = "cancelled"
        paid.save()                         # signal releases the slot
        self.assertEqual(CouponRedemption.objects.count(), 0)
        evaluate_coupon("APLANTS10", items, user=self.buyer)

    def test_allocation_sums_exactly(self):
        parts = _allocate(Decimal("10.00"), [Decimal("1"), Decimal("1"), Decimal("1")])
        self.assertEqual(sum(parts), Decimal("10.00"))
        self.assertEqual(sorted(parts), [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")])
        self.assertEqual(_allocate(Decimal("5"), [Decimal("0"), Decimal("2")]), [Decimal("0"), Decimal("5.00")])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CheckoutIntegrationTests(_Fixtures):

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.buyer)

    def _checkout(self, cart, code, item_ids=None):
        payload = {"cart_id": str(cart.id), "address_id": str(self.address.id), "coupon_code": code}
        if item_ids:
            payload["item_ids"] = item_ids
        with patch("orders.views.create_razorpay_order", return_value=FAKE_RZP), \
             patch("payments.tasks.schedule_payment_checks"), \
             patch("orders.views.check_pincode_deliverable", return_value=(True, "")):
            return self.client.post("/api/orders/checkout/", payload, format="json")

    def test_apply_preview_endpoint(self):
        cart, items = self._cart((self.a_plant_v, 2), (self.b_plant_v, 1))
        resp = self.client.post("/api/coupons/apply/", {"cart_id": str(cart.id), "code": "aplants10"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["discount_amount"], "80.00")
        self.assertTrue(resp.data["seller_funded"])
        plant_row = next(it for it in items if it.variant_id == self.a_plant_v.id)
        self.assertEqual(resp.data["item_discounts"], {str(plant_row.id): "80.00"})

        resp = self.client.post("/api/coupons/apply/", {"cart_id": str(cart.id), "code": "BOGUS"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_guest_items_preview(self):
        client = APIClient()
        resp = client.post("/api/coupons/apply/", {
            "code": "APLANTS10",
            "items": [{"variant_id": str(self.a_plant_v.id), "quantity": 3}],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["discount_amount"], "120.00")

    def test_seller_funded_checkout_splits_discount_per_seller(self):
        cart, _ = self._cart((self.a_plant_v, 2), (self.a_fish_v, 1), (self.b_plant_v, 1))
        resp = self._checkout(cart, "APLANTS10")
        self.assertEqual(resp.status_code, 201, resp.data)

        order = Order.objects.get(id=resp.data["order"]["id"])
        self.assertEqual(order.coupon, self.coupon)
        self.assertEqual(order.coupon_code, "APLANTS10")
        self.assertEqual(order.subtotal, Decimal("1600.00"))
        self.assertEqual(order.discount_amount, Decimal("80.00"))
        self.assertEqual(order.total_amount, Decimal("1520.00") + order.shipping_fee)
        self.assertEqual(Decimal(str(resp.data["amount"])), order.total_amount)
        # GST (5% on plants, inclusive) worked out on the discounted A-plant line
        # plus the untouched B-plant line: (800-80)*5/105 + 500*5/105
        self.assertAlmostEqual(float(order.gst_total), (720 + 500) * 5 / 105, places=1)

        sub_a = SubOrder.objects.get(order=order, seller=self.seller_a)
        sub_b = SubOrder.objects.get(order=order, seller=self.seller_b)
        self.assertEqual(sub_a.discount_amount, Decimal("80.00"))
        self.assertEqual(sub_a.seller_total, Decimal("1100.00") + sub_a.shipping_fee - Decimal("80.00"))
        self.assertEqual(sub_b.discount_amount, Decimal("0"))
        self.assertEqual(sub_b.seller_total, Decimal("500.00") + sub_b.shipping_fee)

        plant_line = OrderItem.objects.get(order=order, variant=self.a_plant_v)
        self.assertEqual(plant_line.discount_amount, Decimal("80.00"))
        self.assertEqual(OrderItem.objects.get(order=order, variant=self.b_plant_v).discount_amount, Decimal("0"))

        # not paid yet → no redemption; pay it → redemption recorded once
        self.assertFalse(CouponRedemption.objects.filter(order=order).exists())
        order.is_paid = True
        order.status = "confirmed"
        order.save()
        order.save()
        self.assertEqual(CouponRedemption.objects.filter(order=order).count(), 1)
        self.assertEqual(CouponRedemption.objects.get(order=order).discount_amount, Decimal("80.00"))

    def test_platform_funded_leaves_seller_total_untouched(self):
        self.coupon.funded_by = FundedBy.PLATFORM
        self.coupon.save()
        cart, _ = self._cart((self.a_plant_v, 3))
        resp = self._checkout(cart, "APLANTS10")
        self.assertEqual(resp.status_code, 201, resp.data)
        order = Order.objects.get(id=resp.data["order"]["id"])
        sub = order.sub_orders.get()
        self.assertEqual(order.discount_amount, Decimal("120.00"))
        self.assertEqual(sub.discount_amount, Decimal("120.00"))
        self.assertEqual(sub.seller_total, Decimal("1200.00") + sub.shipping_fee)   # payout not reduced

    def test_invalid_coupon_blocks_checkout(self):
        cart, _ = self._cart((self.a_plant_v, 1))   # 400 < min 1000
        resp = self._checkout(cart, "APLANTS10")
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.data.get("coupon_error"))
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_without_coupon_unchanged(self):
        cart, _ = self._cart((self.a_plant_v, 1))
        resp = self._checkout(cart, "")
        self.assertEqual(resp.status_code, 201, resp.data)
        order = Order.objects.get(id=resp.data["order"]["id"])
        self.assertIsNone(order.coupon)
        self.assertEqual(order.discount_amount, Decimal("0"))
        self.assertEqual(order.total_amount, Decimal("400.00") + order.shipping_fee)


class AdminApiTests(_Fixtures):

    def setUp(self):
        self.admin = User.objects.create_superuser(email="admin@test.com", username="admin", password="Admin@123")
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_non_admin_is_rejected(self):
        c = APIClient()
        c.force_authenticate(user=self.buyer)
        self.assertEqual(c.get("/api/coupons/admin/").status_code, 403)
        self.assertEqual(APIClient().get("/api/coupons/admin/").status_code, 401)

    def test_list_create_update_delete(self):
        resp = self.client.get("/api/coupons/admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c["code"] for c in resp.data], ["APLANTS10"])
        self.assertEqual(resp.data[0]["seller_name"], "Aqua Store A")
        self.assertEqual(resp.data[0]["category_names"], ["Aquatic Plants"])

        resp = self.client.post("/api/coupons/admin/", {
            "code": "fish20", "name": "20% off fish", "discount_type": "percent", "discount_value": "20",
            "funded_by": "platform", "categories": [self.fish.id], "min_cart_value": "500",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["code"], "FISH20")
        cid = resp.data["id"]
        self.assertEqual(Coupon.objects.get(id=cid).created_by, self.admin)

        # duplicate code + bad percent + seller-funded without seller are rejected
        self.assertEqual(self.client.post("/api/coupons/admin/", {
            "code": "aplants10", "name": "dup", "discount_type": "percent", "discount_value": "5"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/coupons/admin/", {
            "code": "BAD", "name": "x", "discount_type": "percent", "discount_value": "150"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/coupons/admin/", {
            "code": "BAD2", "name": "x", "discount_type": "fixed", "discount_value": "50", "funded_by": "seller"}, format="json").status_code, 400)

        resp = self.client.patch(f"/api/coupons/admin/{cid}/", {"is_active": False, "discount_value": "25"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["is_active"])
        self.assertEqual(resp.data["discount_label"], "25% off")

        self.assertEqual(self.client.delete(f"/api/coupons/admin/{cid}/").status_code, 204)
        self.assertFalse(Coupon.objects.filter(id=cid).exists())          # hidden from default manager
        self.assertTrue(Coupon.all_objects.filter(id=cid, is_deleted=True).exists())  # soft-deleted
