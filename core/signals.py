from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from .models import Product, Category, SubCategory, ProductVariant, ProductImage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invalidate_product_detail_cache(product_id, slug=None):
    """Delete the cached detail response for one product."""
    cache.delete(f'product:detail:{product_id}')
    if slug:
        cache.delete(f'product:slug_to_id:{slug}')


# ---------------------------------------------------------------------------
# Broad API cache clear (keeps response cache fresh)
# ---------------------------------------------------------------------------

@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
@receiver([post_save, post_delete], sender=SubCategory)
def clear_api_cache(sender, **kwargs):
    """Clear cached API responses whenever catalogue data changes."""
    cache.clear()


# ---------------------------------------------------------------------------
# Product detail cache — targeted invalidation per product
# ---------------------------------------------------------------------------

@receiver([post_save, post_delete], sender=Product)
def product_detail_invalidate(sender, instance, **kwargs):
    """Product edited/archived → bust its detail cache entry."""
    _invalidate_product_detail_cache(str(instance.id), slug=instance.slug)


@receiver(post_save, sender=ProductVariant)
def variant_detail_invalidate(sender, instance, **kwargs):
    """Price or stock changed → the product detail page must reflect it."""
    _invalidate_product_detail_cache(str(instance.product_id))


@receiver(post_save, sender=ProductImage)
def image_detail_invalidate(sender, instance, **kwargs):
    """Image added/changed → bust the product's cached detail."""
    _invalidate_product_detail_cache(str(instance.product_id))


# ---------------------------------------------------------------------------
# Feed cache — rebuild asynchronously on stock / product changes
# ---------------------------------------------------------------------------

@receiver([post_save, post_delete], sender=Product)
def product_feed_invalidate(sender, instance, **kwargs):
    """
    Product published, archived, or deleted → feed order may change.
    cache.clear() above already removes the feed key, but we also
    pre-warm the cache via Celery so the next user doesn't wait.
    """
    from .tasks import rebuild_feed_cache
    rebuild_feed_cache.apply_async(countdown=2)


@receiver(post_save, sender=ProductVariant)
def variant_stock_invalidate(sender, instance, **kwargs):
    """
    Stock changed on a variant → a product may have moved between the
    in-stock and out-of-stock blocks.  Rebuild the feed asynchronously.
    countdown=5 debounces bursts of updates (e.g. bulk order fulfilment).
    """
    from .tasks import rebuild_feed_cache
    rebuild_feed_cache.apply_async(countdown=5)
