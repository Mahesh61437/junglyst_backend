import uuid

from django.db import models
from core.models import User

class EventLog(models.Model):
    event_type = models.CharField(max_length=100)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    session_id = models.CharField(max_length=100, null=True, blank=True)
    data = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']


class StockSyncSession(models.Model):
    """
    One stock-sync preview, held until the admin has finished acting on it.

    This lived in the cache until importing a product was found to wipe it:
    Product.post_save invalidates catalogue caches, which on django_redis used
    to flush the whole keyspace, so the first import killed the session and
    every later import from the same preview failed as "session expired".
    Rows here survive cache flushes, Redis restarts and evictions, so
    everything the admin can see in the preview stays importable.
    """
    key           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source        = models.CharField(max_length=32)
    seller_email  = models.EmailField(blank=True, default='')
    # variant_id → pending price change, as returned to the admin for approval
    price_changes = models.JSONField(default=dict, blank=True)
    # sku → parsed scraped record, the source of truth for an import
    new_products  = models.JSONField(default=dict, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.source} sync {self.key} ({self.created_at:%Y-%m-%d %H:%M})'
