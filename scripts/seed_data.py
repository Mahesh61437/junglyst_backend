import os
import django
import sys
from django.utils.text import slugify

# Add the project root to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Category, Product, ProductVariant, ProductImage
from sellers.models import SellerProfile

User = get_user_model()

def seed():
    print("Starting Seeding...")
    
    # 1. Create Superuser
    if not User.objects.filter(email='admin@junglyst.com').exists():
        admin = User.objects.create_superuser(
            email='admin@junglyst.com',
            username='admin',
            password='Password123',
            role='admin'
        )
        print("Created Superuser: admin@junglyst.com / Password123")
    else:
        admin = User.objects.get(email='admin@junglyst.com')

    # 2. Create Sellers
    sellers_data = [
        {'email': 'greenery@seller.com', 'store': 'The Greenery', 'city': 'Bangalore'},
        {'email': 'botany@seller.com', 'store': 'Botany Bay', 'city': 'Pune'},
    ]
    
    for s_data in sellers_data:
        if not User.objects.filter(email=s_data['email']).exists():
            user = User.objects.create_user(
                email=s_data['email'],
                username=s_data['store'].lower().replace(' ', '_'),
                password='Password123',
                role='grower'
            )
            profile, _ = SellerProfile.objects.get_or_create(
                user=user,
                defaults={
                    'store_name': s_data['store'],
                    'slug': slugify(s_data['store']),
                    'location_city': s_data['city'],
                    'bio': f"Boutique seller of premium specimens from {s_data['city']}"
                }
            )
            print(f"Created Seller: {s_data['store']}")
        else:
            user = User.objects.get(email=s_data['email'])

    # 3. Create Categories
    cats = [
        {'name': 'Aroids', 'gst': 12, 'slug': 'aroids'},
        {'name': 'Hoya', 'gst': 5, 'slug': 'hoya'},
        {'name': 'Terrarium Plants', 'gst': 12, 'slug': 'terrarium-plants'},
    ]
    for cat in cats:
        Category.objects.get_or_create(
            name=cat['name'],
            slug=cat['slug'],
            defaults={'gst_percentage': cat['gst']}
        )
    print("Created Categories")

    # 4. Create Products
    try:
        aroids_cat = Category.objects.get(slug='aroids')
        seller = User.objects.get(email='greenery@seller.com')
        
        products = [
            {
                'name': 'Monstera Albo Variegata',
                'desc': 'Stunning variegated Monstera with high-contrast white patches.',
                'variants': [
                    {'name': '2 Leaf Cutting', 'price': 4500, 'stock': 5},
                    {'name': 'Established Plant', 'price': 12000, 'stock': 2}
                ]
            },
            {
                'name': 'Philodendron Pink Princess',
                'desc': 'Highly sought-after Philodendron with bubblegum pink variegation.',
                'variants': [
                    {'name': 'Standard Pot', 'price': 2500, 'stock': 10}
                ]
            },
            {
                'name': 'Hoya Carnosa Compacta',
                'desc': 'The Hindu Rope plant with uniquely twisted, waxy leaves.',
                'variants': [
                    {'name': 'Small Hanging Basket', 'price': 1800, 'stock': 8}
                ]
            }
        ]

        for p_data in products:
            p, created = Product.objects.get_or_create(
                name=p_data['name'],
                slug=slugify(p_data['name']),
                defaults={
                    'description': p_data['desc'],
                    'seller': seller,
                }
            )
            p.categories.add(aroids_cat)
            
            for v_data in p_data['variants']:
                ProductVariant.objects.get_or_create(
                    product=p,
                    name=v_data['name'],
                    defaults={
                        'price': v_data['price'],
                        'stock': v_data['stock'],
                        'sku': f"{p.slug[:5].upper()}-{v_data['name'][:3].upper()}-{uuid.uuid4().hex[:4].upper()}" if 'uuid' in globals() else f"{p.slug[:5].upper()}-{v_data['name'][:3].upper()}"
                    }
                )
            
            # Add a dummy image
            ProductImage.objects.get_or_create(
                product=p,
                is_primary=True,
                defaults={'image_url': 'https://images.unsplash.com/photo-1614594975525-e45190c55d0b?q=80&w=1000&auto=format&fit=crop'}
            )
        print("Created Products and Variants")
    except Exception as e:
        print(f"Error seeding products: {e}")

    print("Seeding Complete!")

if __name__ == '__main__':
    seed()
