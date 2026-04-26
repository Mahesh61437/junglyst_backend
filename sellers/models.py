from django.db import models
from core.models import User
from django.utils.text import slugify

class SellerProfileManager(models.Manager):
    def get_or_get_default(self, user):
        profile, created = self.get_or_create(
            user=user,
            defaults={
                'store_name': f"{user.username}'s Sanctuary",
                'slug': slugify(user.username),
                'brand_color': '#0A3029'
            }
        )
        return profile, created

class SellerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='seller_profile')
    store_name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(unique=True)
    logo_url = models.URLField(max_length=1000, blank=True, null=True)
    banner_url = models.URLField(max_length=1000, blank=True, null=True)
    brand_color = models.CharField(max_length=7, default='#0A3029')
    bio = models.TextField(blank=True, null=True)
    tagline = models.CharField(max_length=255, blank=True, null=True)
    
    gst_number = models.CharField(max_length=15, blank=True, null=True)
    gst_document_url = models.URLField(max_length=1000, blank=True, null=True)
    
    location_city = models.CharField(max_length=100, blank=True, null=True)
    location_pincode = models.CharField(max_length=10, blank=True, null=True)
    
    # Authenticity & Skill Showcase
    expertise_tags = models.JSONField(default=list, blank=True)
    infrastructure_details = models.TextField(blank=True, null=True)
    experience_years = models.PositiveIntegerField(default=0)
    identity_verified = models.BooleanField(default=False)
    
    # Promotion & Carousel Control
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    
    total_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=5.0)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SellerProfileManager()

    def __str__(self):
        return self.store_name

class AllowedSeller(models.Model):
    email = models.EmailField(unique=True, blank=True, null=True)
    phone = models.CharField(max_length=15, unique=True, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email or self.phone
