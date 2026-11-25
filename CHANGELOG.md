# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## 0.1.5 - 2026-11-25

### Security
- Redaction: the key=value pattern could backtrack cubically, so a crafted few-KB request path or header
  pinned a worker's CPU for seconds to minutes. Its repeats are now bounded (16 KB worst case: ~0.1 s).
- URL paths are stored with credential-looking segments masked: long letter+digit runs such as webhook
  and bot tokens in outbound calls, and URL kwargs with sensitive names (e.g. the password-reset `<token>`)
  in request paths, breadcrumbs, referrers and security events.
- The audit hash chain is now an HMAC keyed with `SECRET_KEY` (`SECRET_KEY_FALLBACKS` honoured), so write
  access to the table alone can no longer forge rows that verify. Rows written before this release keep
  their unkeyed hash and still verify, but only before the first keyed row.
- `PERMISSION_MODE = "permissions"`: incident pages, the dashboard, request and trace detail no longer show
  requests, exceptions, SQL or security evidence to viewers without that section's permission. Exception
  breadcrumbs from the log, security, audit and request sections are hidden (page, API, export) from
  viewers who lack that section; audit breadcrumbs are hidden in the default mode too.
- Security incidents no longer name the source IP in `probable_cause`; it stays in the evidence list.
- Settings page: `ALERTS.WEBHOOK_URL`, the OTLP endpoint path and all OTLP header values are masked.
- Webhook alerts escape `&`, `<`, `>`, markdown links and `@everyone`/`@here`/`@channel`, so exception
  text cannot inject links or mass mentions into chat.
- 401/403 responses from the observability UI itself are recorded as `PERMISSION_DENIED` security events.

### Changed
- `REQUESTS.TRUST_PROXY_HEADERS` reads the client IP from the right of `X-Forwarded-For` (`True` = one
  trusted proxy, an integer = that many) and validates it; it used to take the leftmost, client-controlled
  entry. Set it to the number of proxies in front of Django (e.g. `2` for CDN + nginx).

### Fixed
- `migrate` on a fresh database (seen on SQL Server) printed `pipeline.write: dropped … rows` and
  `scheduler: tick failed … Invalid object name 'django_observatory_…'`: events logged before this app's
  tables existed were written anyway. Writes and scheduler jobs now wait quietly until the tables exist.

## 0.1.4 - 2026-11-21

### Added
- `PIPELINE.SHUTDOWN_TIMEOUT` (default 5 s): how long a stopping process waits for queued events to be
  written. Previously fixed at 5 s, so a large backlog was cut off even when the server allowed longer
- Deployment docs: what can be lost on graceful and forced worker shutdown

### Fixed
- Forked worker processes (gunicorn `--preload`, multiprocessing) now start with a fresh queue, lock and
  counters. Before, a child inherited the parent's counters until its first event and could deadlock on a
  queue lock held by the parent's writer at fork time

## 0.1.3 - 2026-10-16

### Fixed
- Projects with their own `403.html` returned a 500 instead of the 403 page: the template-timing wrapper
  rejected `render(context=...)` passed by keyword, which Django's `permission_denied` handler does

## 0.1.2 - 2026-10-16

### Added
- `REQUESTS.IGNORE_USER_AGENTS`: leave uptime monitors and load-balancer health checks out of the data

## 0.1.1 - 2026-10-16

### Added
- Health check *Clock*: reports records dated in the future and, on PostgreSQL, SQL Server, MySQL and Oracle,
  an application server clock that disagrees with the database server's by more than two minutes

### Fixed
- A Windows CPU-reading test could fail on fast machines (CPU counters advance in ~15 ms ticks)

### Maintenance
- CI uses `actions/checkout` v7 and `actions/setup-python` v7

## 0.1.0 - 2026-10-05

First public release.

### Capture
- Logging handler with automatic attachment; structured `observe` API
- Request middleware for WSGI and ASGI; request and trace ids; W3C Trace Context in and out
- Traces and spans (`span`, `task`); SQL monitoring with normalisation, fingerprints and slow-query detection
- Outbound HTTP monitoring for `urllib`, `requests` and `httpx`
- Exception capture, issue grouping and lifecycle (open, acknowledged, resolved, ignored, regressed), breadcrumbs
- Metrics: counters, gauges, histograms, timers; one-minute buckets rolled up to hourly and daily
- Server resources: CPU, memory and disk per host (psutil optional)
- Security events (logins, failures, permission denied, CSRF, password changes)
- Append-only, hash-chained audit trail; Django admin changes audited automatically

### Intelligence
- Alert rules with deduplication, cooldown, email and webhook channels; eight default rules
- Incident detection with rule-based correlation: external service, database, server resources, deployment,
  exception spike, security spike; facts and inference kept separate
- Per-request causal analysis ("Why is this slow?" / "Why did this fail?")
- Application Observability Graph

### Interface and operations
- Built-in UI (Django templates and vanilla JavaScript, light and dark, no external assets) and JSON API
- Query language, global search, CSV / JSON / NDJSON export
- Central redaction engine, sampling, per-kind retention, health page, system checks
- OTLP/HTTP trace export without the OpenTelemetry SDK
- Management commands: `observability_test`, `_health`, `_stats`, `_cleanup`, `_export`, `_rebuild_fingerprints`
- Optional separate telemetry database through `ObservabilityRouter`
