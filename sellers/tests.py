from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from .models import SellerProfile

User = get_user_model()

class SellerDashboardTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='grower@junglyst.com',
            username='grower1',
            password='password123',
            role='grower'
        )
        self.client.force_authenticate(user=self.user)

    def test_get_dashboard_data(self):
        response = self.client.get('/api/sellers/dashboard/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('metrics', response.data)
        self.assertIn('profile', response.data)

    def test_update_profile(self):
        data = {
            'store_name': 'Green Sanctuary',
            'bio': 'A beautiful bio',
            'location_city': 'Bangalore'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        profile = SellerProfile.objects.get(user=self.user)
        self.assertEqual(profile.store_name, 'Green Sanctuary')
        self.assertEqual(profile.slug, 'green-sanctuary')

    def test_collector_upgrade_on_save(self):
        # Create a collector
        collector = User.objects.create_user(
            email='collector@junglyst.com',
            username='collector1',
            password='password123',
            role='collector'
        )
        self.client.force_authenticate(user=collector)
        
        data = {'store_name': 'New Studio'}
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check if role upgraded
        collector.refresh_from_db()
        self.assertEqual(collector.role, 'grower')

    def test_unauthenticated_access(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/sellers/dashboard/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
