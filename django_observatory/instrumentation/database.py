"""SQL monitoring through Django's ``connection.execute_wrappers`` (works for every
database backend, sync or async, with no monkey-patching)."""
import time

from django.db import connections
from django.db.backends.signals import connection_created

from .. import conf, context, fingerprints, internal, metrics, pipeline, redaction

_OWN_TABLES = "django_observatory_"


def _record(sql, params, many, db_context, duration, ok, t_wall):
    conn = db_context["connection"]
    alias, vendor = conn.alias, conn.vendor
    labels = {"alias": alias}
    metrics.increment("db.queries", labels=labels)
    metrics.histogram("db.duration", duration, labels, "ms")
    cfg = conf.get("DATABASE")
    slow = duration >= cfg["SLOW_QUERY_MS"]
    if slow:
        metrics.increment("db.slow_queries", labels=labels)
    captured = redaction.clean(list(params) if not many and params else None) if cfg["CAPTURE_PARAMS"] else None
    ctx = context.current()
    if ctx is not None:
        ctx.db_count += 1
        ctx.db_ms += duration
        if slow:
            ctx.slow_queries += 1
            ctx.crumb("db", f"slow query {duration:.0f}ms", "warning")
        if slow or len(ctx.queries) < cfg["MAX_QUERIES_PER_REQUEST"]:
            # raw tuple only: normalisation is deferred until the trace is known to be kept
            ctx.queries.append(((t_wall - ctx.start_wall) * 1000, sql, duration, alias, vendor, ok, many,
                                context.current_span_id(), captured))
    elif slow or not ok:  # outside any trace (shell, command): keep the interesting ones
        norm = fingerprints.normalize_sql(sql)
        pipeline.enqueue("query", {
            "timestamp": context.to_dt(t_wall), "alias": alias, "vendor": vendor, "duration_ms": duration,
            "sql": redaction.text(sql, 8000), "normalized_sql": norm,
            "fingerprint": fingerprints.sql_fingerprint(norm), "params": captured, "success": ok,
            "is_slow": slow, "many": many,
        }, high=True)


def query_wrapper(execute, sql, params, many, db_context):
    if context.is_suppressed() or not conf.enabled("DATABASE"):
        return execute(sql, params, many, db_context)
    t_wall, t0, ok = time.time(), time.perf_counter(), True
    try:
        return execute(sql, params, many, db_context)
    except Exception:
        ok = False
        raise
    finally:
        try:
            text = sql if isinstance(sql, str) else str(sql)
            if _OWN_TABLES not in text:
                _record(text, params, many, db_context, (time.perf_counter() - t0) * 1000, ok, t_wall)
        except Exception as exc:  # noqa: BLE001
            internal.warn("database", "query instrumentation failed", exc)


def _attach(connection, **_):
    if query_wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(query_wrapper)


def install():
    connection_created.connect(_attach, dispatch_uid="django_observatory.db")
    for conn in connections.all(initialized_only=True):
        _attach(conn)
