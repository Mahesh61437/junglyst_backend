from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from .models import Product, Category, SubCategory

@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
@receiver([post_save, post_delete], sender=SubCategory)
def clear_api_cache(sender, **kwargs):
    """
    Clear the cache whenever a product or category is modified.
    This ensures that the cached API responses are always fresh.
    """
    # For now, we clear the whole cache to be safe.
    # In a larger system, we might want to clear specific keys.
    cache.clear()
    print(f"Cache cleared due to change in {sender.__name__}")
