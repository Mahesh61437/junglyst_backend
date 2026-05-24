from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from .models import SellerProfile, AllowedSeller

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
        AllowedSeller.objects.create(email='grower@junglyst.com', is_active=True)
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
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        profile = SellerProfile.objects.get(user=self.user)
        self.assertEqual(profile.store_name, 'Green Sanctuary')
        self.assertEqual(profile.slug, 'green-sanctuary')

    def test_update_profile_missing_mandatory_fields(self):
        # Missing pickup_address
        data = {
            'store_name': 'Green Sanctuary',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertEqual(response.data['error'], 'Pickup street address is required.')

    def test_update_profile_invalid_phone(self):
        data = {
            'store_name': 'Green Sanctuary',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '12345'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertEqual(response.data['error'], 'Phone number must be a valid 10-digit Indian mobile number.')

    def test_collector_upgrade_on_save(self):
        # Create a collector
        collector = User.objects.create_user(
            email='collector@junglyst.com',
            username='collector1',
            password='password123',
            role='collector'
        )
        AllowedSeller.objects.create(email='collector@junglyst.com', is_active=True)
        self.client.force_authenticate(user=collector)
        
        data = {
            'store_name': 'New Studio',
            'location_city': 'Bangalore',
            'location_state': 'Karnataka',
            'location_pincode': '560001',
            'pickup_address': '123 Green Lane',
            'phone': '9876543210'
        }
        response = self.client.post('/api/sellers/dashboard/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check if role upgraded
        collector.refresh_from_db()
        self.assertEqual(collector.role, 'grower')

    def test_unauthenticated_access(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/sellers/dashboard/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
