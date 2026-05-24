"""
Hybrid Tier Testing & Setup Helper

Usage:
  python manage.py shell < hybrid_tier_test_setup.py

This script:
1. Creates test sellers with light/heavy/hybrid shipping configs
2. Creates test products with light/heavy items
3. Provides utilities to test hybrid cart detection
"""

from decimal import Decimal
from core.models import Product, ProductVariant, Category, SubCategory, User
from sellers.models import SellerProfile, SellerShippingConfig, ShippingDefaultConfig


def setup_default_shipping_configs():
    """Create platform-wide default shipping rates for light, heavy, hybrid."""
    defaults = [
        {
            'item_category': 'light',
            'tier1_max': Decimal('300.00'),
            'tier1_fee': Decimal('50.00'),
            'tier2_max': Decimal('800.00'),
            'tier2_fee': Decimal('25.00'),
        },
        {
            'item_category': 'heavy',
            'tier1_max': Decimal('400.00'),
            'tier1_fee': Decimal('150.00'),
            'tier2_max': Decimal('1200.00'),
            'tier2_fee': Decimal('75.00'),
        },
        {
            'item_category': 'hybrid',
            'tier1_max': Decimal('600.00'),
            'tier1_fee': Decimal('100.00'),
            'tier2_max': Decimal('1500.00'),
            'tier2_fee': Decimal('50.00'),
        },
    ]

    for d in defaults:
        obj, created = ShippingDefaultConfig.objects.update_or_create(
            item_category=d['item_category'],
            defaults={
                'tier1_max': d['tier1_max'],
                'tier1_fee': d['tier1_fee'],
                'tier2_max': d['tier2_max'],
                'tier2_fee': d['tier2_fee'],
            }
        )
        status = "CREATED" if created else "UPDATED"
        print(f"[{status}] Default {d['item_category']} tier: {d['tier1_max']} → ₹{d['tier1_fee']}, {d['tier2_max']} → ₹{d['tier2_fee']}")


def create_test_seller_with_configs(seller_username='test_seller_hybrid', store_name='Hybrid Test Store'):
    """Create a test seller with all three shipping configurations."""
    user, created = User.objects.get_or_create(
        username=seller_username,
        defaults={'email': f'{seller_username}@example.com', 'is_staff': False}
    )

    seller_profile, created = SellerProfile.objects.get_or_create(
        user=user,
        defaults={
            'store_name': store_name,
            'slug': seller_username,
        }
    )
    print(f"[{'CREATED' if created else 'FETCHED'}] Seller: {store_name} ({user.id})")

    configs = [
        {
            'item_category': 'light',
            'tier1_max': Decimal('300.00'),
            'tier1_fee': Decimal('50.00'),
            'tier2_max': Decimal('800.00'),
            'tier2_fee': Decimal('25.00'),
        },
        {
            'item_category': 'heavy',
            'tier1_max': Decimal('400.00'),
            'tier1_fee': Decimal('150.00'),
            'tier2_max': Decimal('1200.00'),
            'tier2_fee': Decimal('75.00'),
        },
        {
            'item_category': 'hybrid',
            'tier1_max': Decimal('600.00'),
            'tier1_fee': Decimal('100.00'),
            'tier2_max': Decimal('1500.00'),
            'tier2_fee': Decimal('50.00'),
        },
    ]

    for cfg in configs:
        config, created = SellerShippingConfig.objects.update_or_create(
            seller=user,
            item_category=cfg['item_category'],
            defaults={
                'tier1_max': cfg['tier1_max'],
                'tier1_fee': cfg['tier1_fee'],
                'tier2_max': cfg['tier2_max'],
                'tier2_fee': cfg['tier2_fee'],
                'show_nudge_products': cfg['item_category'] == 'light',
            }
        )
        status = "CREATED" if created else "UPDATED"
        print(f"  [{status}] {cfg['item_category']} tier config")

    return user


