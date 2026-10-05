"""Metrics: in-process aggregation into one-minute buckets, flushed periodically.

Recording is a dict update under a lock (no I/O), so built-in request/SQL/HTTP metrics
are counted for *every* operation even when the detailed records are sampled away.
Dashboards and alerting read these aggregates, so sampling never skews them.

    from django_observatory import metrics
    metrics.increment("invoice.processed")
    metrics.gauge("queue.depth", 42)
    with metrics.timer("invoice.processing"): ...
"""
import contextlib
import datetime
import functools
import threading
import time

from . import conf, context

# Histogram upper bounds (ms-oriented but unit-agnostic); the last bucket is +inf.
BOUNDS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
MAX_SERIES = 5000  # in-memory label-set cap per flush interval: bounds memory on cardinality bugs

_lock = threading.Lock()
_data = {}
dropped_series = 0


class Agg:
    __slots__ = ("count", "sum", "min", "max", "last", "buckets")

    def __init__(self):
        self.count = 0
        self.sum = 0.0
        self.min = None
        self.max = None
        self.last = None
        self.buckets = None

    def merge(self, count, total, mn, mx, last, buckets):
        self.count += count or 0
        self.sum += total or 0.0
        if mn is not None and (self.min is None or mn < self.min):
            self.min = mn
        if mx is not None and (self.max is None or mx > self.max):
            self.max = mx
        if last is not None:
            self.last = last
        if buckets:
            if self.buckets is None:
                self.buckets = [0] * (len(BOUNDS) + 1)
            for i, n in enumerate(buckets[: len(self.buckets)]):
                self.buckets[i] += n

    @property
    def avg(self):
        return self.sum / self.count if self.count else 0.0

    def quantile(self, q):
        """Estimate from histogram buckets (linear interpolation inside the bucket)."""
        if not self.buckets or not self.count:
            return 0.0
        target, seen = q * sum(self.buckets), 0
        for i, n in enumerate(self.buckets):
            if n and seen + n >= target:
                lo = BOUNDS[i - 1] if i else 0
                hi = BOUNDS[i] if i < len(BOUNDS) else (self.max or BOUNDS[-1])
                est = lo + (hi - lo) * ((target - seen) / n)
                # never report beyond what was actually observed
                return max(min(est, self.max if self.max is not None else est), self.min or 0)
            seen += n
        return self.max or 0.0


def _bucket_index(value):
    for i, b in enumerate(BOUNDS):
        if value <= b:
            return i
    return len(BOUNDS)


