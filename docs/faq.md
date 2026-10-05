# FAQ

### Does it slow my application down?

Capture adds well under a millisecond to a typical request. The request thread only records raw facts and
puts them on an in-memory queue; SQL normalisation, redaction, row building and all database writes happen
in a background thread. The Health page shows the measured per-request cost in your environment, and
`benchmarks/overhead.py` measures it on your hardware.

### What happens if the telemetry database is down or slow?

Your application keeps working. Writes are retried a few times, then dropped and counted. If the queue
fills up, low-priority events (successful requests, info logs) are dropped first; errors, security and
audit events keep reserved space. Nothing blocks a request. This behaviour has explicit tests.

### Is my data sent anywhere?

No. There are no outbound calls, no telemetry "phone home", no external assets. The only network traffic
the package can make is what you configure: an alert webhook, alert email through your mail server, and
OTLP export.

### Do I need Redis, Celery, cron or Docker?

No. One daemon thread per process does the writing and the periodic work. With several processes or
servers, cluster-wide jobs are elected through a database row.

### Which databases are supported?

Anything Django supports. The package uses only portable column types and the ORM. It is developed and
tested on SQLite, and has been run against an application on SQL Server; PostgreSQL and SQL Server are
intended targets for the telemetry tables themselves. Please report anything database-specific.

### Should telemetry go in my application database?

For small and medium sites that is the simplest choice. For busy sites, or when you do not want to add
tables to the application database, use a separate database (a SQLite file is enough) with
`ObservabilityRouter`; see [deployment](deployment.md).

### How much disk space will it use?

It depends on traffic, sampling and retention; see [data model](data-model.md#volume). Start with the
defaults, look at **Settings & System** after a few days, then lower `SAMPLING.requests` or retention if
needed.

### Can I use it with several servers or processes?

Yes. All of them write to the same telemetry database; records carry the host name. Alerts and incidents
are evaluated once. The pipeline counters on the Health page are per process.

### Does it work with ASGI and async views?

Yes. The middleware is sync- and async-capable and context is carried in `contextvars`.

### Does it work with Django REST Framework?

Yes. DRF views are ordinary Django views: requests, SQL, exceptions and the authenticated user are
captured. There is no DRF-specific serializer instrumentation yet.

### Does it work with Celery or other task queues?

Decorate task functions with `@task` (below the queue's own decorator) to trace them and capture their
exceptions. There is no automatic Celery integration yet. Note that a worker process must be able to reach
the telemetry database.

### I use Sentry / OpenTelemetry already. Is this a replacement?

Not necessarily. It can run alongside them. It honours and propagates W3C Trace Context, adopts the ids of
an active OpenTelemetry span, and can export traces over OTLP. Its particular value is where those systems
cannot be deployed, and in having logs, traces, SQL, security, audit and incidents in one place inside the
application.

### How accurate are percentiles?

They are estimated from histogram buckets (bounds at 1, 2, 5, 10, 25, 50, 100, 250, 500 ms, 1, 2.5, 5, 10,
30, 60 s) and interpolated within a bucket, like Prometheus histograms. Good enough to see that P95 moved
from 300 ms to 3 s; not exact to the millisecond. The per-statement P95 on the Database page is exact for
the heaviest statements, computed from stored rows.

### Why do some requests not appear in the Requests list?

Sampling below 1.0, an ignored path, or the observability UI's own URLs (never recorded). Failed and slow
requests are always kept.

### Is the incident "probable cause" reliable?

It is a rule-based inference from correlated measurements, and it is presented as one: with a confidence
level, the evidence it rests on, and the alternatives considered. When the evidence is weak it says
*"No single cause identified"* rather than guessing. Treat it as where to look first.

### Does it use AI?

No. Detection and correlation are deterministic rules and statistics, so they work offline and can be
explained. Incident records are structured and already redacted, so an optional local model could be added
on top later.

### Can staff users see passwords or tokens in the UI?

Secrets are redacted before they are stored, so they are not in the database to be shown. See
[privacy](privacy.md) for exactly what is and is not captured, and its limits.

### Can audit records be changed?

Not through the application: the model, the admin and the UI all refuse edits and deletes, and each record
is chained to the previous one by a hash, so tampering at the database level is detectable with *Verify
integrity*. Someone with direct write access to the database can still alter rows; the chain makes that
evident rather than impossible.

### How do I monitor more than one application?

Install the package in each. Either give each its own telemetry database, or point several at one database
and distinguish them by `SERVICE_NAME` (`service:billing-api` in any filter).

### Can I customise the UI?

The templates are ordinary Django templates under `django_observatory/`; a template of the same name in
your project's `templates/django_observatory/` directory overrides it. There is no theming API yet.

### How do I upgrade?

`pip install -U django-observatory`, then `python manage.py migrate` (with `--database <alias>` if you use
a separate telemetry database), then restart. Read the changelog for anything that needs
`observability_rebuild_fingerprints`.

### Why is the package called `django-observatory` when the UI says "Observability"?

`django-observability` was already taken on PyPI by an unrelated project. The settings dictionary
(`OBSERVABILITY`), the URL you choose and the management commands (`observability_*`) describe what the
package does; the import name is `django_observatory`.
