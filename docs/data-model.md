# Data model

All tables belong to the app `django_observatory` and are prefixed `django_observatory_`. Only portable
column types are used. High-volume tables are linked by indexed ids (`request_id`, `trace_id`), not foreign
keys, because rows are bulk-inserted asynchronously and may live in a different database from your
application's tables. There is no foreign key to your user model: `user_id` and `username` are copied.

```text
RequestRecord ──request_id──┬── DatabaseQuery
      │                     ├── ExternalCall
      │                     ├── ObservabilityEvent (logs)
      │                     ├── ExceptionRecord ──FK──► Issue
      │                     ├── SecurityEvent
      │                     └── AuditEvent
      └──trace_id── TraceRecord ──trace_id── SpanRecord

Metric ──FK── MetricSample
AlertRule ──FK── Alert ──FK (optional)──► Incident ──FK── IncidentSignal
ObservabilityConfiguration (runtime overrides, scheduler leases, first-seen releases)
```

## Common columns

| Columns | On | Meaning |
|---|---|---|
| `request_id`, `trace_id`, `span_id` | most telemetry | correlation ids (`req_` + 24 hex; 32 hex; 16 hex) |
| `service`, `environment`, `release` | events, requests, traces, exceptions, security | deployment dimensions from settings |
| `timestamp` | all telemetry | when it happened (indexed) |

## Tables

### `ObservabilityEvent` — logs and structured events

`event_type` (`log` / `structured`), `level`, `level_no`, `logger_name`, `message`, `category`
(`application`, `http`, `database`, `security` or your own), `application`, `user_id`, `session_id` (a hash,
never the session key), `ip_address`, `hostname`, `process_id`, `thread_id`, `module`, `function`,
`file_name`, `line_number`, `exception_uid`, `metadata` (JSON), `fingerprint`.

### `RequestRecord`

`method`, `path`, `route` (URL pattern), `query_params` (JSON, empty unless enabled), `status_code`,
`duration_ms`, `user_id`, `username`, `session_id` (hash), `ip_address`, `user_agent`, `referrer` (without
query string), `content_type`, `response_size`, `view_name`, `view_module`, `exception_uid`, `db_count`,
`db_ms`, `slow_queries`, `ext_count`, `ext_ms`, `headers` (JSON, redacted), `metadata` (JSON).

### `TraceRecord` and `SpanRecord`

Trace: `trace_id`, `request_id`, `name`, `kind` (`request` / `task` / `manual`), `duration_ms`,
`span_count`, `dropped_spans`, `error`, `user_id`.

Span: `trace_id`, `span_id`, `parent_span_id`, `name`, `kind` (`request`, `view`, `db`, `http`, `template`,
`task`, `custom`), `offset_ms` (start relative to the trace), `duration_ms`, `status` (`ok` / `error`),
`attributes` (JSON).

### `DatabaseQuery`

`alias`, `vendor`, `duration_ms`, `sql` (as sent to the driver: placeholders, no values), `normalized_sql`,
`fingerprint`, `params` (JSON, `NULL` unless `DATABASE.CAPTURE_PARAMS`), `success`, `is_slow`, `many`,
`route`.

### `ExternalCall`

`service`, `host`, `method`, `path` (no query string), `status_code`, `duration_ms`, `error`, `timeout`,
`exc_type`, `response_size`, `route`.

### `Issue` and `ExceptionRecord`

Issue: `fingerprint` (unique), `exc_type`, `title`, `culprit` (module.function), `status`, `severity`,
`first_seen`, `last_seen`, `occurrence_count`, `assignee`, `notes`, `first_release`, `last_release`,
`resolved_at`.

Exception: `uid`, `issue` (FK), `exc_type`, `message`, `stacktrace`, `frames` (JSON: file, line, function,
module, in_app, code), `file_name`, `line_number`, `function`, `handled`, `user_id`, `endpoint`, `method`,
`breadcrumbs` (JSON), `fingerprint`. Local variables are never stored.

### `Metric` and `MetricSample`

Metric: `name` (unique), `kind` (`counter` / `gauge` / `histogram`), `unit`, `description`.

Sample — one pre-aggregated bucket: `metric` (FK), `timestamp` (bucket start), `resolution` (60, 3600 or
86400 seconds), `labels` (JSON), `labels_key`, `count`, `sum`, `min`, `max`, `last`, `buckets` (histogram
counts). Several rows may exist for the same bucket (one per writing process); readers add them up.

### `SecurityEvent`

`event`, `severity` (`info` / `notice` / `warning`), `user_id`, `username`, `ip_address`, `user_agent`,
`resource`, `reason`, `metadata` (JSON).

### `AuditEvent`

`user_id`, `username`, `action`, `object_type`, `object_id`, `before`, `after`, `changes` (JSON),
`ip_address`, `result`, `reason`, `metadata` (JSON), `prev_hash`, `hash`. Append-only; see
[security](security.md#audit-trail-integrity).

### `AlertRule` and `Alert`

Rule: see [alerts](alerts.md). Alert: `rule` (FK), `incident` (FK, optional), `status`, `severity`, `title`,
`message`, `value`, `threshold`, `started_at`, `last_seen`, `resolved_at`, `notified_at`, `evaluations`,
`acknowledged_by`.

### `Incident` and `IncidentSignal`

Incident: `title`, `status` (`OPEN` / `ACKNOWLEDGED` / `RESOLVED`), `severity`, `dedup_key`, `started_at`
(inferred), `detected_at`, `last_seen`, `resolved_at`, `cause_kind` (`external`, `database`, `resources`,
`deployment`, `exception`, `security`, `unexplained`), `cause_subject`, `probable_cause`, `confidence`,
`confidence_score`, `recommendation`, `affected_requests`, `affected_users`, `error_count`,
`affected_endpoints` (JSON), `alternatives` (JSON), `notes`.

Signal — an observed fact: `incident` (FK), `kind`, `source`, `relationship` (`cause` / `effect` /
`evidence`), `description`, `baseline`, `current`, `unit`, `score`, `ref_kind`, `ref_id`.

### `ObservabilityConfiguration`

Key/value rows: `runtime` (overrides from the Settings page), `lease:*` (scheduler election),
`release:*` (when each release was first seen), `defaults:*` (one-time set-up flags). The model also carries
the package's custom permissions.

## Volume

Per captured request: 1 request row, 1 trace row, 2 span rows plus one per SQL statement, outbound call,
template and manual span, and 1 row per SQL statement and outbound call. Metrics add roughly one row per
active label combination per minute and process, shrinking through rollups.

A rough guide for 100,000 requests a day with five queries each:

| Sampling | Rows per day | Notes |
|---|---|---|
| `1.0` | ≈ 1.5 million | fine for a dedicated SQLite file or a server database with 14–30 day retention |
| `0.2` | ≈ 300,000 plus errors and slow requests | recommended starting point for busy sites |

Use `observability_stats` or **Settings & System** to see real numbers, and adjust `SAMPLING`, `RETENTION`
and `REQUESTS.IGNORE_PATHS`.

## Indexes

Chosen for the queries the UI makes: time range on every table; `(level_no, timestamp)` and
`(category, timestamp)` on events; `(route, timestamp)`, `(status_code, timestamp)` and
`(user_id, timestamp)` on requests; `(fingerprint, timestamp)` and `(is_slow, timestamp)` on SQL;
`(service, timestamp)` on external calls; `request_id` and `trace_id` wherever they appear.

## Partitioning and archiving

Not implemented. Retention deletes by time in primary-key ranges, and every telemetry table has a single
`timestamp` column, so time-based partitioning or archive tables can be added at the database level without
changing the application.
