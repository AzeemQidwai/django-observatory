# Configuration reference

Everything is optional. Settings live in one dictionary that is deep-merged over the defaults, so you only
write what you change:

```python
OBSERVABILITY = {
    "SERVICE_NAME": "project-api",
    "ENVIRONMENT": "production",
    "SAMPLING": {"requests": 0.25},          # the other sampling keys keep their defaults
}
```

`python manage.py check` validates the configuration, and **Settings & System** in the UI shows the
effective values.

## General

| Setting | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Master switch. `False` disables all capture; the UI still opens. |
| `SERVICE_NAME` | `"django"` | Name of this application. Stamped on every record. |
| `ENVIRONMENT` | `"development"` | `development`, `testing`, `staging`, `production` or your own. |
| `APPLICATION` | `""` | Optional grouping when several services form one application. |
| `RELEASE` | `""` | Version / build identifier. Enables release comparison and deployment correlation. |
| `PERMISSION_MODE` | `"staff"` | `"staff"` or `"permissions"`. See [security](security.md#access). |
| `BREADCRUMBS` | `30` | Events remembered per request to attach to an exception. |

## `STORAGE`

| Key | Default | Description |
|---|---|---|
| `BACKEND` | `"django"` | `"django"` (ORM) or the dotted path of an `ObservabilityBackend` subclass. |
| `DATABASE_ALIAS` | `"default"` | Database holding telemetry. For another alias also add `ObservabilityRouter`; see [deployment](deployment.md). |
| `SQLITE_WAL` | `True` | On SQLite, switch the telemetry database to WAL so writes do not block readers. |

## `PIPELINE`

| Key | Default | Description |
|---|---|---|
| `QUEUE_SIZE` | `10000` | In-memory queue capacity per process. Above 80% low-priority events are dropped first. |
| `BATCH_SIZE` | `500` | Maximum events written in one transaction. |
| `FLUSH_INTERVAL` | `1.0` | Seconds the worker waits for events before writing what it has. |
| `MAX_RETRIES` | `3` | Retries of a failed batch (with backoff) before falling back to per-kind writes. |
| `SYNC` | `False` | Write in the calling thread. For tests only. |

## `LOGGING`

| Key | Default | Description |
|---|---|---|
| `AUTO_ATTACH` | `True` | Add `ObservabilityHandler` to the root logger at start-up, unless you already reference it. |
| `LEVEL` | `"INFO"` | Minimum level of the auto-attached handler. |
| `IGNORE_LOGGERS` | `["django.db.backends", "django.utils.autoreload", "django.template", "django.server"]` | Logger name prefixes never stored. |

When the root logger is unconfigured, auto-attach lowers its level to `LEVEL` and keeps the standard
behaviour of printing warnings to stderr. If you configured the root logger yourself, its level is left
alone.

Explicit wiring (auto-attach is then skipped):

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "observability": {"class": "django_observatory.logging.ObservabilityHandler", "level": "INFO"},
    },
    "root": {"handlers": ["observability"], "level": "INFO"},
    "loggers": {
        "payments": {"handlers": ["observability"], "level": "INFO", "propagate": False},
    },
}
```

## `REQUESTS`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Capture requests and build traces. |
| `IGNORE_PATHS` | `["/static/", "/media/", "/favicon.ico"]` | Path prefixes not observed at all. Add polling and health-check URLs. |
| `SLOW_MS` | `1000` | A request this slow is always kept regardless of sampling and gets "Why is this slow?". |
| `RESPONSE_HEADERS` | `True` | Add `X-Request-ID` and `X-Trace-ID` to responses. |
| `CAPTURE_HEADERS` | `True` | Store request headers (sensitive ones are always redacted). |
| `CAPTURE_QUERY_PARAMS` | `False` | Store query-string parameters (redacted by key). |
| `CAPTURE_BODY` | `False` | Store form fields, or the first `MAX_BODY_BYTES` of other bodies. Multipart is never stored. |
| `MAX_BODY_BYTES` | `4096` | Limit for non-form bodies. |
| `TRUST_REQUEST_ID_HEADER` | `False` | Reuse an incoming `X-Request-ID` (only safe characters, 8–64 long). |
| `TRUST_PROXY_HEADERS` | `False` | Take the client IP from `X-Forwarded-For`. Enable only behind a trusted proxy. |

The UI's own URLs are never observed.

## `DATABASE`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Monitor SQL through Django's `execute_wrappers` (all backends). |
| `SLOW_QUERY_MS` | `500` | Threshold for a slow query. Slow queries are always stored. |
| `MAX_QUERIES_PER_REQUEST` | `200` | Statements kept per request; beyond it they are only counted. |
| `CAPTURE_PARAMS` | `False` | Store parameter values. Off by default for privacy. |

## `EXTERNAL_HTTP`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Monitor `urllib`, `requests` and `httpx` calls. |
| `SERVICES` | `{}` | Hostname → display name, for example `{"sap.corp.local": "SAP", "api.stripe.com": "Stripe"}`. A key also matches its subdomains. Unmapped hosts are shown by hostname. |

## `TRACING`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Record spans. |
| `MAX_SPANS_PER_TRACE` | `500` | Cap per trace; extra spans are counted as dropped. |
| `TEMPLATES` | `True` | Record top-level template renders as spans. |

## `METRICS`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Record metrics (built-in and your own). |
| `FLUSH_INTERVAL` | `60` | Seconds between writes of aggregated buckets. |
| `PROCESS` | `True` | Record `process.*` gauges (threads; memory and CPU with psutil). |

## `SYSTEM`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Sample host CPU, memory and disk once per metrics flush. |
| `DISKS` | `None` | Paths to watch, e.g. `["C:\\", "D:\\"]`. `None` = system volume plus volumes the project writes to. |
| `CPU_PERCENT` | `90` | Level treated as a problem (tiles, alerts, incidents). |
| `MEMORY_PERCENT` | `90` | Same, for memory. |
| `DISK_PERCENT` | `90` | Same, for any watched volume. |

See [server monitoring](server-monitoring.md).

## `SECURITY`, `AUDIT`

`{"ENABLED": True}` each. Disabling stops recording; existing records stay.

## `EXCEPTIONS`

| Key | Default | Description |
|---|---|---|
| `IGNORE` | `["Http404", "PermissionDenied", "SuspiciousOperation"]` | Exception class names (matched anywhere in the class hierarchy) that never create issues. |

## `ALERTS`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Evaluate alert rules. |
| `DEFAULT_RULES` | `True` | Create the eight default rules once. |
| `EMAIL_TO` | `[]` | Recipients for the `email` channel (uses Django's email settings). |
| `WEBHOOK_URL` | `""` | Target of the `webhook` channel (JSON `POST`). |

See [alerts](alerts.md).

## `INCIDENTS`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Run incident detection. |
| `WINDOW_MINUTES` | `5` | The "now" window. |
| `BASELINE_MINUTES` | `60` | The preceding window it is compared with. |
| `MIN_REQUESTS` | `20` | Fewer requests than this in the window are not judged. |
| `RESOLVE_AFTER_MINUTES` | `15` | Quiet time after which an incident resolves itself. |

## `SCHEDULER`

| Key | Default | Description |
|---|---|---|
| `ENABLED` | `True` | Run periodic jobs from the worker thread. |
| `INTERVAL` | `60` | Seconds between alert and incident evaluations (once per cluster). |

## `SAMPLING`

A rate between 0 and 1 for each kind.

| Key | Default | Applies to |
|---|---|---|
| `requests` | `1.0` | Detailed request, trace, span and SQL rows of **successful, fast** requests. |
| `successful_logs` | `1.0` | Log records below WARNING. |
| `errors` | `1.0` | Log records at WARNING and above. |
| `exceptions` | `1.0` | Exception records. |
| `security` | `1.0` | Security events. |
| `audit` | `1.0` | Audit events. |

Requests that fail or exceed `REQUESTS.SLOW_MS` are always kept, slow queries are always kept, and metrics
count everything, so `requests: 0.1` on a busy site loses no errors and does not distort dashboards, alerts
or incident detection. Leave `security` and `audit` at `1.0`; `check` warns otherwise.

## `REDACTION`

| Key | Default | Description |
|---|---|---|
| `keys` | `[]` | Additional sensitive key names (case-insensitive substring match). |
| `patterns` | `[]` | Additional regular expressions; matches in free text are replaced by `[REDACTED]`. |

See [privacy](privacy.md).

## `RETENTION`

Days to keep each kind. Cleanup runs hourly in bounded batches.

| Key | Default | Removes |
|---|---|---|
| `logs` | `30` | log events |
| `requests` | `30` | requests, captured SQL, external calls |
| `traces` | `14` | traces and spans |
| `metrics` | `90` | metric samples (rolled up to hourly after 48 h and daily after 30 days) |
| `exceptions` | `180` | exception occurrences, and issues left without any |
| `security` | `365` | security events |
| `audit` | `365` | audit events (the only way they are ever removed) |
| `alerts` | `180` | resolved alerts and incidents |

Both `{"logs": 30}` and `{"LOGS_DAYS": 30}` spellings are accepted.

## `OTEL`

| Key | Default | Description |
|---|---|---|
| `enabled` | `False` | Export kept traces as OTLP/HTTP JSON. |
| `endpoint` | `""` | Collector base URL, e.g. `http://otel-collector:4318`. |
| `headers` | `{}` | Extra request headers (authentication). |
| `timeout` | `5` | Seconds. |