def _record(name, kind, value, labels, unit="", internal=False):
    global dropped_series
    try:
        if (context.is_suppressed() and not internal) or not conf.enabled("METRICS"):
            return
        key = (name, kind, unit, tuple(sorted(labels.items())) if labels else (), int(time.time()) // 60 * 60)
        with _lock:
            a = _data.get(key)
            if a is None:
                if len(_data) >= MAX_SERIES:
                    dropped_series += 1
                    return
                a = _data[key] = Agg()
            a.count += 1
            a.last = value
            if kind == "gauge":
                a.sum = value
                a.count = 1
            else:
                a.sum += value
            if kind != "counter":
                a.min = value if a.min is None or value < a.min else a.min
                a.max = value if a.max is None or value > a.max else a.max
            if kind == "histogram":
                if a.buckets is None:
                    a.buckets = [0] * (len(BOUNDS) + 1)
                a.buckets[_bucket_index(value)] += 1
    except Exception:  # noqa: BLE001 - metrics must never break the caller
        pass


# -- public API ---------------------------------------------------------------
def increment(name, value=1, labels=None):
    _record(name, "counter", value, labels)


def gauge(name, value, labels=None):
    _record(name, "gauge", value, labels)


def internal_gauge(name, value, labels=None):
    """Gauge recorded by observability itself (worker thread), where suppression is on."""
    _record(name, "gauge", value, labels, internal=True)


def histogram(name, value, labels=None, unit=""):
    _record(name, "histogram", value, labels, unit)


class timer(contextlib.ContextDecorator):  # noqa: N801 - used as ``with metrics.timer(...)``
    """Context manager / decorator recording elapsed milliseconds as a histogram."""

    def __init__(self, name, labels=None):
        self.name, self.labels = name, labels

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        histogram(self.name, (time.perf_counter() - self._t0) * 1000, self.labels, "ms")
        return False


def _process_gauges():
    import os

    labels = {"pid": str(os.getpid())}
    internal_gauge("process.threads", threading.active_count(), labels)
    try:
        import psutil  # optional

        p = psutil.Process()
        internal_gauge("process.memory_mb", round(p.memory_info().rss / 1048576, 1), labels)
        internal_gauge("process.cpu_percent", p.cpu_percent(interval=None), labels)
    except Exception:  # noqa: BLE001 - psutil absent or restricted: simply skip
        pass


def flush():
    """Hand accumulated buckets to the pipeline. Safe to call any time."""
    global _data
    from . import pipeline

    if conf.get("METRICS", "PROCESS"):
        _process_gauges()
    with _lock:
        data, _data = _data, {}
    for (name, kind, unit, labels, ts), a in data.items():
        labels = dict(labels)
        pipeline.enqueue("metric", {
            "name": name, "kind": kind, "unit": unit, "timestamp": context.to_dt(ts), "resolution": 60,
            "labels": labels, "labels_key": ",".join(f"{k}={v}" for k, v in sorted(labels.items())),
            "count": a.count, "sum": a.sum, "min": a.min, "max": a.max, "last": a.last, "buckets": a.buckets,
        })


def reset(**_):
    global _data
    with _lock:
        _data = {}


# -- reading ------------------------------------------------------------------
def _scan(names, start, end, keyfn, where=None):
    """Aggregate stored samples into {key: Agg}. ``where`` filters on labels (dict).

    ponytail: aggregates in Python over the window's rows; fine to ~10^5 rows because
    old data is rolled up to hourly/daily. Push down to SQL GROUP BY if that ceiling is hit.
    """
    from .models import MetricSample

    if isinstance(names, str):
        names = [names]
    qs = MetricSample.objects.filter(metric__name__in=names, timestamp__gte=start, timestamp__lt=end)
    out = {}
    rows = qs.values_list("metric__name", "timestamp", "labels", "count", "sum", "min", "max", "last", "buckets")
    for name, ts, labels, count, total, mn, mx, last, buckets in rows.iterator(chunk_size=2000):
        labels = labels or {}
        if where and any(labels.get(k) != v for k, v in where.items()):
            continue
        key = keyfn(name, ts, labels)
        if key is None:
            continue
        a = out.get(key)
        if a is None:
            a = out[key] = Agg()
        a.merge(count, total, mn, mx, last, buckets)
    return out


def total(name, start, end, **where):
    return _scan(name, start, end, lambda n, t, lb: 0, where).get(0) or Agg()


def by_label(name, label, start, end, **where):
    return _scan(name, start, end, lambda n, t, lb: lb.get(label, ""), where)


def timeseries(name, start, end, step=60, **where):
    """[(bucket_start_datetime, Agg)] with empty buckets filled, oldest first."""
    step = max(int(step), 60)

    def key(n, ts, lb):
        return int(ts.timestamp()) // step * step

    got = _scan(name, start, end, key, where)
    first = int(start.timestamp()) // step * step
    last = int((end - datetime.timedelta(microseconds=1)).timestamp()) // step * step
    return [(context.to_dt(t), got.get(t) or Agg()) for t in range(first, last + 1, step)]


def step_for(start, end, points=60):
    """A bucket size giving roughly ``points`` buckets, snapped to sensible steps."""
    span = (end - start).total_seconds()
    for s in (60, 300, 900, 3600, 10800, 21600, 86400):
        if span / s <= points:
            return s
    return 86400


def timed(name, labels=None):
    """Decorator form of timer() that also works on coroutine functions."""

    def deco(fn):
        import inspect

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a, **kw):
                with timer(name, labels):
                    return await fn(*a, **kw)
            return awrapper
        return timer(name, labels)(fn)

    return deco
