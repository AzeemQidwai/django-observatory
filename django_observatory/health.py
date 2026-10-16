"""Self-monitoring: is observability itself healthy, and what does it cost?"""
import datetime
import time

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from . import conf, context, internal, metrics, redaction
from .pipeline import pipeline

HEALTHY, DEGRADED, UNHEALTHY = "HEALTHY", "DEGRADED", "UNHEALTHY"


def _check(name, fn):
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001
        status, detail = UNHEALTHY, f"{type(exc).__name__}: {exc}"
    return {"name": name, "status": status, "detail": detail}


def _alias():
    return conf.get("STORAGE", "DATABASE_ALIAS") or "default"


def _database():
    t0 = time.perf_counter()
    with connections[_alias()].cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    ms = (time.perf_counter() - t0) * 1000
    return (HEALTHY if ms < 500 else DEGRADED), f"'{_alias()}' ({connections[_alias()].vendor}) answered in {ms:.1f} ms"


def _migrations():
    executor = MigrationExecutor(connections[_alias()])
    targets = [k for k in executor.loader.graph.leaf_nodes() if k[0] == "django_observatory"]
    pending = executor.migration_plan(targets)
    if pending:
        return UNHEALTHY, f"{len(pending)} unapplied migration(s): run manage.py migrate"
    return HEALTHY, "all migrations applied"


def _storage():
    from .models import ObservabilityConfiguration as Cfg

    Cfg.objects.update_or_create(key="health:probe", defaults={"updated_at": timezone.now(), "value": {}})
    return HEALTHY, f"backend '{conf.get('STORAGE', 'BACKEND')}' accepts writes"


def _queue():
    h = pipeline.health()
    fill = h["queue_depth"] / max(h["queue_size"], 1)
    detail = f"{h['queue_depth']}/{h['queue_size']} queued, {h['dropped']} dropped, {h['failed']} failed"
    if fill >= 0.8 or h["failed"]:
        return (UNHEALTHY if fill >= 0.95 else DEGRADED), detail
    return (DEGRADED if h["dropped"] else HEALTHY), detail


def _worker():
    h = pipeline.health()
    if h["sync"]:
        return DEGRADED, "synchronous mode (PIPELINE.SYNC): writes happen inside requests"
    if h["started"] is None:
        return HEALTHY, "idle: starts on the first event in this process"
    if not h["worker_alive"]:
        return UNHEALTHY, "worker thread is not running (it restarts on the next event)"
    return HEALTHY, f"running, {h['written']} rows written, {h['retries']} retries"


def _ingestion():
    h = pipeline.health()
    if h["last_write"] is None:
        return HEALTHY, "no events written by this process yet"
    age = time.time() - h["last_write"]
    stuck = h["queue_depth"] > 0 and age > 60
    return (UNHEALTHY if stuck else HEALTHY), f"last write {age:.0f}s ago"


def _configuration():
    from .checks import check_configuration

    problems = check_configuration(None)
    errors = [p for p in problems if p.level >= 40]
    if errors:
        return UNHEALTHY, errors[0].msg
    if problems:
        return DEGRADED, f"{len(problems)} warning(s): {problems[0].msg}"
    return HEALTHY, "settings valid"


def _redaction():
    probe = redaction.clean({"password": "x", "note": "Authorization: Bearer abc123def"})
    ok = probe["password"] == redaction.REDACTED and "abc123def" not in probe["note"]
    return (HEALTHY, "engine active") if ok else (UNHEALTHY, "redaction self-test failed")


def _instrumentation():
    from django.conf import settings

    from .checks import MW
    from .instrumentation.database import query_wrapper

    parts = []
    if MW in settings.MIDDLEWARE:
        parts.append("middleware")
    if any(query_wrapper in c.execute_wrappers for c in connections.all(initialized_only=True)):
        parts.append("database")
    from .instrumentation import http

    if http._installed:
        parts.append("http")
    if not conf.get("ENABLED"):
        return DEGRADED, "observability is disabled (ENABLED=False)"
    return (HEALTHY if "middleware" in parts else DEGRADED), "active: " + (", ".join(parts) or "none")


