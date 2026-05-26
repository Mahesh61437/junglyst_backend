from celery import shared_task


@shared_task(name='core.rebuild_feed_cache')
def rebuild_feed_cache():
    """
    Rebuild the product feed cache and pre-warm all category / sub_category
    filter caches so the first browse request for any filter is a cache hit.
    """
    from .feed import invalidate_feed_cache, get_ordered_product_ids, prewarm_filter_caches
    invalidate_feed_cache()
    ids     = get_ordered_product_ids()
    summary = prewarm_filter_caches(ids)
    return (
        f'Feed rebuilt: {len(ids)} products | '
        f'pre-warmed {summary["categories"]} categories, '
        f'{summary["subcategories"]} sub_categories'
    )
