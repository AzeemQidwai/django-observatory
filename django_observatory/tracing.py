"""Spans and tasks.

    from django_observatory import span, task

    with span("generate_monthly_report", report_type="monthly"):
        ...

    @task
    def nightly_sync(): ...
"""
import functools
import inspect
import random
import time

from . import conf, context, fingerprints, internal, metrics, pipeline, redaction


class span:  # noqa: N801 - used as ``with span(...)`` / ``@span(...)``
    """Context manager (sync + async) and decorator. Outside any request it becomes
    the root of a new trace, so scripts and management commands are traced too."""

    def __init__(self, name, kind="custom", **attributes):
        self.name = name
        self.kind = kind
        self.attributes = attributes
        self.status = "ok"
        self._ctx = self._own = self._token = None

    def set(self, **attributes):
        self.attributes.update(attributes)
        return self

    def __enter__(self):
        try:
            if context.is_suppressed() or not conf.enabled("TRACING"):
                return self
            ctx = context.current()
            if ctx is None:
                ctx = context.TraceContext(self.name, kind="task" if self.kind == "task" else "manual")
                self._own = context.activate(ctx)
                self.id = ctx.root_span_id
            else:
                self.id = context.new_span_id()
                self._parent = context.current_span_id()
                self._token = context._span.set(self.id)
            self._ctx = ctx
            self._t0 = time.perf_counter()
        except Exception as exc:  # noqa: BLE001
            internal.warn("span.enter", "failed", exc)
            self._ctx = None
        return self

    def __exit__(self, exc_type, exc, tb):
        ctx = self._ctx
        if ctx is None:
            return False
        try:
            now = time.perf_counter()
            duration = (now - self._t0) * 1000
            if exc is not None:
                self.status = "error"
                self.attributes.setdefault("error", f"{exc_type.__name__}: {exc}")
            if self._own is not None:
                if exc is not None:
                    from .instrumentation.exceptions import capture_exception

                    ctx.error = True
                    capture_exception(exc, handled=False)
                finish_trace(ctx, self.name, duration, attributes=self.attributes)
            else:
                ctx.add_span({
                    "span_id": self.id, "parent_span_id": self._parent, "name": self.name, "kind": self.kind,
                    "offset_ms": (self._t0 - ctx.start) * 1000, "duration_ms": duration, "status": self.status,
                    "attributes": self.attributes,
                })
        except Exception as e:  # noqa: BLE001
            internal.warn("span.exit", "failed", e)
        finally:
            if self._own is not None:
                context.deactivate(self._own)
            elif self._token is not None:
                context._span.reset(self._token)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)

    def __call__(self, fn):
        name, kind, attrs = self.name, self.kind, self.attributes
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a, **kw):
                with span(name, kind, **attrs):
                    return await fn(*a, **kw)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with span(name, kind, **attrs):
                return fn(*a, **kw)
        return wrapper


def task(fn=None, *, name=None):
    """Instrument a background job/command, with or without arguments. No Celery assumed."""

    def deco(f):
        task_name = name or f"{f.__module__}.{f.__qualname__}"

        def _done(t0, ok):
            metrics.increment("task.runs", labels={"task": task_name, "outcome": "ok" if ok else "error"})
            metrics.histogram("task.duration", (time.perf_counter() - t0) * 1000, {"task": task_name}, "ms")

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def awrapper(*a, **kw):
                t0, ok = time.perf_counter(), False
                try:
                    with span(task_name, "task"):
                        result = await f(*a, **kw)
                    ok = True
                    return result
                finally:
                    _done(t0, ok)
            return awrapper

        @functools.wraps(f)
        def wrapper(*a, **kw):
            t0, ok = time.perf_counter(), False
            try:
                with span(task_name, "task"):
                    result = f(*a, **kw)
                ok = True
                return result
            finally:
                _done(t0, ok)
        return wrapper

    return deco(fn) if callable(fn) else deco


def keep_decision(error, duration_ms):
    """Tail sampling: errors and slow operations are always kept."""
    if error or duration_ms >= conf.get("REQUESTS", "SLOW_MS"):
        return True
    rate = conf.get("SAMPLING", "requests", default=1.0)
    return rate >= 1.0 or random.random() < rate


def finish_trace(ctx, name, duration_ms, *, keep=None, route="", root_attributes=None, attributes=None):
    """Serialise and enqueue everything buffered on a finished trace context.

    Expensive work (SQL normalisation, redaction) happens here, only for kept traces.
    Slow queries are persisted even when the trace itself is sampled out.
    """
    error = ctx.error
    if keep is None:
        keep = keep_decision(error, duration_ms)
    slow_ms = conf.get("DATABASE", "SLOW_QUERY_MS")
    to_dt, start = context.to_dt, ctx.start_wall
    enqueue = pipeline.enqueue

    for q in ctx.queries:
        offset, sql, dur, alias, vendor, ok, many, parent_id, params = q
        slow = dur >= slow_ms
        if not (keep or slow):
            continue
        norm = fingerprints.normalize_sql(sql)
        span_id = context.new_span_id()
        enqueue("query", {
            "timestamp": to_dt(start + offset / 1000), "alias": alias, "vendor": vendor, "duration_ms": dur,
            "sql": redaction.text(sql, 8000), "normalized_sql": norm,
            "fingerprint": fingerprints.sql_fingerprint(norm), "params": params, "success": ok,
            "is_slow": slow, "many": many, "route": route, "request_id": ctx.request_id,
            "trace_id": ctx.trace_id, "span_id": span_id,
        }, high=slow)
        if keep:
            ctx.add_span({
                "span_id": span_id, "parent_span_id": parent_id, "name": norm[:120], "kind": "db",
                "offset_ms": offset, "duration_ms": dur, "status": "ok" if ok else "error",
                "attributes": {"db.alias": alias, "db.system": vendor, "db.slow": slow},
            })
    if not keep:
        return False

    for e in ctx.externals:
        e = dict(e)
        ts = to_dt(start + e.pop("offset_ms") / 1000)
        enqueue("external", {**e, "route": route, "request_id": ctx.request_id, "trace_id": ctx.trace_id,
                             "timestamp": ts}, high=e["error"])
    attrs = redaction.clean(root_attributes or attributes or {})
    enqueue("trace", {
        "trace_id": ctx.trace_id, "request_id": ctx.request_id if ctx.kind == "request" else "",
        "timestamp": to_dt(start), "name": name, "kind": ctx.kind, "duration_ms": duration_ms,
        "span_count": len(ctx.spans) + 1, "dropped_spans": ctx.dropped_spans, "error": error,
        "user_id": ctx.user_id, **conf.dims(),
    }, high=error)
    enqueue("span", {
        "trace_id": ctx.trace_id, "span_id": ctx.root_span_id, "parent_span_id": ctx.parent_span_id,
        "name": name, "kind": ctx.kind, "timestamp": to_dt(start), "offset_ms": 0.0,
        "duration_ms": duration_ms, "status": "error" if error else "ok", "attributes": attrs,
    })
    for s in ctx.spans:
        s["attributes"] = redaction.clean(s["attributes"])
        enqueue("span", {**s, "trace_id": ctx.trace_id, "timestamp": to_dt(start + s["offset_ms"] / 1000)})
    from .instrumentation import otel

    otel.export_trace(ctx, name, duration_ms)
    return True

