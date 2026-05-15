
import os
import django
import sys
from django.utils.text import slugify

# Setup Django environment
sys.path.append('/Users/mahesh/Desktop/junglyst/junglyst_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from sellers.models import SellerProfile

User = get_user_model()

def create_15_sellers():
    sellers_data = [
        {"name": "Emerald Exotics", "tagline": "Rare foliage from the deep mist.", "banner": "photo-1542601906990-b4d3fb778b09", "logo": "photo-1614594871741-583a24032e6e"},
        {"name": "Aqua Flora Studio", "tagline": "Mastering the art of underwater life.", "banner": "photo-1518531933037-91b2f5f229cc", "logo": "photo-1534438327276-14e5300c3a48"},
        {"name": "Moss & Stone", "tagline": "Ancient textures for modern sanctuaries.", "banner": "photo-1502672260266-1c1ef2d93688", "logo": "photo-1550948390-6ee7ef7a5d3a"},
        {"name": "The Fernery", "tagline": "Chronicles of prehistoric greenery.", "banner": "photo-1512428559087-560fa5ceab42", "logo": "photo-1582213708544-4bae90a501bc"},
        {"name": "Botanical Alchemy", "tagline": "Where science meets botanical beauty.", "banner": "photo-1441986300917-64674bd600d8", "logo": "photo-1525498128493-380d1990a112"},
        {"name": "Verdant Vault", "tagline": "Exclusive access to private collections.", "banner": "photo-1523381210434-271e8be1f52b", "logo": "photo-1533038590840-1cde6e668a91"},
        {"name": "Crystal Creek Plants", "tagline": "Purity in every leaf.", "banner": "photo-1555529669-e69e7aa0ba9a", "logo": "photo-1508197149814-0cc02930d44b"},
        {"name": "Orchid Oasis", "tagline": "Exotic elegance, meticulously curated.", "banner": "photo-1545239351-ef35f43d514b", "logo": "photo-1502082553048-f009c37129b9"},
        {"name": "Succulent Sphere", "tagline": "Architectural nature for your space.", "banner": "photo-1459411552884-841db9b3cc2a", "logo": "photo-1535242208474-9a28972a0d03"},
        {"name": "Zen Gardeners", "tagline": "Find peace in the green.", "banner": "photo-1520923642038-b4259acecbd7", "logo": "photo-1530667912788-f976e8ee0bd5"},
        {"name": "Leafy Legends", "tagline": "Historic specimens with a story.", "banner": "photo-1454496522488-7a8e488e8606", "logo": "photo-1542601906990-b4d3fb778b09"},
        {"name": "The Grower's Guild", "tagline": "A collective of master curators.", "banner": "photo-1516528387618-afa90b13e000", "logo": "photo-1518531933037-91b2f5f229cc"},
        {"name": "BioSphere Botanics", "tagline": "Complete ecosystems in a jar.", "banner": "photo-1466721594955-2d185c3c59bc", "logo": "photo-1502672260266-1c1ef2d93688"},
        {"name": "Mist & Mountain", "tagline": "High-altitude rarities.", "banner": "photo-1501004318641-729e8439a7df", "logo": "photo-1534438327276-14e5300c3a48"},
        {"name": "Jungle Pulse", "tagline": "The heartbeat of the rainforest.", "banner": "photo-1497215728101-856f4ea42174", "logo": "photo-1525498128493-380d1990a112"}
    ]

    for i, data in enumerate(sellers_data):
        username = slugify(data['name'])
        email = f"{username}@junglyst.com"
        
        # Create User
        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        if created:
            user.set_password('junglyst123')
            user.save()
            print(f"Created user: {username}")
        
        # Create Seller Profile
        profile, p_created = SellerProfile.objects.get_or_create(
            user=user,
            defaults={
                'store_name': data['name'],
                'slug': username,
                'tagline': data['tagline'],
                'banner_url': f"https://images.unsplash.com/{data['banner']}?w=2000",
                'logo_url': f"https://images.unsplash.com/{data['logo']}?w=400",
                'is_featured': i < 5,  # First 5 are featured
                'is_active': True,
                'brand_color': '#1b2d2a',
                'rating': '4.9',
                'experience_years': 5 + i
            }
        )
        if p_created:
            print(f"Created seller profile: {data['name']}")
        else:
            # Update existing for testing
            profile.banner_url = f"https://images.unsplash.com/{data['banner']}?w=2000"
            profile.logo_url = f"https://images.unsplash.com/{data['logo']}?w=400"
            profile.is_featured = i < 5
            profile.save()
            print(f"Updated seller profile: {data['name']}")

if __name__ == "__main__":
    create_15_sellers()
