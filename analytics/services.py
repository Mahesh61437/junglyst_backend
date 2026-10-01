"""
Shared analytics helpers for dashboards (seller + admin).

Everything here is computed synchronously from existing order data — no extra
storage, no time-series database. Keep it cheap: a handful of aggregate queries
per request.
"""
from datetime import datetime, timedelta, date

from django.db.models import Sum, F, Value, DecimalField, Count
from django.db.models.functions import Coalesce, TruncDay
from django.utils import timezone

# Order statuses that count as realised revenue. Pending/failed (unpaid),
# cancelled and returned are excluded so numbers reflect actual sales.
REVENUE_STATUSES = ('confirmed', 'processing', 'shipped', 'delivered')

# Revenue = price x quantity. The old code summed unit_price alone, which
# undercounted every multi-quantity line item.
REVENUE_EXPR = Coalesce(
    Sum(F('unit_price') * F('quantity'), output_field=DecimalField(max_digits=14, decimal_places=2)),
    Value(0, output_field=DecimalField(max_digits=14, decimal_places=2)),
)


def parse_date_range(request):
    """Resolve the reporting window from query params.

    Supports either an explicit ``?start=YYYY-MM-DD&end=YYYY-MM-DD`` or a named
    ``?period=`` of: ``7d``, ``30d`` (default), ``90d``, ``this_month``,
    ``last_month``, ``ytd``, ``all``.

    Returns a dict with the current window plus the immediately-preceding window
    of equal length (for period-over-period trend), and a human label.
    """
    now = timezone.now()
    tz = timezone.get_current_timezone()
    period = (request.query_params.get('period') or '').strip().lower()
    start_str = request.query_params.get('start')
    end_str = request.query_params.get('end')

    start = end = None
    label = ''

    def _aware(d, end_of_day=False):
        t = datetime.combine(d, datetime.max.time() if end_of_day else datetime.min.time())
        return timezone.make_aware(t, tz)

    if start_str and end_str:
        try:
            s = datetime.strptime(start_str, '%Y-%m-%d').date()
            e = datetime.strptime(end_str, '%Y-%m-%d').date()
            start, end = _aware(s), _aware(e, end_of_day=True)
            label = f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}"
        except ValueError:
            start = end = None

    if start is None:
        if period == 'all':
            start, end = None, now
            label = 'All time'
        elif period == 'this_month':
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
            label = now.strftime('%B %Y')
        elif period == 'last_month':
            first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = first_this - timedelta(microseconds=1)
            start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            label = start.strftime('%B %Y')
        elif period == 'ytd':
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
            label = f"YTD {now.year}"
        else:
            days = {'7d': 7, '90d': 90}.get(period, 30)
            start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now
            label = f"Last {days} days"

    # Previous comparison window of equal length (skip for 'all').
    prev_start = prev_end = None
    if start is not None:
        span = end - start
        prev_end = start - timedelta(microseconds=1)
        prev_start = prev_end - span

    return {
        'start': start, 'end': end,
        'prev_start': prev_start, 'prev_end': prev_end,
        'label': label,
    }


def filter_by_range(qs, rng, field='order__created_at'):
    """Apply a date window (from parse_date_range) to an OrderItem queryset."""
    if rng.get('start') is not None:
        qs = qs.filter(**{f'{field}__gte': rng['start']})
    if rng.get('end') is not None:
        qs = qs.filter(**{f'{field}__lte': rng['end']})
    return qs


def pct_change(current, previous):
    """Period-over-period percentage change, rounded to 1 dp.

    Returns ``None`` when there's no prior baseline (so the UI can show "—"
    instead of a misleading +100%/+0%).
    """
    current = float(current or 0)
    previous = float(previous or 0)
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / previous * 100, 1)


def revenue_core_metrics(order_items):
    """Realised revenue, order count and items sold for an OrderItem queryset.

    The caller is responsible for any seller/date/status filtering.
    """
    agg = order_items.aggregate(
        revenue=REVENUE_EXPR,
        items_sold=Coalesce(Sum('quantity'), Value(0)),
    )
    return {
        'revenue': float(agg['revenue'] or 0),
        'items_sold': int(agg['items_sold'] or 0),
        'orders': order_items.values('order').distinct().count(),
    }


def daily_revenue_series(order_items, rng):
    """Daily revenue/orders buckets across the window, gaps filled with zeros."""
    rows = (
        order_items
        .annotate(day=TruncDay('order__created_at'))
        .values('day')
        .annotate(
            revenue=REVENUE_EXPR,
            orders=Count('order', distinct=True),
        )
        .order_by('day')
    )
    by_day = {r['day'].date(): r for r in rows if r['day']}

    start = rng.get('start')
    end = rng.get('end') or timezone.now()
    if start is None:
        # 'all' — span from the earliest data point (or just show what exists).
        if not by_day:
            return []
        start_d = min(by_day.keys())
    else:
        start_d = start.date()
    end_d = end.date()

    # Cap the number of points so an 'all'/huge range can't blow up the payload.
    max_points = 180
    total_days = (end_d - start_d).days + 1
    if total_days > max_points:
        # Fall back to whatever buckets exist, newest max_points.
        series = [
            {'date': d.strftime('%b %d'), 'revenue': float(r['revenue'] or 0), 'orders': r['orders']}
            for d, r in sorted(by_day.items())
        ]
        return series[-max_points:]

    series = []
    for i in range(total_days):
        d = start_d + timedelta(days=i)
        r = by_day.get(d)
        series.append({
            'date': d.strftime('%b %d'),
            'revenue': float(r['revenue']) if r else 0.0,
            'orders': r['orders'] if r else 0,
        })
    return series


def seller_pnl_breakdown(order_items):
    """Gross → fees/taxes → net settlement for a (already filtered) OrderItem qs.

    Mirrors the reverse-engineering used by the GST dashboard: the stored
    ``unit_price`` is GST- and commission-inclusive, so we back out the base.
    """
    gross = taxable = gst = commission = 0.0
    items = order_items.select_related('variant')
    for it in items.iterator():
        line = float(it.unit_price) * it.quantity
        gst_rate = float(it.gst_percentage or 0)
        comm_rate = float(it.variant.commission_rate) if it.variant else 10.0
        factor = 1 + (gst_rate / 100) + (comm_rate / 100)
        base = line / factor if factor else line
        gross += line
        taxable += base
        gst += base * (gst_rate / 100)
        commission += base * (comm_rate / 100)

    platform_fee_gst = commission * 0.18
    tcs = taxable * 0.01
    tds = gross * 0.01
    net = gross - commission - platform_fee_gst - tcs - tds

    return {
        'gross_sales': round(gross, 2),
        'taxable_value': round(taxable, 2),
        'total_gst': round(gst, 2),
        'platform_fee': round(commission, 2),
        'platform_fee_gst': round(platform_fee_gst, 2),
        'tcs_deducted': round(tcs, 2),
        'tds_deducted': round(tds, 2),
        'net_settlement': round(net, 2),
    }
