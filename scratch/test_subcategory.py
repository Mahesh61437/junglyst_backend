import os
import django
import sys

# Setup Django environment
sys.path.append('/Users/mahesh/Desktop/junglyst/junglyst_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from core.models import Category, SubCategory

def test_subcategory_creation():
    # Ensure a category exists
    cat, created = Category.objects.get_or_create(
        name="Test Category",
        defaults={'slug': 'test-category', 'gst_percentage': 18.0}
    )
    
    # Create a subcategory
    subcat, created = SubCategory.objects.get_or_create(
        category=cat,
        name="Test SubCategory",
        defaults={'slug': 'test-subcategory'}
    )
    
    print(f"Category: {cat.name} (GST: {cat.gst_percentage}%)")
    print(f"SubCategory: {subcat.name} (Parent: {subcat.category.name})")
    
    # Test nesting in serializer (manual check)
    from core.serializers import CategorySerializer
    serializer = CategorySerializer(cat)
    print("\nSerialized Category Data:")
    import json
    print(json.dumps(serializer.data, indent=2))
    
    # Cleanup if needed (optional)
    # subcat.delete()
    # cat.delete()

if __name__ == "__main__":
    test_subcategory_creation()
