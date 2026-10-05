# django-observatory

[![PyPI](https://img.shields.io/pypi/v/django-observatory.svg)](https://pypi.org/project/django-observatory/)
[![Python](https://img.shields.io/pypi/pyversions/django-observatory.svg)](https://pypi.org/project/django-observatory/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.2%20%7C%206.x-0C4B33.svg)](https://www.djangoproject.com/)
[![CI](https://github.com/AzeemQidwai/django-observatory/actions/workflows/ci.yml/badge.svg)](https://github.com/AzeemQidwai/django-observatory/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Production observability for Django without a separate observability infrastructure.**

`pip install`, three settings, `migrate` — and `/observability/` is a complete operations console for your
application: logs, requests, traces, SQL, exceptions, metrics, server resources, security and audit events,
alerts, and incidents with a probable cause. Everything is stored in your own database and rendered by
Django. No Docker, Redis, Elasticsearch, Prometheus, Grafana, Sentry, agent, collector or cloud service.

It was built for places where the usual stack is not an option: Windows servers, air-gapped networks,
regulated environments where telemetry must not leave the organisation, and teams that are only allowed to
install Python packages. It stays compatible with the wider ecosystem through W3C Trace Context and OTLP
export.

![Dashboard](https://raw.githubusercontent.com/AzeemQidwai/django-observatory/main/docs/images/dashboard.png)

## Why

Most observability tools answer *what happened*. This one is designed to walk an operator from there to
*where*, *what caused it*, *who was affected* and *what to look at next* — inside the application it is
monitoring.

| | |
|---|---|
| **Zero infrastructure** | Python + Django + your existing database. One background thread per process. |
| **Offline first** | No outbound network calls, no CDN, no external assets. Works air-gapped. |
| **Django aware** | Request → view → template → ORM → SQL → external API, correlated automatically. |
| **Explains incidents** | Deterministic rules turn correlated signals into one incident with evidence. No AI. |
| **Private by default** | Bodies, cookies, auth headers and SQL parameters are never stored; secrets are redacted. |
| **Cannot take you down** | If observability fails, your application keeps serving. This is tested explicitly. |

## Features

- **Logs** – your existing `logging` calls are captured with no configuration; structured `observe.info(...)` API
- **Requests** – every request gets a request id and trace id (`X-Request-ID`, `X-Trace-ID` response headers)
- **Traces** – waterfall of view, templates, SQL and outbound HTTP; manual `span()` and `@task`
- **SQL** – normalised statements, fingerprints, slow-query detection, N+1 hints, drill-down to the request
- **External APIs** – `urllib`, `requests`, `httpx`: latency and failure rate per service
- **Exceptions and issues** – grouping by stable fingerprint, lifecycle with regression detection, breadcrumbs
- **Metrics** – counters, gauges, histograms, timers, percentiles; automatic rollups
- **Server resources** – CPU, memory and disk per host
- **Security events** – logins, failed logins, permission denied, CSRF failures, password changes
- **Audit trail** – append-only, hash-chained, tamper-evident, separately permissioned
- **Alerts** – rules with deduplication and cooldown; in-app, email, webhook
- **Incidents** – correlated detection with probable cause, confidence, impact and evidence
- **Causal analysis** – "Why is this slow?" / "Why did this fail?" on every request
- **Query language** – `status:>=500 AND duration:>1000 NOT path:/health*`
- **Exports and API** – CSV, JSON, NDJSON; read-only JSON API
- **OpenTelemetry** – W3C Trace Context in and out; OTLP/HTTP export without the SDK

## Quick start

```bash
pip install django-observatory
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django_observatory",
]

MIDDLEWARE = [
    "django_observatory.middleware.ObservabilityMiddleware",   # first, so it times the whole stack
    # ...
]
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    # ...
    path("observability/", include("django_observatory.urls")),
]
```

```bash
python manage.py migrate
python manage.py observability_test     # verifies every component in your environment
python manage.py runserver
```

Open <http://localhost:8000/observability/> as a staff user. That is the whole setup.

Optional extras: `pip install "django-observatory[process]"` adds `psutil` (CPU and memory readings on every
platform, per-process memory).

## Use it from your code

Nothing is required: requests, SQL, outbound HTTP, exceptions and `logging` are captured automatically.
The API is for what only you know.

```python
import logging
from django_observatory import observe, span, task, metrics, audit, security

logging.getLogger(__name__).info("material created", extra={"material_code": "A1023"})   # just works
observe.warning("stock_low", material_code="A1023", quantity=4)

with span("generate_monthly_report", report_type="monthly"):
    generate_report()

@task
def nightly_sync(): ...

metrics.increment("invoice.processed")
metrics.gauge("queue.depth", 42)
with metrics.timer("invoice.processing"):
    process_invoice()

audit.record(action="UPDATE", object_type="Material", object_id="A1023",
             changes={"quantity": {"before": 120, "after": 80}})
security.record(event="PERMISSION_DENIED", resource="/api/materials/", reason="Missing permission")
```

## From a symptom to a cause

A dependency slows down. Within a minute the dashboard shows one incident instead of a wall of alerts:

![Incident](https://raw.githubusercontent.com/AzeemQidwai/django-observatory/main/docs/images/incident.png)

The probable cause is an inference and is labelled as one, with its confidence. The evidence beneath it is
observed fact: the numbers before and after, the share of failing requests that call the service, the
exceptions raised. Opening any affected request shows where its time went:

![Request](https://raw.githubusercontent.com/AzeemQidwai/django-observatory/main/docs/images/request.png)

More screenshots: [endpoints](docs/images/performance.png) · [server](docs/images/server.png) ·
[logs](docs/images/logs.png) · [database, dark mode](docs/images/database-dark.png)

## Documentation

| Start here | Reference | Operate |
|---|---|---|
| [Getting started](docs/getting-started.md) | [Configuration](docs/configuration.md) | [Deployment](docs/deployment.md) |
| [User guide to the UI](docs/user-guide.md) | [Python API](docs/instrumentation.md) | [Alerts](docs/alerts.md) |
| [FAQ](docs/faq.md) | [Query language](docs/query-language.md) | [Incident correlation](docs/incident-correlation.md) |
| | [JSON API and exports](docs/api.md) | [Server monitoring](docs/server-monitoring.md) |
| | [Management commands](docs/commands.md) | [Security](docs/security.md) · [Privacy](docs/privacy.md) |
| | [Data model](docs/data-model.md) | [OpenTelemetry](docs/opentelemetry.md) |
| | [Architecture](docs/architecture.md) | [Troubleshooting](docs/troubleshooting.md) |

Full index: [docs/index.md](docs/index.md)

## Requirements

- Python 3.10 – 3.13
- Django 4.2, 5.2 or 6.x
- Any database Django supports. Tested on SQLite; designed for PostgreSQL and SQL Server (portable column
  types, no database-specific SQL)
- Windows, Linux or macOS; WSGI or ASGI

## Try the demo

```bash
git clone https://github.com/AzeemQidwai/django-observatory.git
cd django-observatory/example
pip install -e ..
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Browse `/api/materials/` a few times, open `/sap/degrade/?on=1` to make the simulated SAP dependency slow,
call `/api/materials/` again, and watch `/observability/` open an incident named after the cause.

## What this is not

It is not the first observability tool for Django, and it does not replace OpenTelemetry, or Sentry in
every scenario. Django Silk, Django Debug Toolbar, Sentry and the OpenTelemetry integrations are excellent
at what they do. This package combines Django-native instrumentation, local storage, security and audit
monitoring, and incident correlation into something you can deploy anywhere Django runs — and it exports
to those ecosystems when you outgrow it.

Scaling limits are documented honestly in [Architecture](docs/architecture.md#deliberate-limits) and
[Deployment](docs/deployment.md#sizing-and-retention).

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Security reports go through [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
