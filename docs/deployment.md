# Deployment

No service, agent, scheduler or container is required. The package runs inside the Django process.

## Windows Server 2019 (IIS / Waitress)

1. `pip install django-observatory` (pure Python wheel; Django is the only dependency).
2. Add the app, middleware and URL include; run `python manage.py migrate`.
3. Start the site as usual, for example `waitress-serve --threads=8 project.wsgi:application`, or behind IIS
   with HttpPlatformHandler / wfastcgi.
4. `python manage.py observability_test`, then open `/observability/`.

Nothing here uses systemd, Unix sockets, `fork`-only APIs or shell scripts. Paths use `pathlib`. The UI
serves its own two asset files, so `collectstatic` and IIS static mappings are not needed for it.

Each worker process starts its own background thread on the first event. With IIS recycling or several
Waitress processes, cluster-wide jobs (alerts, incident detection, cleanup) still run once per interval.

## Database

| Database | Notes |
|---|---|
| SQLite | Fully supported. WAL mode is enabled on the storage database so writes do not block readers. |
| SQL Server | Supported through `mssql-django`. Only portable column types are used; strings are truncated to column size before insert. |
| PostgreSQL | Supported. No PostgreSQL-specific SQL is used. |

### Keeping telemetry out of the application database (recommended for busy sites)

```python
DATABASES = {
    "default": {...},
    "observability": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "observability.sqlite3"},
}
DATABASE_ROUTERS = ["django_observatory.routers.ObservabilityRouter"]
OBSERVABILITY = {"STORAGE": {"DATABASE_ALIAS": "observability"}}
```
```bash
python manage.py migrate --database observability
```

## Servers

Works under `runserver`, Waitress, IIS, gunicorn, uWSGI, Daphne and other ASGI servers. The middleware is
sync- and async-capable and state is carried in `contextvars`, never thread-locals.

uWSGI: start with `--enable-threads` (the worker is a thread). Pre-fork servers are handled: the worker
thread is started lazily per process, after the fork.

## Sizing and retention

Rough volume per request at 100% sampling: 1 request row, 1 trace row, 2 + N span rows, N query rows.
For sustained traffic set `SAMPLING.requests` to `0.1`–`0.25`; errors, slow requests and metrics are
unaffected. Retention is applied hourly in bounded batches; run `observability_cleanup` from Task Scheduler
only if you prefer an explicit schedule. Metrics are rolled up to hourly after 48 hours and daily after 30
days.

## Overhead

`python benchmarks/overhead.py` measures it on your hardware. Reference run (SQLite, request with two
queries): about +0.4 ms per request and +0.3 ms per log record. Serialisation (SQL normalisation, redaction,
row building) and all storage work run in the worker thread, off the request path.
The Health page shows the measured per-request cost in production.

## Upgrading / removing

Upgrades are ordinary Django migrations. To remove the package: delete the three settings entries and run
`python manage.py migrate django_observatory zero` beforehand to drop its tables.
