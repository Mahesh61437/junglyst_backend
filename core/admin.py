from django.contrib import admin
from .models import User, Category, SubCategory, Tag, Product, ProductVariant, ProductImage

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'role', 'is_verified_seller', 'is_deleted')
    list_filter = ('role', 'is_deleted')

class SubCategoryInline(admin.TabularInline):
    model = SubCategory
    extra = 1
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'gst_percentage', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [SubCategoryInline]

@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'is_deleted')
    list_filter = ('category', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller', 'sub_category', 'is_active', 'is_deleted')
    list_filter = ('seller', 'sub_category', 'is_active', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'price', 'stock', 'is_deleted')

admin.site.register(Tag)
admin.site.register(ProductImage)
