# Management commands

## `observability_test`

End-to-end self test against the real configured storage: logging, redaction, sampling configuration, a
request through the middleware, tracing, SQL capture, exception grouping, metrics, security and audit
events, failure isolation, the alert engine, incident detection and the OpenTelemetry adapter. Everything it
writes is tagged and deleted afterwards.

```bash
python manage.py observability_test
```

Exit code `1` if any step fails. Run it after installing, after upgrading and after changing databases.

## `observability_health`

```bash
python manage.py observability_health
```

```text
Observability Health

Database         HEALTHY    'default' (sqlite) answered in 0.1 ms
Migrations       HEALTHY    all migrations applied
Storage          HEALTHY    backend 'django' accepts writes
Event Queue      HEALTHY    0/10000 queued, 0 dropped, 0 failed
Worker           HEALTHY    idle: starts on the first event in this process
Event Ingestion  HEALTHY    no events written by this process yet
Configuration    HEALTHY    settings valid
Redaction        HEALTHY    engine active
Instrumentation  HEALTHY    active: middleware, database, http
Overhead         HEALTHY    avg 0.21 ms, p95 0.90 ms per request (last hour)
Server Resources HEALTHY    memory 51%, disk C:\ 62% (148.0 GB free) [psutil]

Overall: HEALTHY
```

Exit code `1` when the overall status is `UNHEALTHY`. Queue and worker figures describe the process running
the command, not the web workers; use the Health page for those.

## `observability_stats`

Rows stored and the oldest record for each kind of data.

```bash
python manage.py observability_stats
```

## `observability_cleanup`

Applies retention now and rolls up old metrics. The same work runs automatically every hour; use the
command for an explicit schedule or to reclaim space immediately.

```bash
python manage.py observability_cleanup
python manage.py observability_cleanup --kind logs --kind traces
python manage.py observability_cleanup --batch-size 500 --no-rollup
```

| Option | Description |
|---|---|
| `--kind` | Limit to `logs`, `requests`, `traces`, `metrics`, `exceptions`, `security`, `audit`, `alerts`. Repeatable. |
| `--batch-size` | Rows deleted per transaction (default 2000). Smaller = shorter locks. |
| `--no-rollup` | Skip metric downsampling. |

Deletion is done in primary-key ranges, never as one large `DELETE`.

## `observability_export`

```bash
python manage.py observability_export <resource> [--format ndjson|json|csv] [--query "..."] [--limit N] [--output FILE]
```

| Argument | Description |
|---|---|
| `resource` | `logs`, `requests`, `traces`, `exceptions`, `issues`, `security`, `audit`, `queries`, `external_calls`, `incidents`, `alert_history` |
| `--format` | `ndjson` (default), `json`, `csv` |
| `--query` | [Query language](query-language.md) filter |
| `--limit` | Maximum rows (default and maximum 50,000) |
| `--output` | File path; standard output if omitted |

Output is redacted. Newest records first.

## `observability_rebuild_fingerprints`

Recomputes SQL fingerprints and exception fingerprints from the stored data and regroups issues. Run it
after upgrading to a version whose release notes say the fingerprint rules changed.

```bash
python manage.py observability_rebuild_fingerprints
```

## Scheduling

Nothing needs scheduling: retention, rollups, alert evaluation and incident detection run from the
application's worker thread. If you prefer explicit jobs (Windows Task Scheduler, cron), schedule
`observability_cleanup` and, for an external liveness signal, `observability_health`.