def create_test_products(seller_user):
    """Create test products with light and heavy items."""
    # Get or create category
    category, _ = Category.objects.get_or_create(
        name='Test Aquatic Plants',
        defaults={'gst_percentage': 18.0, 'commission_rate': 10.0}
    )

    # Create light product
    light_product, _ = Product.objects.get_or_create(
        slug='test-light-plant',
        defaults={
            'name': 'Test Light Plant',
            'description': 'A test light item for hybrid tier testing',
            'seller': seller_user,
        }
    )
    light_product.categories.add(category)

    light_variant, created = ProductVariant.objects.get_or_create(
        sku='TEST-LIGHT-001',
        defaults={
            'product': light_product,
            'name': 'Single Pot',
            'base_price': Decimal('100.00'),
            'gst_rate': Decimal('18.00'),
            'commission_rate': Decimal('10.00'),
            'item_category': 'light',
            'packed_weight_grams': 200,
            'stock': 50,
        }
    )
    print(f"[{'CREATED' if created else 'FETCHED'}] Light variant: {light_variant}")

    # Create heavy product
    heavy_product, _ = Product.objects.get_or_create(
        slug='test-heavy-substrate',
        defaults={
            'name': 'Test Heavy Substrate',
            'description': 'A test heavy item for hybrid tier testing',
            'seller': seller_user,
        }
    )
    heavy_product.categories.add(category)

    heavy_variant, created = ProductVariant.objects.get_or_create(
        sku='TEST-HEAVY-001',
        defaults={
            'product': heavy_product,
            'name': '5kg Bag',
            'base_price': Decimal('500.00'),
            'gst_rate': Decimal('18.00'),
            'commission_rate': Decimal('10.00'),
            'item_category': 'heavy',
            'packed_weight_grams': 5200,
            'stock': 30,
        }
    )
    print(f"[{'CREATED' if created else 'FETCHED'}] Heavy variant: {heavy_variant}")

    return light_variant, heavy_variant


def print_test_scenarios(light_variant, heavy_variant, seller_user):
    """Print test scenarios with expected shipping tiers."""
    print("\n" + "="*70)
    print("TEST SCENARIOS FOR HYBRID TIER")
    print("="*70)

    scenarios = [
        {
            'name': 'Light-only cart (₹100)',
            'items': [{'variant': light_variant, 'qty': 1}],
            'expected_category': 'light',
            'expected_fee': 'tier1_fee (₹50)',
        },
        {
            'name': 'Heavy-only cart (₹500)',
            'items': [{'variant': heavy_variant, 'qty': 1}],
            'expected_category': 'heavy',
            'expected_fee': 'tier1_fee (₹150)',
        },
        {
            'name': 'Hybrid cart (₹100 + ₹500 = ₹600)',
            'items': [
                {'variant': light_variant, 'qty': 1},
                {'variant': heavy_variant, 'qty': 1},
            ],
            'expected_category': 'hybrid',
            'expected_fee': 'tier1_fee (₹100)',
        },
        {
            'name': 'Light + Light cart (₹100 + ₹100 = ₹200)',
            'items': [
                {'variant': light_variant, 'qty': 2},
            ],
            'expected_category': 'light',
            'expected_fee': 'tier1_fee (₹50)',
        },
        {
            'name': 'Heavy + Heavy cart (₹500 + ₹500 = ₹1000)',
            'items': [
                {'variant': heavy_variant, 'qty': 2},
            ],
            'expected_category': 'heavy',
            'expected_fee': 'tier2_fee (₹75)',
        },
    ]

    for i, scenario in enumerate(scenarios, 1):
        print(f"\n[Scenario {i}] {scenario['name']}")
        # print(f"  Items: {', '.join(f\"{item['qty']}x {item['variant'].product.name}\" for item in scenario['items'])}")
        print(f"  Expected Category: {scenario['expected_category']}")
        print(f"  Expected Shipping Fee: {scenario['expected_fee']}")
        print(f"  → Seller from: {seller_user.seller_profile.store_name}")


# ── Run all setup ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Starting Hybrid Tier Setup...\n")

    # 1. Setup defaults
    print("[1] Setting up default shipping configs...")
    setup_default_shipping_configs()

    # 2. Create test seller
    print("\n[2] Creating test seller with shipping configs...")
    seller_user = create_test_seller_with_configs()

    # 3. Create test products
    print("\n[3] Creating test products...")
    light_variant, heavy_variant = create_test_products(seller_user)

    # 4. Print test scenarios
    print_test_scenarios(light_variant, heavy_variant, seller_user)

    print("\n" + "="*70)
    print("SETUP COMPLETE!")
    print("="*70)
    print("\nTo test hybrid tier detection:")
    print("1. Go to API: POST /api/cart/add_item/")
    print("   - Add light variant (light-only cart)")
    print("   - Check cart response → shipping_category should be 'light'")
    print("\n2. Go to API: POST /api/cart/add_item/")
    print("   - Add heavy variant (now mixed cart)")
    print("   - Check cart response → shipping_category should be 'hybrid'")
    print("\n3. Go to API: POST /api/orders/checkout/")
    print("   - Submit checkout with both items")
    print("   - Verify shipping_fee matches hybrid tier config (₹100)")
    print("\nNote: Frontend should display different shipping estimates based on")
    print("seller_weight_summary[].shipping_category returned from cart API.")
