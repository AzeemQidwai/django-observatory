"""OpenTelemetry interoperability. Entirely optional.

- W3C Trace Context is always honoured/propagated (context.py / http.py), no dependency.
- If ``opentelemetry-api`` is installed and a span is active, its ids are adopted so our
  records line up with whatever the OTel SDK exports.
- With OTEL.enabled + OTEL.endpoint, finished traces are exported as OTLP/HTTP JSON
  (``<endpoint>/v1/traces``) using only the standard library: no SDK, no collector
  required locally. Works with the OTel Collector, Jaeger, Tempo, SigNoz, ...
"""
import json
import queue
import threading
import urllib.request

from .. import conf, context, internal

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # the normal case
    _otel_trace = None

_KIND = {"request": 2, "http": 3}  # SERVER, CLIENT; everything else INTERNAL (1)
_q = queue.Queue(maxsize=200)
_thread = None


def available():
    return _otel_trace is not None


def active_ids():
    """(trace_id, span_id) of the current OpenTelemetry span, or (None, None)."""
    if _otel_trace is None:
        return None, None
    try:
        sc = _otel_trace.get_current_span().get_span_context()
        if sc and sc.is_valid:
            return format(sc.trace_id, "032x"), format(sc.span_id, "016x")
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _attrs(d):
    out = []
    for k, v in (d or {}).items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": str(v)}
        out.append({"key": str(k), "value": val})
    return out


def to_otlp(ctx, name, duration_ms):
    """Build an OTLP ExportTraceServiceRequest (JSON encoding) for one finished trace."""
    start_ns = int(ctx.start_wall * 1e9)

    def span(span_id, parent, sname, kind, offset_ms, dur_ms, status, attributes):
        s = start_ns + int(offset_ms * 1e6)
        return {
            "traceId": ctx.trace_id, "spanId": span_id, "parentSpanId": parent or "", "name": sname,
            "kind": _KIND.get(kind, 1), "startTimeUnixNano": str(s), "endTimeUnixNano": str(s + int(dur_ms * 1e6)),
            "attributes": _attrs(attributes), "status": {"code": 2 if status == "error" else 1},
        }

    spans = [span(ctx.root_span_id, ctx.parent_span_id, name, ctx.kind, 0, duration_ms,
                  "error" if ctx.error else "ok", {"request.id": ctx.request_id, "enduser.id": ctx.user_id})]
    spans += [span(s["span_id"], s["parent_span_id"], s["name"], s["kind"], s["offset_ms"], s["duration_ms"],
                   s["status"], s["attributes"]) for s in ctx.spans]
    dims = conf.dims()
    return {"resourceSpans": [{
        "resource": {"attributes": _attrs({
            "service.name": dims["service"], "service.version": dims["release"],
            "deployment.environment": dims["environment"],
        })},
        "scopeSpans": [{"scope": {"name": "django_observatory"}, "spans": spans}],
    }]}


def _sender():
    context.suppress_thread()  # the export request itself must not be observed
    while True:
        payload = _q.get()
        try:
            cfg = conf.get("OTEL")
            req = urllib.request.Request(
                cfg["endpoint"].rstrip("/") + "/v1/traces", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", **(cfg.get("headers") or {})}, method="POST",
            )
            urllib.request.urlopen(req, timeout=cfg.get("timeout", 5)).close()  # noqa: S310
        except Exception as exc:  # noqa: BLE001
            internal.warn("otel.export", "OTLP export failed", exc, every=60)


def export_trace(ctx, name, duration_ms):
    global _thread
    cfg = conf.get("OTEL")
    if not (cfg.get("enabled") and cfg.get("endpoint")) or conf.get("PIPELINE", "SYNC"):
        return
    try:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_sender, name="django-observatory-otlp", daemon=True)
            _thread.start()
        _q.put_nowait(to_otlp(ctx, name, duration_ms))
    except queue.Full:
        pass  # exporter is behind: drop rather than hold memory or block
    except Exception as exc:  # noqa: BLE001
        internal.warn("otel.export", "could not queue trace", exc)
