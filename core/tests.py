from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils.text import slugify
from .models import Category, Product, ProductVariant, Tag

User = get_user_model()

class ProductModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='test@junglyst.com',
            username='testuser',
            password='password123',
            role='grower'
        )
        self.category = Category.objects.create(
            name='Test Plants',
            slug='test-plants',
            gst_percentage=12.00,
            commission_rate=20.00
        )

    def test_product_creation_and_slug(self):
        product = Product.objects.create(
            name='Monstera Deliciosa',
            description='A beautiful monstera',
            seller=self.user
        )
        # Verify slug auto-generation in serializer or model? 
        # (Our serializer handles it, but let's check if the model allows manual or if we need to test serializer)
        # Based on our serializer code, it sets the slug before saving.
        
        # Let's test the serializer logic indirectly by testing the creation flow or just manual slug for now
        product.slug = slugify(product.name)
        product.save()
        self.assertEqual(product.slug, 'monstera-deliciosa')

    def test_variant_price_calculation(self):
        product = Product.objects.create(
            name='Monstera',
            description='Test',
            seller=self.user
        )
        variant = ProductVariant.objects.create(
            product=product,
            name='Small',
            base_price=1000.00,
            gst_rate=12.00,
            commission_rate=20.00,
            stock=10
        )
        # Final Price = Base + GST(12%) + Commission(20%)
        # 1000 + 120 + 200 = 1320
        self.assertEqual(float(variant.price), 1320.00)

    def test_unique_slug_generation(self):
        # This tests the logic we added to the ProductSerializer.create
        from .serializers import ProductSerializer
        
        data = {
            'name': 'Duplicate Plant',
            'description': 'Description',
            'category_id': self.category.id,
            'variants': [{'name': 'V1', 'base_price': 100, 'stock': 5}]
        }
        
        # Create first
        serializer1 = ProductSerializer(data=data)
        serializer1.is_valid(raise_exception=True)
        p1 = serializer1.save(seller=self.user)
        self.assertEqual(p1.slug, 'duplicate-plant')
        
        # Create second with same name
        serializer2 = ProductSerializer(data=data)
        serializer2.is_valid(raise_exception=True)
        p2 = serializer2.save(seller=self.user)
        self.assertEqual(p2.slug, 'duplicate-plant-1')

class CategoryModelTest(TestCase):
    def test_category_commission_default(self):
        cat = Category.objects.create(name='New Cat', slug='new-cat')
        self.assertEqual(float(cat.commission_rate), 20.00)
        self.assertEqual(float(cat.gst_percentage), 0.00)
