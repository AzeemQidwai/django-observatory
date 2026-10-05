# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## Unreleased

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
