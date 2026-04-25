from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils.text import slugify
from rest_framework.test import APITestCase
from rest_framework import status
from .models import Category, Product, ProductVariant, Tag
from sellers.models import SellerProfile
from .serializers import ProductSerializer

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
        self.assertEqual(float(variant.price), 1320.00)

    def test_unique_slug_generation(self):
        data = {
            'name': 'Duplicate Plant',
            'description': 'Description',
            'category_id': self.category.id,
            'variants': [{'name': 'V1', 'base_price': 100, 'stock': 5}]
        }
        
        serializer1 = ProductSerializer(data=data)
        serializer1.is_valid(raise_exception=True)
        p1 = serializer1.save(seller=self.user)
        self.assertEqual(p1.slug, 'duplicate-plant')
        
        serializer2 = ProductSerializer(data=data)
        serializer2.is_valid(raise_exception=True)
        p2 = serializer2.save(seller=self.user)
        self.assertEqual(p2.slug, 'duplicate-plant-1')

class CategoryModelTest(TestCase):
    def test_category_commission_default(self):
        cat = Category.objects.create(name='New Cat', slug='new-cat')
        self.assertEqual(float(cat.commission_rate), 20.00)
        self.assertEqual(float(cat.gst_percentage), 0.00)

class AuthTests(APITestCase):
    def setUp(self):
        User.objects.create_user(email='base@test.com', username='baseuser', password='password123')

    def test_user_registration(self):
        url = '/api/core/register/'
        data = {
            'email': 'newgrower@junglyst.com',
            'username': 'newgrower',
            'password': 'GrowerPassword123!',
            'role': 'grower'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.count(), 2) 
        self.assertEqual(User.objects.get(email='newgrower@junglyst.com').role, 'grower')

    def test_user_login(self):
        url = '/api/core/login/'
        data = {'email': 'base@test.com', 'password': 'password123'}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

class SellerDashboardTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='seller@test.com', 
            username='selleruser', 
            password='password123', 
            role='grower'
        )
        self.client.force_authenticate(user=self.user)

    def test_get_dashboard(self):
        url = '/api/sellers/dashboard/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('metrics', response.data)
        self.assertIn('profile', response.data)

    def test_update_profile(self):
        url = '/api/sellers/dashboard/'
        data = {
            'store_name': 'The Orchid Sanctuary',
            'expertise': 'Orchid Specialist',
            'brand_color': '#FF5733'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        profile = SellerProfile.objects.get(user=self.user)
        self.assertEqual(profile.store_name, 'The Orchid Sanctuary')
        self.assertEqual(profile.brand_color, '#FF5733')
