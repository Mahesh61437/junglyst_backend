import os
import django
import sys

# Setup Django environment
sys.path.append('/Users/mahesh/Desktop/junglyst/junglyst_backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'junglyst_backend.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

def create_admin():
    email = "admin@junglyst.com"
    username = "admin"
    password = "JunglystAdmin123!"
    
    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(
            email=email,
            username=username,
            password=password
        )
        print(f"Superuser created successfully!")
        print(f"Username: {username}")
        print(f"Email: {email}")
        print(f"Password: {password}")
    else:
        print(f"User '{username}' already exists. Updating password...")
        u = User.objects.get(username=username)
        u.set_password(password)
        u.is_superuser = True
        u.is_staff = True
        u.save()
        print(f"Superuser updated successfully!")

if __name__ == "__main__":
    create_admin()