See [OpenTelemetry](opentelemetry.md).

## Flat setting names

These top-level settings are honoured as well:

| Setting | Equivalent |
|---|---|
| `OBSERVABILITY_ENVIRONMENT` | `ENVIRONMENT` |
| `OBSERVABILITY_SERVICE_NAME` | `SERVICE_NAME` |
| `OBSERVABILITY_RELEASE` | `RELEASE` |
| `OBSERVABILITY_SLOW_QUERY_THRESHOLD_MS` | `DATABASE.SLOW_QUERY_MS` |
| `OBSERVABILITY_SAMPLING` | `SAMPLING` |
| `OBSERVABILITY_REDACTION` | `REDACTION` |
| `OBSERVABILITY_RETENTION` | `RETENTION` |
| `OBSERVABILITY_OTEL` | `OTEL` |

## Runtime overrides

`SAMPLING.requests`, `SAMPLING.successful_logs`, `DATABASE.SLOW_QUERY_MS` and `REQUESTS.SLOW_MS` can be
changed from **Settings & System** without a deployment. They are stored in the database and reach every
process within a minute. A blank field means "use the value from settings".

## System checks

| Id | Meaning |
|---|---|
| `observability.E001` | Settings could not be loaded |
| `E002` | A sampling rate is not between 0 and 1 |
| `E003` | A retention value is not a positive number of days |
| `E004` | Unknown storage backend |
| `E005` | `DATABASE_ALIAS` is not in `DATABASES` |
| `E006` | The middleware is listed more than once |
| `E007` | Invalid `PERMISSION_MODE` |
| `E008` | A redaction pattern is not a valid regular expression |
| `E009` | OTEL enabled without an endpoint |
| `E010` | `QUEUE_SIZE` below 100 |
| `W001` | Security or audit sampling below 1.0 |
| `W002` | A separate database alias is set but the router is not installed |
| `W003` | The middleware is missing |
| `W004` | Body, query or SQL-parameter capture enabled with `DEBUG=False` |
| `W005` | Synchronous pipeline with `DEBUG=False` |

## Example: a production configuration

```python
OBSERVABILITY = {
    "SERVICE_NAME": "credit-portal",
    "ENVIRONMENT": "production",
    "RELEASE": os.environ.get("RELEASE", ""),
    "STORAGE": {"DATABASE_ALIAS": "observability"},
    "REQUESTS": {
        "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "/notifications/feed/", "/healthz"],
        "TRUST_PROXY_HEADERS": True,
    },
    "DATABASE": {"SLOW_QUERY_MS": 300},
    "EXTERNAL_HTTP": {"SERVICES": {"sap.corp.local": "SAP", "smtp-relay.corp.local": "Mail relay"}},
    "SAMPLING": {"requests": 0.2},
    "REDACTION": {"keys": ["cnic", "iban"], "patterns": [r"\b\d{5}-\d{7}-\d\b"]},
    "ALERTS": {"EMAIL_TO": ["ops@example.com"]},
    "SYSTEM": {"DISKS": ["C:\\", "D:\\"]},
}
DATABASE_ROUTERS = ["django_observatory.routers.ObservabilityRouter"]
```
