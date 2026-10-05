# Troubleshooting

Start with `python manage.py observability_test` and the **Health** page (or `observability_health`).

| Symptom | Cause and fix |
|---|---|
| Dashboard empty | Middleware missing from `MIDDLEWARE` (`manage.py check` warns, W003), or `ENABLED` is False. |
| `no such table: django_observatory_…` | Run `python manage.py migrate` (with `--database <alias>` if you use a separate database). |
| 403 on `/observability/` | The user is not staff, or `PERMISSION_MODE="permissions"` without `view_observability`. |
| `logger.info()` calls missing | The logger's effective level is above INFO, or you configured the root logger at WARNING. Set its level or `LOGGING.LEVEL`. |
| Logs appear twice | The handler is attached both by you and to a parent logger. Keep one. |
| Some requests missing | `SAMPLING.requests` < 1 (errors and slow requests are always kept), or the path is in `REQUESTS.IGNORE_PATHS`. |
| No user on requests | The middleware only records a user that the request already loaded; anonymous or unauthenticated endpoints show none. |
| External calls not shown | Only `urllib`, `requests` and `httpx` are instrumented. Other clients can be wrapped with `span("call X", "http")`. |
| Queue "dropped" count rising | Storage is slower than the event rate: lower sampling, use a separate database, raise `PIPELINE.QUEUE_SIZE`. |
| `database is locked` (SQLite) | Keep `STORAGE.SQLITE_WAL` on, move telemetry to its own SQLite file, set `OPTIONS: {"timeout": 20}`. |
| Alerts or incidents never fire | `SCHEDULER.ENABLED` is False, there is too little traffic (`INCIDENTS.MIN_REQUESTS`), or the process is idle: the scheduler runs in the worker thread of a live process. |
| No email notifications | Set `ALERTS.EMAIL_TO` and a working Django `EMAIL_BACKEND`. |
| Client IP is the proxy | Set `REQUESTS.TRUST_PROXY_HEADERS = True` behind a trusted reverse proxy. |
| Messages `[django-observatory] WARNING …` on stderr | An internal failure was isolated. The application is unaffected; the message names the component. |

## Tests in your project

Use the synchronous pipeline for deterministic assertions:

```python
OBSERVABILITY = {"PIPELINE": {"SYNC": True}, "SCHEDULER": {"ENABLED": False}}
```
or disable it entirely with `{"ENABLED": False}`.
