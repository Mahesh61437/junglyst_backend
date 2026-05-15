
import os
import django
import sys

# Setup Django environment
sys.path.append('/Users/mahesh/Desktop/junglyst/junglyst_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from sellers.models import SellerProfile

def seed_seller_images():
    # Final stable curated premium images for seeding
    assets = [
        {
            'name': "admin's Sanctuary",
            'logo': 'https://images.unsplash.com/photo-1545239351-ef35f43d514b?w=400',
            'banner': 'https://images.unsplash.com/photo-1459411552884-841db9b3cc2a?w=2000'
        },
        {
            'name': 'Amazon Botanics',
            'logo': 'https://images.unsplash.com/photo-1512428559087-560fa5ceab42?w=400',
            'banner': 'https://images.unsplash.com/photo-1520923642038-b4259acecbd7?w=2000'
        },
        {
            'name': 'Aqua Studio',
            'logo': 'https://images.unsplash.com/photo-1518531933037-91b2f5f229cc?w=400',
            'banner': 'https://images.unsplash.com/photo-1454496522488-7a8e488e8606?w=2000'
        },
        {
            'name': "pavankoneti161gmailcom's Sanctuary",
            'logo': 'https://images.unsplash.com/photo-1542601906990-b4d3fb778b09?w=400',
            'banner': 'https://images.unsplash.com/photo-1502672260266-1c1ef2d93688?w=2000'
        }
    ]

    for data in assets:
        try:
            profile = SellerProfile.objects.get(store_name=data['name'])
            profile.logo_url = data['logo']
            profile.banner_url = data['banner']
            profile.save()
            print(f"Updated {data['name']} with guaranteed stable demo images.")
        except SellerProfile.DoesNotExist:
            print(f"Seller {data['name']} not found, skipping.")

if __name__ == "__main__":
    seed_seller_images()
