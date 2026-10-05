"""Retention cleanup and metric downsampling (1 min -> 1 hour -> 1 day)."""
import datetime

from django.db import transaction
from django.utils import timezone

from . import conf, context
from .metrics import Agg
from .storage import get_backend

# (from resolution, to resolution, age after which rows are rolled up)
ROLLUPS = ((60, 3600, datetime.timedelta(hours=48)), (3600, 86400, datetime.timedelta(days=30)))


def cleanup(kinds=None, batch_size=2000, now=None):
    """Apply per-kind retention. Returns {kind: rows deleted}."""
    now = now or timezone.now()
    backend, out = get_backend(), {}
    with context.suppressed():
        for kind, days in conf.get("RETENTION").items():
            if kinds and kind not in kinds:
                continue
            try:
                out[kind] = backend.delete_before(kind, now - datetime.timedelta(days=days), batch_size)
            except KeyError:
                continue  # unknown retention key: reported by the system check
    return out


def rollup(now=None, max_buckets=200):
    """Merge fine-grained metric samples into coarser buckets; returns rows removed.

    Bounded per run (``max_buckets`` target buckets) so it never holds a long transaction.
    """
    from .models import MetricSample

    now = now or timezone.now()
    removed = 0
    with context.suppressed():
        for src, dst, age in ROLLUPS:
            cutoff = now - age
            cutoff = cutoff.replace(minute=0, second=0, microsecond=0)
            if dst == 86400:
                cutoff = cutoff.replace(hour=0)
            for _ in range(max_buckets):
                oldest = (MetricSample.objects.filter(resolution=src, timestamp__lt=cutoff)
                          .order_by("timestamp").values_list("timestamp", flat=True).first())
                if oldest is None:
                    break
                start = oldest.replace(minute=0, second=0, microsecond=0)
                if dst == 86400:
                    start = start.replace(hour=0)
                end = start + datetime.timedelta(seconds=dst)
                if end > cutoff:
                    break
                rows = MetricSample.objects.filter(resolution=src, timestamp__gte=start, timestamp__lt=end)
                merged = {}
                for r in rows.iterator(chunk_size=2000):
                    key = (r.metric_id, r.labels_key)
                    agg = merged.get(key)
                    if agg is None:
                        agg = merged[key] = (Agg(), r.labels)
                    agg[0].merge(r.count, r.sum, r.min, r.max, r.last, r.buckets)
                with transaction.atomic(using=rows.db):
                    n, _ = rows.delete()
                    MetricSample.objects.bulk_create([
                        MetricSample(metric_id=mid, timestamp=start, resolution=dst, labels=labels, labels_key=lk,
                                     count=a.count, sum=a.sum, min=a.min, max=a.max, last=a.last, buckets=a.buckets)
                        for (mid, lk), (a, labels) in merged.items()
                    ], batch_size=200)
                removed += n - len(merged)
    return removed


def run():
    cleanup()
    rollup()
