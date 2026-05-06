import os
import django
import random
from decimal import Decimal

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')

# Temporary override for Railway DB (provided by user)
os.environ['DB_HOST'] = 'shuttle.proxy.rlwy.net'
os.environ['DB_PORT'] = '44939'
os.environ['DB_NAME'] = 'railway'
os.environ['DB_USER'] = 'postgres'
os.environ['DB_PASSWORD'] = 'gbvAbFfiHVijgytXsDmdfdRkvKVXFKgY'

django.setup()

from django.contrib.auth import get_user_model
from core.models import Category, SubCategory, Product, ProductVariant, ProductImage
from sellers.models import SellerProfile
from django.utils.text import slugify

User = get_user_model()

def seed():
    print("🌿 Starting Junglyst Dev Seeding...")

    # 1. Create Admin
    admin_user, _ = User.objects.get_or_create(
        email='admin@junglyst.com',
        defaults={'username': 'admin', 'role': 'admin', 'is_staff': True, 'is_superuser': True}
    )
    admin_user.set_password('Password123')
    admin_user.save()

    # 2. Create Categories
    categories_data = [
        {'name': 'Aquatic Plants', 'gst': 12, 'comm': 15},
        {'name': 'Rare Ferns', 'gst': 12, 'comm': 20},
        {'name': 'Mosses & Liverworts', 'gst': 5, 'comm': 10},
    ]

    for cat_info in categories_data:
        cat, _ = Category.objects.get_or_create(
            name=cat_info['name'],
            defaults={
                'slug': slugify(cat_info['name']),
                'gst_percentage': cat_info['gst'],
                'commission_rate': cat_info['comm']
            }
        )
        print(f"  - Category: {cat.name}")

    # 3. Create Growers
    growers = [
        {'email': 'amazon_botanics@test.com', 'name': 'Amazon Botanics', 'bio': 'Specializing in rare South American specimens.'},
        {'email': 'aqua_studio@test.com', 'name': 'Aqua Studio', 'bio': 'Boutique aquascaping and rare moss culture.'},
    ]

    for g in growers:
        user, created = User.objects.get_or_create(
            email=g['email'],
            defaults={'username': slugify(g['name']), 'role': 'grower'}
        )
        user.set_password('Password123')
        user.save()

        SellerProfile.objects.update_or_create(
            user=user,
            defaults={
                'store_name': g['name'],
                'bio': g['bio'],
                'slug': slugify(g['name']),
                'identity_verified': True
            }
        )
        print(f"  - Grower: {g['name']}")

    # 4. Create Products
    cat_aquatic = Category.objects.get(name='Aquatic Plants')
    cat_ferns = Category.objects.get(name='Rare Ferns')
    seller_amazon = User.objects.get(email='amazon_botanics@test.com')

    products_data = [
        {
            'name': 'Peperomia Green Creeper',
            'desc': 'Elegant trailing plant with thick, waxy heart-shaped leaves. Perfect for terrariums.',
            'price': 50,
            'original': 100,
            'stock': 1,
            'trending': True,
            'image': 'https://images.unsplash.com/photo-1545239351-ef35f43d514b?auto=format&fit=crop&q=80&w=800',
            'category': cat_ferns
        },
        {
            'name': 'Creeping Charlie (Pilea nummulariifolia)',
            'desc': 'Fast-growing creeper with textured, bright green leaves. Great for ground cover.',
            'price': 30,
            'original': 70,
            'stock': 0,
            'trending': True,
            'image': 'https://images.unsplash.com/photo-1596547609652-9cf5d8d76921?auto=format&fit=crop&q=80&w=800',
            'category': cat_ferns
        },
        {
            'name': 'Bucephalandra Ghost 2011',
            'desc': 'Ultra-rare rhizome plant with deep purple hues and iridescent leaves.',
            'price': 1200,
            'original': 1800,
            'stock': 5,
            'trending': False,
            'image': 'https://images.unsplash.com/photo-1620674156044-52b714665d46?auto=format&fit=crop&q=80&w=800',
            'category': cat_aquatic
        }
    ]

    for p_info in products_data:
        product, _ = Product.objects.update_or_create(
            name=p_info['name'],
            defaults={
                'description': p_info['desc'],
                'seller': seller_amazon,
                'is_active': True,
                'is_rare': p_info['trending'],
                'slug': slugify(p_info['name'])
            }
        )
        product.categories.add(p_info['category'])

        # Variant for Price/Stock
        ProductVariant.objects.update_or_create(
            product=product,
            name='Standard',
            defaults={
                'base_price': Decimal(str(p_info['price'])),
                'stock': p_info['stock'],
                'gst_rate': 12,
                'commission_rate': 15
            }
        )

        # Image
        ProductImage.objects.update_or_create(
            product=product,
            defaults={'image_url': p_info['image'], 'is_primary': True}
        )
        print(f"  - Synchronized: {product.name}")

    print("✅ Seeding Complete!")

if __name__ == '__main__':
    seed()
