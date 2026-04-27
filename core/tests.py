from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model

User = get_user_model()

class AuthTests(APITestCase):
    def setUp(self):
        self.register_url = reverse('auth_register')
        self.login_url = reverse('token_obtain_pair')
        self.user_data = {
            "email": "testuser@example.com",
            "username": "testuser",
            "password": "Password123",
            "role": "collector"
        }
        self.user = User.objects.create_user(**self.user_data)

    def test_registration(self):
        url = self.register_url
        data = {
            "email": "newuser@example.com",
            "username": "newuser",
            "password": "Password123",
            "role": "collector"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.filter(email="newuser@example.com").count(), 1)

    def test_login_with_email(self):
        url = self.login_url
        data = {
            "email": "testuser@example.com",
            "password": "Password123"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('user', response.data)

    def test_login_with_username(self):
        url = self.login_url
        data = {
            "email": "testuser", # Using the email field name but providing username value
            "password": "Password123"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

    def test_login_failed(self):
        url = self.login_url
        data = {
            "email": "testuser@example.com",
            "password": "WrongPassword"
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