def _overhead():
    end = timezone.now()
    agg = metrics.total("obs.overhead", end - datetime.timedelta(hours=1), end)
    if not agg.count:
        return HEALTHY, "no measurements yet"
    p95 = agg.quantile(0.95)
    return (HEALTHY if p95 < 10 else DEGRADED), f"avg {agg.avg:.2f} ms, p95 {p95:.2f} ms per request (last hour)"


def _server():
    from . import system

    s = system.sample()
    limits = conf.get("SYSTEM")
    parts, status = [], HEALTHY
    if s["cpu"] is not None:
        parts.append(f"CPU {s['cpu']:.0f}%")
    if s["memory"]:
        parts.append(f"memory {s['memory']['percent']:.0f}%")
        if s["memory"]["percent"] >= limits["MEMORY_PERCENT"]:
            status = DEGRADED
    for d in s["disks"] or []:
        parts.append(f"disk {d['mount']} {d['percent']:.0f}% ({d['free_gb']:.1f} GB free)")
        if d["percent"] >= 97:
            status = UNHEALTHY
        elif d["percent"] >= limits["DISK_PERCENT"] and status == HEALTHY:
            status = DEGRADED
    if not parts:
        return DEGRADED, "no readings available on this platform (install psutil)"
    return status, ", ".join(parts) + f" [{s['source']}]"


# UTC "now" according to the database server (None where it is the same machine or unknown).
_DB_UTC_NOW = {
    "postgresql": "SELECT CURRENT_TIMESTAMP",
    "microsoft": "SELECT SYSUTCDATETIME()",
    "mysql": "SELECT UTC_TIMESTAMP()",
    "oracle": "SELECT SYS_EXTRACT_UTC(SYSTIMESTAMP) FROM DUAL",
}
CLOCK_TOLERANCE = datetime.timedelta(minutes=2)


def _clock():
    """Every timestamp comes from the application server's clock. A wrong clock stores
    records at the wrong time, so look for both the cause and the symptom."""
    from .models import ObservabilityEvent, RequestRecord

    now = timezone.now()
    problems = []
    conn = connections[_alias()]
    sql = _DB_UTC_NOW.get(conn.vendor)
    if sql:
        with conn.cursor() as cur:
            cur.execute(sql)
            db_now = cur.fetchone()[0]
        if db_now.tzinfo is None:
            db_now = db_now.replace(tzinfo=datetime.timezone.utc)
        skew = (now - db_now).total_seconds()
        if abs(skew) > CLOCK_TOLERANCE.total_seconds():
            problems.append(f"this server's clock is {abs(skew):.0f}s {'ahead of' if skew > 0 else 'behind'} "
                            f"the database server's")
    limit = now + CLOCK_TOLERANCE
    future = {name: model.objects.filter(timestamp__gt=limit)
              for name, model in (("requests", RequestRecord), ("log events", ObservabilityEvent))}
    counts = {name: qs.count() for name, qs in future.items()}
    if any(counts.values()):
        latest = max(qs.order_by("-timestamp").values_list("timestamp", flat=True).first()
                     for name, qs in future.items() if counts[name])
        detail = ", ".join(f"{n} {name}" for name, n in counts.items() if n)
        problems.append(f"{detail} dated in the future (latest {timezone.localtime(latest):%Y-%m-%d %H:%M}): "
                        "a server clock was ahead when they were written")
    if problems:
        return DEGRADED, "; ".join(problems)
    return HEALTHY, "no future-dated records" + ("; clock agrees with the database server" if sql else "")


CHECKS = (
    ("Database", _database), ("Migrations", _migrations), ("Storage", _storage), ("Event Queue", _queue),
    ("Worker", _worker), ("Event Ingestion", _ingestion), ("Configuration", _configuration),
    ("Redaction", _redaction), ("Instrumentation", _instrumentation), ("Overhead", _overhead),
    ("Server Resources", _server),
    ("Clock", _clock),
)


def report():
    with context.suppressed():
        checks = [_check(name, fn) for name, fn in CHECKS]
    worst = UNHEALTHY if any(c["status"] == UNHEALTHY for c in checks) else \
        DEGRADED if any(c["status"] == DEGRADED for c in checks) else HEALTHY
    return {"status": worst, "checks": checks, "pipeline": pipeline.health(),
            "internal_errors": dict(internal.errors), "dropped_metric_series": metrics.dropped_series}
