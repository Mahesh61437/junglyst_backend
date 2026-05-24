from django.contrib import admin
from .models import User, Category, SubCategory, CategoryShippingRate, Tag, Product, ProductVariant, ProductImage, Configuration

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'role', 'is_verified_seller', 'is_deleted')
    list_filter = ('role', 'is_deleted')


class ShippingRateInline(admin.TabularInline):
    model = CategoryShippingRate
    extra = 1
    fields = ('min_weight_grams', 'max_weight_grams', 'rate', 'free_above_order_value')
    fk_name = 'category'


class SubCategoryShippingRateInline(admin.TabularInline):
    model = CategoryShippingRate
    extra = 1
    fields = ('min_weight_grams', 'max_weight_grams', 'rate', 'free_above_order_value')
    fk_name = 'sub_category'


class SubCategoryInline(admin.TabularInline):
    model = SubCategory
    extra = 1
    prepopulated_fields = {'slug': ('name',)}
    fields = ('name', 'slug', 'gst_percentage', 'commission_rate', 'description')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'shipping_type', 'gst_percentage', 'commission_rate', 'is_deleted')
    list_filter = ('shipping_type', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [SubCategoryInline, ShippingRateInline]


@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'gst_percentage', 'commission_rate', 'is_deleted')
    list_filter = ('category', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [SubCategoryShippingRateInline]


@admin.register(CategoryShippingRate)
class CategoryShippingRateAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'category', 'sub_category', 'min_weight_grams', 'max_weight_grams', 'rate', 'free_above_order_value')
    list_filter = ('category', 'sub_category')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller', 'get_sub_categories', 'care_level', 'is_rare', 'is_active', 'is_deleted')
    list_filter = ('seller', 'care_level', 'is_rare', 'is_active', 'is_deleted')
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ('name', 'scientific_name')

    def get_sub_categories(self, obj):
        return ", ".join(sub.name for sub in obj.sub_categories.all())
    get_sub_categories.short_description = 'Subcategories'


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product', 'name', 'price', 'stock', 'item_category', 'is_deleted')
    list_filter = ('item_category',)


admin.site.register(Tag)
admin.site.register(ProductImage)


@admin.register(Configuration)
class ConfigurationAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at', 'updated_at')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Configuration', {
            'fields': ('name', 'data')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
