import os
import django
import sys

# Setup Django environment
sys.path.append('/Users/mahesh/Desktop/junglyst/junglyst_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()
client = APIClient()

def test_flow():
    email = "test_login_flow@junglyst.com"
    password = "FlowPassword123!"
    
    # Clean up if exists
    User.objects.filter(email=email).delete()
    
    print(f"Testing registration for {email}...")
    reg_res = client.post('/api/core/register/', {
        'email': email,
        'username': 'testflow',
        'password': password,
        'role': 'grower'
    }, format='json')
    
    if reg_res.status_code == 201:
        print("Registration SUCCESS")
    else:
        print(f"Registration FAILED: {reg_res.status_code} - {reg_res.data}")
        return

    print("Testing login...")
    log_res = client.post('/api/core/login/', {
        'email': email,
        'password': password
    }, format='json')
    
    if log_res.status_code == 200:
        print("Login SUCCESS")
        print(f"Access Token: {log_res.data.get('access')[:20]}...")
    else:
        print(f"Login FAILED: {log_res.status_code} - {log_res.data}")

if __name__ == "__main__":
    test_flow()
