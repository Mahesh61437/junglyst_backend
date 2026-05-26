import time
import django
django.setup()

from django.db import connection
from core.models import Product, ProductVariant
from django.db.models import Exists, OuterRef

# Warm up database
Product.objects.filter(is_active=True, is_draft=False).first()

# Measure Stage 1 without Exists annotation
start_stage1 = time.perf_counter()
pool_values = list(
    Product.objects.filter(is_active=True, is_draft=False)
    .order_by('seller_id', '-created_at')
    .values('id', 'seller_id')[:500]
)
duration_stage1 = (time.perf_counter() - start_stage1) * 1000

# Measure Python Processing
start_py = time.perf_counter()
n = len(pool_values)
import random
for i in range(n - 1, 0, -1):
    j = random.randint(0, i)
    pool_values[i], pool_values[j] = pool_values[j], pool_values[i]
    
max_per_seller = 2
selected_items = []
seller_counts = {}
for item in pool_values:
    s_id = item['seller_id']
    count = seller_counts.get(s_id, 0)
    if count < max_per_seller:
        selected_items.append(item)
        seller_counts[s_id] = count + 1
        
selected_ids = [item['id'] for item in selected_items]
duration_py = (time.perf_counter() - start_py) * 1000

# Measure Stage 2 (where we annotate with has_stock)
from core.views import _product_list_queryset
start_stage2 = time.perf_counter()
full_products = {
    p.id: p for p in _product_list_queryset().filter(id__in=selected_ids).annotate(
        has_stock=Exists(ProductVariant.objects.filter(product=OuterRef('pk'), stock__gt=0))
    )
}
final_feed = []
for item in selected_items:
    p_obj = full_products.get(item['id'])
    if p_obj:
        final_feed.append(p_obj)

# Sort the final feed by has_stock in-memory (stable sort)
final_feed.sort(key=lambda p: getattr(p, 'has_stock', False), reverse=True)
duration_stage2 = (time.perf_counter() - start_stage2) * 1000

print(f"Stage 1 (Fetch values without Exists): {duration_stage1:.2f}ms")
print(f"Python Processing: {duration_py:.2f}ms")
print(f"Stage 2 (Fetch full details + Exists for {len(final_feed)} products): {duration_stage2:.2f}ms")
print(f"Total: {duration_stage1 + duration_py + duration_stage2:.2f}ms")
