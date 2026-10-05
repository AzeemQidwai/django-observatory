# JSON API and exports

A read-only API over the same data and filters as the UI, for scripts and integrations inside your network.

## Authentication and permissions

The API uses Django's session authentication: the caller must be logged in, with the same permissions the
UI requires for that data. There is no token authentication built in; to call it from another system, put
it behind your project's own authentication (for example a view decorator or middleware on the
`observability/api/` prefix).

| Response | Meaning |
|---|---|
| `401 {"error": "forbidden"}` | not logged in |
| `403 {"error": "forbidden"}` | logged in without the required permission |
| `400 {"error": "..."}` | invalid query or parameter; the message is safe to show |
| `404` | unknown resource or id |

All values are redacted on output.

## List

```text
GET /observability/api/<resource>/
```

| Resource | Permission |
|---|---|
| `logs` | `view_logs` |
| `requests` | `view_requests` |
| `traces` | `view_traces` |
| `exceptions`, `issues` | `view_exceptions` |
| `security` | `view_security_events` |
| `audit` | `view_audit_events` |
| `queries`, `external_calls` | `view_metrics` |
| `incidents` | `view_observability` |

| Parameter | Default | Description |
|---|---|---|
| `q` | | [Query language](query-language.md) filter |
| `range` | `24h` | `15m`, `1h`, `6h`, `24h`, `7d`, `30d`, `all` |
| `sort` | newest first | a column name, prefix `-` for descending |
| `limit` | `100` | 1–1000 |
| `offset` | `0` | |

```bash
curl -b "sessionid=..." "https://app.example.com/observability/api/requests/?q=status:>=500&range=1h&limit=2"
```

```json
{
  "results": [
    {"id": 812, "request_id": "req_20ec78ee3f5a234b869ea0a1", "trace_id": "…", "timestamp": "2026-10-05T06:12:20+00:00",
     "method": "GET", "path": "/api/materials/", "route": "/api/materials/", "status_code": 500,
     "duration_ms": 2931.4, "username": "ahmed", "db_count": 1, "db_ms": 1.2, "ext_count": 1, "ext_ms": 2004.8,
     "headers": {"Authorization": "[REDACTED]", "Host": "app.example.com"}, "…": "…"}
  ],
  "next_offset": 2
}
```

`next_offset` is `null` on the last page. Field names are the model's column names; see the
[data model](data-model.md).

## Detail

```text
GET /observability/api/<resource>/<id>/
```

Returns one record by primary key, with the same permission as the list.

## Metrics

```text
GET /observability/api/metrics/                       list of metric names, kinds and units
GET /observability/api/metrics/?name=http.duration&range=6h
```

```json
{"name": "http.duration", "range": "6h", "points": [
  {"t": "2026-10-05T06:00:00+00:00", "count": 412, "sum": 51230.4, "min": 2.1, "max": 2931.4, "avg": 124.3, "p95": 480.0}
]}
```

Requires `view_metrics`. `p95` is `null` for counters and gauges.

## Health

```text
GET /observability/api/health/
```

Returns the health report and HTTP `503` when the status is `UNHEALTHY`, so it can be polled by a load
balancer or an external monitor (with authentication).

```json
{"status": "HEALTHY",
 "checks": [{"name": "Database", "status": "HEALTHY", "detail": "'default' (postgresql) answered in 0.8 ms"}, "…"],
 "pipeline": {"enqueued": 18233, "written": 18233, "dropped": 0, "failed": 0, "retries": 0, "queue_depth": 0,
              "queue_size": 10000, "worker_alive": true, "sync": false, "last_write": 1759644732.1, "started": 1759640000.0},
 "internal_errors": {"count": 0, "last": "", "last_at": null},
 "dropped_metric_series": 0}
```

## Exports

### From the UI

Every list page has **CSV**, **JSON** and **NDJSON** buttons that export the current filter and time range
(requires `export_observability_data`; in `staff` mode every staff user has it). The same works by URL:

```text
GET /observability/logs/?q=level:error&range=7d&export=ndjson
```

Responses are streamed, capped at 50,000 rows, and sent as attachments.

### From the command line

```bash
python manage.py observability_export logs --format ndjson --query "level:error" --output errors.ndjson
python manage.py observability_export audit --format csv --output audit-2026-10.csv
```

See [management commands](commands.md).

### Guarantees

- **Redacted again on output**, so redaction rules added after the data was captured also apply.
- **CSV cells that begin with `=`, `+`, `-` or `@`** are prefixed with an apostrophe, so a spreadsheet
  does not execute them as formulas.
- Nested values (metadata, headers, frames) are JSON-encoded in CSV cells.

## Reading the data directly

The models are ordinary Django models, and the data is yours:

```python
from django_observatory.models import RequestRecord, Issue

RequestRecord.objects.filter(status_code__gte=500, route="/api/materials/").count()
Issue.objects.filter(status="OPEN").order_by("-occurrence_count")[:10]
```

Any BI or reporting tool that can read your database can read these tables.
