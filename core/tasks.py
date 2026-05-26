from celery import shared_task


@shared_task(name='core.rebuild_feed_cache')
def rebuild_feed_cache():
    """
    Rebuild the product feed cache and pre-warm:
      - all category / sub_category filter caches
      - all sorted+fair feeds (rating asc/desc, created_at asc/desc)

    All subsequent requests for these common query patterns are pure Redis hits.
    """
    from .feed import (
        invalidate_feed_cache, get_ordered_product_ids,
        prewarm_filter_caches, prewarm_sorted_feeds,
    )
    invalidate_feed_cache()
    ids      = get_ordered_product_ids()
    filters  = prewarm_filter_caches(ids)
    sorts    = prewarm_sorted_feeds(ids)
    return (
        f'Feed rebuilt: {len(ids)} products | '
        f'filters: {filters["categories"]} categories, {filters["subcategories"]} sub_categories | '
        f'sorts pre-warmed: {list(sorts.keys())}'
    )
