from celery import shared_task


@shared_task(name='analytics.purge_stock_sync_sessions')
def purge_stock_sync_sessions():
    """
    Delete stock-sync previews past their retention window.

    compare_diff() also purges when a new preview is taken, but that only helps
    while syncs keep running — stop syncing and the last batch would sit in the
    table indefinitely. This runs on a schedule so cleanup does not depend on
    anyone using the feature.
    """
    from analytics.sync_utils import _purge_stale_sessions
    deleted, _ = _purge_stale_sessions()
    return {'deleted': deleted}
