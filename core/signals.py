from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

from sellers.models import SellerProfile, SellerExcludedState, REGION_SHIPS_FIELDS
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


# ---------------------------------------------------------------------------
# Seller delivery regions — region-filtered shop/home responses are cached
# ---------------------------------------------------------------------------

def _region_flags(values) -> tuple:
    return tuple(values[field] for field in REGION_SHIPS_FIELDS.values())


@receiver(pre_save, sender=SellerProfile)
def remember_seller_regions(sender, instance, **kwargs):
    """Snapshot the stored region flags so post_save can tell if they changed."""
    previous = None
    if instance.pk:
        previous = (
            SellerProfile.objects.filter(pk=instance.pk)
            .values(*REGION_SHIPS_FIELDS.values())
            .first()
        )
    instance._previous_region_flags = _region_flags(previous) if previous else None


@receiver(post_save, sender=SellerProfile)
def seller_regions_invalidate(sender, instance, created, **kwargs):
    """
    A seller started or stopped shipping to a region → their products enter or
    leave that region's shop and home feeds. A new profile ships everywhere by
    default, which changes nothing.
    """
    previous = getattr(instance, '_previous_region_flags', None)
    if created or previous is None:
        return
    current = tuple(getattr(instance, field) for field in REGION_SHIPS_FIELDS.values())
    if current != previous:
        cache.clear()


@receiver([post_save, post_delete], sender=SellerExcludedState)
def seller_excluded_state_invalidate(sender, **kwargs):
    """A seller excluded / re-included a state (e.g. via Django admin) → state-filtered feeds change."""
    cache.clear()
