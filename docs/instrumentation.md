# Python API

Everything here is optional. Requests, SQL, outbound HTTP, templates, exceptions and standard `logging`
are captured automatically. Use the API for what only your code knows.

```python
from django_observatory import observe, span, task, metrics, audit, security, bind, capture_exception
```

All functions are safe to call anywhere: they never raise into your code and never block on storage.
Outside a request they still work; correlation ids are simply empty.

## Logging

### Standard library logging

```python
import logging
logger = logging.getLogger(__name__)

logger.info("Material created")
logger.warning("Stock level is low for %s", code)
logger.error("Material import failed", extra={"batch": 4711, "supplier": "ACME"})
logger.exception("Failed to process material")      # also records the exception and groups it into an issue
```

Each record is stored with its level, logger, message, file, line and function, the request id, trace id
and span id, the user and IP when known, host, process and thread, and everything passed in `extra=` as
metadata. Messages and metadata are redacted before they are queued.

The handler is attached to the root logger at start-up (`LOGGING.AUTO_ATTACH`). To wire it yourself, see
[configuration](configuration.md#logging). Loggers configured with `propagate: False` need the handler added
explicitly.

Records from `django.db.backends`, `django.utils.autoreload`, `django.template` and `django.server` are
ignored by default (`LOGGING.IGNORE_LOGGERS`).

### Structured events: `observe`

```python
observe.debug("cache_miss", key="rates")
observe.info("material_created", material_code="ABC123", quantity=100)
observe.warning("stock_low", material_code="ABC123", quantity=4)
observe.error("import_failed", batch=4711, reason="bad header")
observe.critical("ledger_out_of_balance", difference="12.50")
```

The first argument is the event name; keyword arguments become searchable metadata. Pass
`category="billing"` to set the category (default `application`).

Prefer structured events over formatted strings: the name groups them, and a key such as `token=` is
redacted by key rather than relying on pattern matching.

## Tracing

### `span`

```python
with span("generate_monthly_report"):
    generate_report()

with span("generate_monthly_report", report_type="monthly", department="projects") as s:
    rows = build()
    s.set(rows=len(rows))            # add attributes while it runs

@span("recalculate_prices")          # decorator; works on async functions too
def recalculate(): ...

async with span("fetch_rates"):      # async context manager
    await fetch()
```

Spans nest automatically. SQL and outbound calls made inside a span appear beneath it in the waterfall. An
exception leaving the block marks the span as an error and is re-raised unchanged.

Outside a request, the outermost span starts a new trace of its own, so scripts and management commands
are traced too.

Signature: `span(name, kind="custom", **attributes)`. A trace keeps at most `TRACING.MAX_SPANS_PER_TRACE`
spans; the number dropped is recorded on the trace.

### `task`

For background jobs, scheduled commands and queue consumers. No Celery is assumed.

```python
@task
def generate_report(): ...

@task(name="reports.monthly")
async def monthly(): ...
```

Each run becomes a trace of kind `task` with its duration and outcome, an unhandled exception is captured
and grouped into an issue (then re-raised), and the metrics `task.runs{task, outcome}` and
`task.duration{task}` are recorded. Inside a request, a task is recorded as a span of the request's trace.

### Request context

```python
bind(tenant="acme", plan="enterprise")       # attached to every log record of this request / task

request.observability.request_id             # also in the X-Request-ID response header
request.observability.trace_id
```

### Outbound HTTP

Calls through `urllib.request`, `requests` and `httpx` (sync and async) are recorded automatically with
service, host, method, path, status, duration and outcome. Query strings, headers and bodies are not
stored. A `traceparent` header is added to outgoing requests so downstream services can join the trace.

For any other client, wrap the call in a span. It appears in the trace waterfall (coloured as an external
call), but not on the External APIs page, which is built from the automatic instrumentation:

```python
with span("POST payment-gateway", kind="http", service="Payments"):
    client.charge(...)
```

## Metrics

```python
metrics.increment("invoice.processed")
metrics.increment("invoice.amount", 1250.0, labels={"currency": "PKR"})
metrics.gauge("queue.depth", 42)
metrics.histogram("import.rows", 18234)
metrics.histogram("render.ms", 84.2, unit="ms")

with metrics.timer("invoice.processing"):            # histogram of elapsed milliseconds
    process_invoice()

@metrics.timer("pdf.render", labels={"engine": "libreoffice"})
def render(): ...

@metrics.timed("rates.fetch")                        # decorator that also supports async functions
async def fetch_rates(): ...
```

| Kind | Use for | Read as |
|---|---|---|
| counter | things that happen | total and rate per period |
| gauge | a level that goes up and down | latest / average value |
| histogram | durations and sizes | average, min, max, percentiles, distribution |
| timer | a histogram of elapsed milliseconds | |

Recording a metric is an in-memory update; values are written once per `METRICS.FLUSH_INTERVAL` (60 s).

**Keep label values bounded.** Every distinct combination of label values is a separate series. Use a
route, a status class or a region; never a user id, an order id or a URL with ids in it. The process keeps
at most 5,000 series per flush interval and drops new ones beyond that (shown on the Health page).

Histogram bucket bounds (unit-agnostic): 1, 2, 5, 10, 25, 50, 100, 250, 500, 1 000, 2 500, 5 000, 10 000,
30 000, 60 000. Percentiles are interpolated within a bucket and never exceed the observed maximum.

### Built-in metrics

| Metric | Kind | Labels |
|---|---|---|
| `http.requests` | counter | `route`, `method`, `status` (`2xx` … `5xx`) |
| `http.duration` | histogram (ms) | `route` |
| `db.queries`, `db.slow_queries` | counter | `alias` |
| `db.duration` | histogram (ms) | `alias` |
| `ext.calls` | counter | `service`, `outcome` (`ok` / `error`) |
| `ext.duration` | histogram (ms) | `service` |
| `exceptions` | counter | `type` |
| `logs` | counter | `level` |
| `security.events` | counter | `event` |
| `audit.events` | counter | `action` |
| `task.runs` | counter | `task`, `outcome` |
| `task.duration` | histogram (ms) | `task` |
| `system.cpu_percent`, `system.memory_percent`, `system.memory_used_mb`, `system.memory_total_mb`, `system.load1` | gauge | `host` |
| `system.disk_percent`, `system.disk_free_gb`, `system.disk_total_gb` | gauge | `host`, `mount` |
| `process.threads`, `process.memory_mb`, `process.cpu_percent` | gauge | `pid` |
| `obs.overhead` | histogram (ms) | — the package's own per-request cost |

### Reading metrics in code

```python
import datetime
from django.utils import timezone
from django_observatory import metrics

end = timezone.now(); start = end - datetime.timedelta(hours=1)

total = metrics.total("http.duration", start, end, route="/api/materials/")
total.count, total.avg, total.max, total.quantile(0.95)

metrics.by_label("ext.calls", "service", start, end)            # {"SAP": Agg, ...}
metrics.timeseries("http.requests", start, end, step=300)        # [(datetime, Agg), ...]
```

For a counter the total is `Agg.sum`; `Agg.count` is the number of increments.

## Audit trail

```python
audit.record(
    action="UPDATE",
    object_type="Material",
    object_id="A1023",
    changes={"quantity": {"before": 120, "after": 80}},
)

audit.record("DELETE", "Invoice", invoice.pk, result="FAILURE", reason="Period is closed", user=request.user)
audit.record("EXPORT", "CustomerList", before=None, after=None, rows=5321, format="xlsx")
```

Signature:

```python
audit.record(action, object_type="", object_id="", *, changes=None, before=None, after=None,
             result="SUCCESS", reason="", user=None, username="", **metadata)
```

The user, request id, trace id, IP and time are filled in from the current request. `changes` in the
`{"field": {"before": x, "after": y}}` form also fills `before` and `after`. Extra keyword arguments are
stored as metadata. Values are redacted by key, so a `password` field in `changes` is never stored.

Records are append-only and hash-chained; see [security](security.md#audit-trail-integrity). Changes made
through the Django admin are audited automatically.

## Security events

```python
security.record(event="PERMISSION_DENIED", resource="/api/materials/", reason="Missing permission")
security.record("SESSION_REVOKED", user=target_user, reason="Password reset by administrator", request=request)
```

Signature:

```python
security.record(event, *, resource="", reason="", user=None, username="", ip="",
                severity=None, request=None, **metadata)
```

Recorded automatically: `LOGIN_SUCCESS`, `SUPERUSER_LOGIN`, `LOGOUT`, `LOGIN_FAILURE` (Django auth signals),
`PASSWORD_CHANGE` (whenever a user's password is set and saved), `PERMISSION_DENIED` (any 403 response) and
`CSRF_FAILURE`. Event names are free-form upper-case strings; use your own for domain events.

## Exceptions

Unhandled exceptions in views and tasks, and anything logged with `logger.exception(...)` or
`exc_info=True`, are captured automatically. To record one you handle yourself:

```python
try:
    sync_with_sap()
except SapUnavailable as exc:
    capture_exception(exc, handled=True)
    use_cached_values()
```

The same exception object is only recorded once, however many layers see it. Local variables are never
collected. `Http404`, `PermissionDenied` and `SuspiciousOperation` are treated as control flow, not
defects (`EXCEPTIONS.IGNORE`).

## Suppressing capture

```python
from django_observatory.context import suppressed

with suppressed():
    run_noisy_internal_job()         # nothing inside is observed
```

## Testing your project

```python
# settings for tests
OBSERVABILITY = {"PIPELINE": {"SYNC": True}, "SCHEDULER": {"ENABLED": False}}   # deterministic writes
# or switch it off entirely
OBSERVABILITY = {"ENABLED": False}
```

With `SYNC` you can assert on what was captured:

```python
from django_observatory.models import AuditEvent

def test_update_is_audited(client):
    client.post("/materials/A1023/", {"quantity": 80})
    assert AuditEvent.objects.filter(object_id="A1023", action="UPDATE").exists()
```
