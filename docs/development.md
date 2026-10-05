# Development

## Layout

```text
django_observatory/
  __init__.py            public API, imported lazily
  apps.py                start-up: instrumentation, signals, logging attachment
  conf.py                settings merge, runtime overrides
  context.py             contextvars trace context, id generation, W3C parsing
  redaction.py           the redaction engine
  fingerprints.py        SQL normalisation, exception and log fingerprints
  pipeline.py            queue, worker thread, retries, back-pressure
  scheduler.py           periodic jobs, cluster election by lease
  storage/               ObservabilityBackend contract + Django ORM backend
  models.py              all tables
  middleware.py          request lifecycle
  logging.py             ObservabilityHandler, emit_event
  observe.py audit.py security.py metrics.py tracing.py system.py     the public API modules
  instrumentation/       database.py http.py exceptions.py otel.py + template spans
  alerts.py              rules, measurement, notification channels
  correlation/           engine.py (detection) rules.py (hypotheses) causal.py graph.py
  search.py              query language
  tables.py              declarative list pages (columns, fields, charts)
  views.py urls.py       UI and JSON API
  exporters.py health.py maintenance.py checks.py permissions.py routers.py
  templates/django_observatory/    server-rendered pages and partials
  static/django_observatory/       app.css, app.js (no build step, no dependencies)
  management/commands/
  migrations/
tests/                   Django test runner; tests/settings.py, tests/urls.py
example/                 demo project with a simulated external dependency
benchmarks/overhead.py
docs/
```

## Running the tests

```bash
pip install -e ".[all]"
python -m django test tests --settings=tests.settings
python -m django test tests.test_capture.FailureIsolationTests --settings=tests.settings   # one class
```

| File | Covers |
|---|---|
| `test_core.py` | redaction, fingerprints, configuration and checks, query language |
| `test_capture.py` | logging, requests, tracing, SQL, exceptions, outbound HTTP, security, audit, metrics, failure isolation, the real worker pipeline, OpenTelemetry |
| `test_ui.py` | permissions, every page, charts, API, exports, XSS / CSRF / traversal / injection |
| `test_intelligence.py` | incidents, correlation rules, causal analysis, alerts, server resources, retention, commands, scheduler |

Tests use the synchronous pipeline for determinism. Incident tests build synthetic telemetry at chosen
times with the `Scenario` helper in `test_intelligence.py`.

CI runs the suite on Python 3.10–3.13 with Django 4.2, 5.2 and 6.0, with optional dependencies installed,
on Windows, and installs the built wheel into a fresh project.

## Principles to keep

See the ground rules in [CONTRIBUTING.md](../CONTRIBUTING.md). In code terms:

- Application-thread code must be cheap and wrapped. Use `internal.safe` or `try/except` with
  `internal.warn(...)`; never let an exception reach the caller, and never log through the normal logging
  tree from inside the package (that is what `django_observatory.internal` is for).
- Expensive work belongs in the worker: enqueue raw data, or a `"deferred"` callable.
- Anything stored goes through `redaction.clean` / `redaction.text`.
- Record built-in metrics for every operation, before any sampling decision.
- Deliberate simplifications are marked with a `ponytail:` comment naming the ceiling and the upgrade path.

## Extending

### An incident rule

```python
from django_observatory.correlation import rules

def rule_queue_backlog(s, kinds, signals, affected):
    """s: engine.Snapshot; kinds: {symptom kinds}; signals: symptom dicts; affected: failing/slow requests."""
    depth = ...                                  # read your own metric for the window
    if depth < 1000:
        return None
    h = rules.Hypothesis("queue", "orders", "Order queue backlog", "Queue consumers stalled",
                         "Check the consumer service and broker.")
    h.add(0.5, f"queue.depth is {depth}")        # evidence: points and an observed fact
    if "request_latency" in kinds:
        h.add(0.2, "Request latency degraded in the same window")
    return h

rules.RULES = (*rules.RULES[:-1], rule_queue_backlog, rules.RULES[-1])   # keep the fallback last
```

Register it in an `AppConfig.ready()`. Scores: ≥ 0.75 HIGH, ≥ 0.5 MEDIUM, otherwise LOW.

### A notification channel

`alerts.CHANNELS["name"] = fn(subject, body, payload)`; see [alerts](alerts.md#adding-a-channel).

### A storage backend

Subclass `django_observatory.storage.ObservabilityBackend` and implement `write(kind, rows)`,
`write_batch(grouped)`, `query(kind)`, `delete_before(retention_kind, timestamp, batch_size)` and
`get_statistics()`. Rows are plain, already-redacted dictionaries. Set `STORAGE.BACKEND` to its dotted
path. The built-in UI reads through the Django models, so a custom backend is typically a forwarding sink
that subclasses `DjangoORMBackend` and also ships batches elsewhere.

### A list page

Add a `Table(...)` to `tables.py`: model, permission, columns, query-language fields, and optionally a
volume chart. The URL, UI, API, export and search follow from the definition.

## Front end

No framework and no build step. `app.js` provides: the pointer-anchored tooltip, `lineChart`, `barChart`
(stacked, time or categorical), the relationship graph, live refresh and copy-to-clipboard. Charts are
described by JSON emitted with `json_script`:

```json
{"type": "bar", "unit": "ms", "labels": ["2026-10-05T06:00:00+00:00"], "categorical": false, "max": 100,
 "series": [{"name": "P95", "values": [480.0], "color": "critical"}]}
```

Colours are CSS variables defined for light and dark in `app.css`. Status colours (good, warning, serious,
critical) are reserved for state and always paired with a shape or label. Check both themes and a
420-pixel-wide viewport.

Asset URLs carry a version derived from the files' modification time, so browsers pick up changes at once.

## Releasing

1. Update `__version__` in `django_observatory/__init__.py` (the single source of the version).
2. Move the *Unreleased* entries in `CHANGELOG.md` under the new version and date.
3. Commit, then tag and push:

   ```bash
   git tag v0.2.0
   git push origin main v0.2.0
   ```

4. The **Release** workflow runs the tests, checks that the tag matches the version, builds the sdist and
   wheel, validates them, publishes to PyPI through Trusted Publishing, and creates the GitHub release.

To check a build locally:

```bash
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

### Versioning

Semantic versioning. While the version is below 1.0, minor releases may contain breaking changes; they are
called out in the changelog. Migrations are always provided for model changes, and settings keep working
across patch releases.
