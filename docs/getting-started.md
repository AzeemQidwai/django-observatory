# Getting started

## 1. Install

```bash
pip install django-observatory
```

On a server without internet access, copy the wheel file and install it directly; Django is the only
dependency:

```bash
pip install django_observatory-0.1.0-py3-none-any.whl
```

Optional extras:

| Extra | Adds | Install when |
|---|---|---|
| `django-observatory[process]` | `psutil` | you want CPU and memory on any platform, and per-process memory |
| `django-observatory[otel]` | `opentelemetry-api` | the OpenTelemetry SDK also runs in this process |
| `django-observatory[all]` | both | |

## 2. Add three things

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # your apps ...
    "django_observatory",
]

MIDDLEWARE = [
    "django_observatory.middleware.ObservabilityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
```

Place the middleware first. It then measures the whole stack, including other middleware. It reads the
user at the end of the request, so it does not need to come after `AuthenticationMiddleware`.

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("observability/", include("django_observatory.urls")),
]
```

You can mount it under any prefix (`path("ops/", include(...))`).

`django.contrib.auth`, `contenttypes` and `sessions` must be installed, as in any standard project. The
login page used for anonymous visitors is your project's `LOGIN_URL`.

## 3. Create the tables and verify

```bash
python manage.py migrate
python manage.py check                 # validates the configuration
python manage.py observability_test    # exercises every component, then cleans up
```

Expected output:

```text
Django Observability Self Test

[PASS] Configuration
[PASS] Logging + storage + queue
[PASS] Redaction
...
[PASS] OpenTelemetry adapter

Result: PASS
```

## 4. Open the console

```bash
python manage.py runserver
```

Sign in as a staff user and open <http://localhost:8000/observability/>. Use the application for a moment
and the dashboard fills in. Nothing else needs configuring: logging is attached automatically, SQL and
outbound HTTP are instrumented, exceptions become issues.

## 5. Name your service (recommended)

```python
OBSERVABILITY = {
    "SERVICE_NAME": "project-api",
    "ENVIRONMENT": "production",       # development | testing | staging | production
    "RELEASE": "2026.10.05.1",         # enables release comparison and deployment correlation
}
```

## A ten-minute tour

1. **Dashboard** — request volume by status, error rate, latency percentiles, exceptions, database and
   external latency, log volume, response-time distribution, slowest and busiest endpoints. Hover any chart.
   The time range is top right; *Live* refreshes every five seconds.
2. **Requests** — type `status:>=500` in the filter box, or click a chip. Open a request: status, duration,
   a *latency breakdown* (external services / database / application code), the *timeline* waterfall, the
   SQL it ran, its logs and its headers with sensitive values redacted.
3. **Why did this fail?** — on a failed or slow request the page states the suspected cause, its confidence,
   and the observed evidence separately.
4. **Issues** — exceptions grouped by fingerprint. Open one to see the trend, affected users and endpoints,
   and the latest stack trace. Resolve it; if it happens again it is marked *REGRESSED*. *Copy traceback* puts
   the full trace on the clipboard.
5. **Endpoints / Database / External APIs** — where time goes, per route, per SQL statement, per service.
6. **Server** — CPU, memory and disk of the machine.
7. **Alerts** — eight rules exist by default. Add your own.
8. **Incidents** — when signals correlate, one incident is opened with a probable cause.
9. **Health** — whether the observability pipeline itself is healthy and what it costs per request.

The [user guide](user-guide.md) covers each page in detail.

## Add your own signals

```python
from django_observatory import observe, span, metrics, audit

observe.info("order_placed", order_id=order.pk, total=str(order.total))

with span("price_basket", items=len(basket)):
    total = price(basket)

metrics.increment("orders.placed")

audit.record("UPDATE", "Order", order.pk, changes={"status": {"before": "NEW", "after": "PAID"}})
```

See the [Python API](instrumentation.md).

## Production checklist

- [ ] `SERVICE_NAME`, `ENVIRONMENT`, `RELEASE` set
- [ ] Decide where telemetry lives: the application database, or [a separate one](deployment.md#keeping-telemetry-out-of-the-application-database-recommended-for-busy-sites)
- [ ] Busy site? Set `SAMPLING.requests` to `0.1`–`0.25` (errors, slow requests and dashboards are unaffected)
- [ ] Polling or health-check URLs added to `REQUESTS.IGNORE_PATHS`
- [ ] `EXTERNAL_HTTP.SERVICES` maps hostnames to friendly names
- [ ] `ALERTS.EMAIL_TO` or `ALERTS.WEBHOOK_URL` set, and a working Django email backend
- [ ] Review who is staff; grant `view_audit_events` and `manage_observability_settings` deliberately
- [ ] Behind a reverse proxy: `REQUESTS.TRUST_PROXY_HEADERS = True`
- [ ] Domain-specific identifiers added to `REDACTION`
- [ ] `psutil` installed on Windows for CPU and memory readings

## Removing it

```bash
python manage.py migrate django_observatory zero
```

then remove the app, the middleware and the URL include.
