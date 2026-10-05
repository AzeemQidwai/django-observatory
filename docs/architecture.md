# Architecture

```
application thread                      worker thread (one per process)
------------------                      -------------------------------
middleware / logging handler /          queue.get -> group by kind
execute_wrapper / http patch                 -> backend.write_batch (ONE transaction)
        |                                    -> retry with backoff, then per-kind fallback
sample -> redact -> enrich                   -> scheduler.maybe_tick():
        |                                         metrics flush   (every process)
  queue.put_nowait  ------------------>           alerts, incidents, rollup, retention
  (never blocks, never raises)                    (one process, elected by a DB lease)
```

## Modules

| Module | Responsibility |
|---|---|
| `conf` | `OBSERVABILITY` settings deep-merged over defaults; runtime overrides |
| `context` | `contextvars` trace context (request id, trace id, span, user, breadcrumbs) |
| `redaction` | The single redaction engine (`clean` for structures, `text` for strings) |
| `fingerprints` | SQL normalisation, exception and log fingerprints |
| `pipeline` | Bounded queue + daemon worker; priority shedding; retries |
| `storage/` | `ObservabilityBackend` contract and the Django ORM implementation |
| `middleware` | Request lifecycle (sync + async), correlation headers, tail sampling |
| `logging`, `observe` | stdlib handler and structured API |
| `tracing` | `span`, `task`, trace finalisation |
| `instrumentation/` | SQL (`execute_wrappers`), outbound HTTP, exceptions, templates, OpenTelemetry |
| `metrics` | In-process aggregation, flush, percentile and time-series reads |
| `system` | Host CPU, memory and disk sampling |
| `security`, `audit`, `signals` | Security events, audit trail, auth signal hooks |
| `alerts` | Rules, deduplication, notification channels |
| `correlation/` | `engine` (detection), `rules` (hypotheses), `causal`, `graph` |
| `search` | Query language -> `Q` objects |
| `tables`, `views`, `urls` | Declarative list pages, UI and JSON API |
| `health`, `maintenance`, `scheduler`, `exporters`, `checks` | Operations |

## Decisions worth knowing

**Metrics are counted for every operation; detailed records are sampled.** Request, SQL and HTTP metrics
are aggregated in memory (a dict update under a lock) and flushed once a minute, so dashboards, alerts and
incident detection stay accurate at 10% sampling. Detailed rows use tail sampling: errors and slow
requests are always kept. SQL normalisation and redaction run only for kept traces.

**Correlation by indexed ids, not foreign keys.** Telemetry is bulk-inserted asynchronously and may live in a
separate database, so `request_id` / `trace_id` columns link the records. The Application Observability
Graph is assembled from those relations; there is no graph database.

**One commit per batch, and WAL on SQLite.** The worker writes each batch in a single transaction. On SQLite
it switches the database to WAL so application reads are not blocked while telemetry is written
(`STORAGE.SQLITE_WAL`).

**No scheduler dependency.** Periodic work runs from the worker thread. Cluster-wide jobs are elected with
one atomic `UPDATE` on a lease row, so ten Waitress/gunicorn processes evaluate alerts once.

**The UI never observes itself.** Requests under the mounted prefix, the worker's own writes, notifications
and OTLP exports run in a suppressed context.

## Storage backends

Implement `django_observatory.storage.ObservabilityBackend` (`write`, `write_batch`, `query`,
`delete_before`, `get_statistics`) and point `STORAGE.BACKEND` at its dotted path. Rows arrive as plain,
already-redacted dicts. The built-in UI reads through the Django models, so a remote backend is a sink for
forwarding telemetry rather than a replacement for local storage.

## Deliberate limits

Marked with a `ponytail:` comment in the code, naming the ceiling and the upgrade path.

- Free-text search is a case-insensitive substring match. Portable everywhere; swap in FTS5 / tsvector if
  log volume makes it slow.
- Metric reads aggregate in Python over the window's rows; fine to roughly 10^5 rows thanks to rollups.
- List pages use offset pagination without `COUNT(*)`.
- The latency breakdown assumes external and SQL time do not overlap (true for synchronous code).
