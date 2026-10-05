# OpenTelemetry

OpenTelemetry is optional. The package works identically without it.

| Capability | Requires |
|---|---|
| Accept incoming W3C `traceparent` (trace id + parent span) | nothing |
| Propagate `traceparent` on outbound `urllib` / `requests` / `httpx` calls | nothing |
| 32-hex trace ids and 16-hex span ids (OTel-compatible) | nothing |
| Export finished traces as OTLP/HTTP JSON | nothing (standard library) |
| Adopt the ids of an active OpenTelemetry span | `pip install django-observatory[otel]` |

## Exporting

```python
OBSERVABILITY = {"OTEL": {"enabled": True, "endpoint": "http://otel-collector:4318",
                          "headers": {"Authorization": "Bearer ..."}, "timeout": 5}}
```

Each kept trace is posted to `<endpoint>/v1/traces` from a background thread with a bounded queue. If the
collector is slow or down, traces are dropped from the export queue; the application and local storage are
unaffected. Any OTLP/HTTP receiver works: OpenTelemetry Collector, Jaeger, Grafana Tempo, SigNoz.

Resource attributes: `service.name`, `service.version` (release), `deployment.environment`. Span kinds:
request → SERVER, outbound HTTP → CLIENT, everything else INTERNAL.

## Running alongside the OpenTelemetry SDK

If `opentelemetry-instrumentation-django` is active, the middleware adopts the current span's trace id so
local records and exported spans share ids. Leave `OTEL.enabled` off in that case to avoid exporting twice.

## Not yet covered

OTLP export of metrics and logs, and gRPC transport. The event model already carries the needed fields.
