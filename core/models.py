from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
import uuid

class UserRole(models.TextChoices):
    COLLECTOR = 'collector', _('Collector')
    GROWER = 'grower', _('Grower')
    ADMIN = 'admin', _('Admin')

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def delete(self, **kwargs):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save()

    class Meta:
        abstract = True

class User(AbstractUser, SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_('email address'), unique=True)
    phone = models.CharField(_('phone number'), max_length=15, unique=True, null=True, blank=True)
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.COLLECTOR)
    
    is_guest = models.BooleanField(default=False)
    is_verified_seller = models.BooleanField(default=False)
    
    avatar_url = models.URLField(max_length=1000, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True, default='Kerala, India')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

class Category(SoftDeleteModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=20.00)
    
    meta_title = models.CharField(max_length=200, blank=True, null=True)
    meta_description = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['name']

    def __str__(self):
        return self.name

class SubCategory(SoftDeleteModel):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='subcategories')
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Subcategories"
        unique_together = ('category', 'name')
        ordering = ['name']

    def __str__(self):
        return f"{self.category.name} > {self.name}"

class Tag(SoftDeleteModel):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Product(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    tagline = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField()
    
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='products')
    categories = models.ManyToManyField(Category, related_name='products', blank=True)
    sub_category = models.ForeignKey(SubCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    tags = models.ManyToManyField(Tag, related_name='products', blank=True)
    
    scientific_name = models.CharField(max_length=255, blank=True, null=True)
    care_level = models.CharField(max_length=50, choices=[('Easy', 'Easy'), ('Medium', 'Medium'), ('Advanced', 'Advanced')], default='Easy')
    light_requirements = models.CharField(max_length=50, choices=[('Low', 'Low'), ('Medium', 'Medium'), ('High', 'High')], default='Medium')
    growth_rate = models.CharField(max_length=50, choices=[('Slow', 'Slow'), ('Moderate', 'Moderate'), ('Fast', 'Fast')], default='Moderate')
    
    is_rare = models.BooleanField(default=False)
    origin = models.CharField(max_length=100, blank=True, null=True)
    
    # Aquatic specific
    water_temperature = models.CharField(max_length=50, blank=True, null=True)
    ph_range = models.CharField(max_length=50, blank=True, null=True)
    
    is_active = models.BooleanField(default=True)
    co2_requirement = models.CharField(max_length=50, choices=[('Low', 'Low'), ('Medium', 'Medium'), ('High', 'High')], default='Low')
    
    view_count = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=5.0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

class ProductVariant(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=100, default='Standard')
    
    sku = models.CharField(max_length=100, unique=True, null=True, blank=True)
    
    # Financial Breakdown
    base_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Seller's payout expectation")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="GST percentage (e.g. 18.00)")
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=10.00, help_text="Platform commission percentage")
    
    # Final Buyer Price (calculated)
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Final price shown to buyers")
    
    compare_at_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    weight = models.DecimalField(max_digits=10, decimal_places=3, help_text="Weight in Kilograms", default=0.5)
    length = models.DecimalField(max_digits=10, decimal_places=2, help_text="Length in cm", default=10.0)
    width = models.DecimalField(max_digits=10, decimal_places=2, help_text="Width in cm", default=10.0)
    height = models.DecimalField(max_digits=10, decimal_places=2, help_text="Height in cm", default=10.0)
    
    stock = models.IntegerField(default=0)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Auto-calculate final price if not manually overridden or during creation
        # Final Price = Base Price + GST (on base) + Commission (on base)
        from decimal import Decimal
        base = Decimal(str(self.base_price))
        gst = Decimal(str(self.gst_rate))
        comm = Decimal(str(self.commission_rate))
        
        gst_amount = base * (gst / Decimal('100'))
        commission_amount = base * (comm / Decimal('100'))
        self.price = base + gst_amount + commission_amount
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name} - {self.name}"

class ProductImage(SoftDeleteModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True, blank=True, related_name='images')
    image_url = models.URLField(max_length=1000)
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Image for {self.product.name}"
