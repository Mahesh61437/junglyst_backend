from celery import shared_task


@shared_task(bind=True, name='core.scrape_and_sync')
def scrape_and_sync(self, source: str, seller_email: str, source_markup, seller_commission, buyer_commission):
    """
    Celery task: scrape live prices/stock for `source` (petkadai | himadri),
    apply stock changes immediately, and return price diffs for admin approval.

    Result stored in django-db backend — poll via AsyncResult(task_id).
    """
    from decimal import Decimal
    from analytics.sync_utils import (
        scrape_live_petkadai, scrape_live_himadri, compute_diff,
    )
    from analytics.views import _set_last_synced, _get_last_synced

    markup_dec = Decimal(str(source_markup))     if source_markup     is not None else None
    seller_dec = Decimal(str(seller_commission)) if seller_commission is not None else None
    buyer_dec  = Decimal(str(buyer_commission))  if buyer_commission  is not None else None

    def _progress(msg: str):
        self.update_state(state='PROGRESS', meta={'progress': msg})

    _progress('Connecting to live site…')

    if source == 'petkadai':
        records = scrape_live_petkadai(progress_cb=_progress)
    else:
        records = scrape_live_himadri(progress_cb=_progress)

    _progress(f'Scraped {len(records)} products. Computing diffs…')

    result = compute_diff(records, source, seller_email, markup_dec, seller_dec, buyer_dec, progress_cb=_progress)

    _set_last_synced(source)
    result['last_synced'] = _get_last_synced(source)

    return result


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
