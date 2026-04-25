from django.contrib import admin
from .models import User, Category, Tag, Product, ProductVariant, ProductImage

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'role', 'is_verified_seller', 'is_deleted')
    list_filter = ('role', 'is_deleted')

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'gst_percentage', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller', 'is_active', 'is_deleted')
    list_filter = ('seller', 'is_active', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'price', 'stock', 'is_deleted')

admin.site.register(Tag)
admin.site.register(ProductImage)
