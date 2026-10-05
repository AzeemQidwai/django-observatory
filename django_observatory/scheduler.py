"""Periodic housekeeping without cron/Celery: the pipeline worker calls maybe_tick().

Per-process jobs (metric flush) run in every process. Cluster jobs (alerts, incident
detection, rollups, retention) run in at most one process per interval, elected by a
single atomic UPDATE on a lease row. No extra infrastructure, safe with many workers.
"""
import datetime
import os
import time

from django.utils import timezone

from . import conf, internal

_last_local = 0.0
_last_hourly = 0.0


def _claim(key, interval):
    """True if this process won the right to run ``key`` for this interval."""
    from .models import ObservabilityConfiguration as Cfg

    now = timezone.now()
    Cfg.objects.get_or_create(key=key, defaults={"updated_at": now - datetime.timedelta(days=1)})
    won = Cfg.objects.filter(key=key, updated_at__lte=now - datetime.timedelta(seconds=interval * 0.9)).update(
        updated_at=now, value={"pid": os.getpid()}
    )
    return won == 1


def _load_runtime_config():
    from .models import ObservabilityConfiguration as Cfg

    row = Cfg.objects.filter(key="runtime").first()
    overrides = {}
    for dotted, val in ((row.value if row else None) or {}).items():
        section, _, key = dotted.partition(".")
        overrides[(section, key)] = val
    conf.set_runtime(overrides)


def tick(force=False):
    """Run all due jobs now. Each job is isolated: one failing never blocks the others."""
    global _last_hourly
    from . import metrics, system

    jobs = [("system", system.record), ("metrics.flush", metrics.flush), ("runtime_config", _load_runtime_config)]
    interval = conf.get("SCHEDULER", "INTERVAL")
    if force or _claim("lease:tick", interval):
        from . import alerts
        from .correlation import engine

        if conf.enabled("ALERTS"):
            jobs.append(("alerts", alerts.evaluate))
        if conf.enabled("INCIDENTS"):
            jobs.append(("incidents", engine.detect))
    now_m = time.monotonic()
    if force or ((_last_hourly == 0.0 or now_m - _last_hourly > 3600) and _claim("lease:hourly", 3600)):
        _last_hourly = now_m
        from . import maintenance

        jobs.append(("maintenance", maintenance.run))
    for name, job in jobs:
        try:
            job()
        except Exception as exc:  # noqa: BLE001
            internal.warn(f"scheduler.{name}", "job failed", exc)


def maybe_tick():
    global _last_local
    if not conf.get("SCHEDULER", "ENABLED"):
        return
    now = time.monotonic()
    if now - _last_local >= min(conf.get("SCHEDULER", "INTERVAL"), conf.get("METRICS", "FLUSH_INTERVAL")):
        _last_local = now
        tick()
