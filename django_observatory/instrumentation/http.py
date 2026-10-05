"""Outbound HTTP monitoring for urllib (stdlib), requests and httpx when installed.

Stored: service, host, method, path, status, timing, error class. Never stored:
query strings, headers, bodies.
"""
import functools
import time
import urllib.request
from urllib.parse import urlsplit

from .. import conf, context, internal, metrics, pipeline, redaction

_installed = False


def service_for(host):
    services = conf.get("EXTERNAL_HTTP", "SERVICES") or {}
    if host in services:
        return services[host]
    bare = host.rsplit(":", 1)[0]
    for pattern, name in services.items():
        if bare == pattern or bare.endswith("." + pattern.lstrip(".")):
            return name
    return host


def _active():
    return not context.is_suppressed() and conf.enabled("EXTERNAL_HTTP")


def traceparent():
    """W3C header value for the current span, or None outside a trace."""
    rid, tid, sid = context.ids()
    return f"00-{tid}-{sid or context.new_span_id()}-01" if tid else None


def _is_timeout(exc):
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()


def record(method, url, t_wall, duration, status=None, exc=None, size=None):
    try:
        parts = urlsplit(str(url))
        host = parts.netloc.rsplit("@", 1)[-1]
        service = service_for(host)
        if status is None and exc is not None:
            status = getattr(exc, "code", None) if isinstance(getattr(exc, "code", None), int) else None
            if status is not None:  # urllib raises HTTPError for 4xx/5xx: that is a response, not a failure
                exc = None
        error = exc is not None or (status is not None and status >= 500)
        timeout = exc is not None and _is_timeout(exc)
        metrics.increment("ext.calls", labels={"service": service, "outcome": "error" if error else "ok"})
        metrics.histogram("ext.duration", duration, {"service": service}, "ms")
        row = {
            "service": service, "host": host, "method": method.upper(), "path": redaction.text(parts.path, 500),
            "status_code": status, "duration_ms": duration, "error": error, "timeout": timeout,
            "exc_type": type(exc).__name__ if exc is not None else "", "response_size": size,
        }
        ctx = context.current()
        if ctx is None:
            pipeline.enqueue("external", {**row, "timestamp": context.to_dt(t_wall)}, high=error)
            return
        span_id = context.new_span_id()
        offset = (t_wall - ctx.start_wall) * 1000
        ctx.ext_count += 1
        ctx.ext_ms += duration
        ctx.crumb("http", f"{method.upper()} {service}{parts.path} -> {'timeout' if timeout else status or row['exc_type']}"
                  f" ({duration:.0f}ms)", "error" if error else "info")
        ctx.externals.append({**row, "span_id": span_id, "offset_ms": offset})
        ctx.add_span({
            "span_id": span_id, "parent_span_id": context.current_span_id(), "name": f"{method.upper()} {service}",
            "kind": "http", "offset_ms": offset, "duration_ms": duration, "status": "error" if error else "ok",
            "attributes": {"http.host": host, "http.path": row["path"], "http.status_code": status,
                           "service": service, "timeout": timeout, "error": row["exc_type"]},
        })
    except Exception as e:  # noqa: BLE001
        internal.warn("http", "external call instrumentation failed", e)


def _size(headers):
    try:
        return int(headers.get("content-length"))
    except Exception:  # noqa: BLE001
        return None


def _wrap_sync(orig, describe):
    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if not _active():
            return orig(self, *args, **kwargs)
        req = args[0] if args else (kwargs.get("request") if "request" in kwargs else kwargs.get("fullurl"))
        try:
            method, url = describe(req, args[1:] if args else (), kwargs)
        except Exception:  # noqa: BLE001
            method, url = "GET", str(req or "")
        t_wall, t0 = time.time(), time.perf_counter()
        try:
            resp = orig(self, *args, **kwargs)
        except Exception as exc:
            record(method, url, t_wall, (time.perf_counter() - t0) * 1000, exc=exc)
            raise
        status = next((v for v in (getattr(resp, "status_code", None), getattr(resp, "status", None))
                       if isinstance(v, int)), None)
        record(method, url, t_wall, (time.perf_counter() - t0) * 1000, status, size=_size(getattr(resp, "headers", {})))
        return resp
    return wrapper


def _wrap_async(orig, describe):
    @functools.wraps(orig)
    async def wrapper(self, *args, **kwargs):
        if not _active():
            return await orig(self, *args, **kwargs)
        req = args[0] if args else (kwargs.get("request") if "request" in kwargs else kwargs.get("fullurl"))
        try:
            method, url = describe(req, args[1:] if args else (), kwargs)
        except Exception:  # noqa: BLE001
            method, url = "GET", str(req or "")
        t_wall, t0 = time.time(), time.perf_counter()
        try:
            resp = await orig(self, *args, **kwargs)
        except Exception as exc:
            record(method, url, t_wall, (time.perf_counter() - t0) * 1000, exc=exc)
            raise
        status = next((v for v in (getattr(resp, "status_code", None), getattr(resp, "status", None))
                       if isinstance(v, int)), None)
        record(method, url, t_wall, (time.perf_counter() - t0) * 1000, status, size=_size(getattr(resp, "headers", {})))
        return resp
    return wrapper


def _describe_prepared(request, args, kwargs):  # requests.PreparedRequest / httpx.Request
    tp = traceparent()
    if tp:
        try:
            request.headers.setdefault("traceparent", tp)
        except Exception:  # noqa: BLE001
            pass
    return request.method or "GET", request.url


def _describe_urllib(fullurl, args, kwargs):
    if isinstance(fullurl, urllib.request.Request):
        tp = traceparent()
        if tp and not fullurl.has_header("Traceparent"):
            fullurl.add_header("traceparent", tp)
        return fullurl.get_method(), fullurl.full_url
    data = args[0] if args else kwargs.get("data")
    return ("POST" if data is not None else "GET"), fullurl


def install():
    """Idempotent. Libraries that are not installed are simply skipped."""
    global _installed
    if _installed:
        return
    _installed = True
    opener = urllib.request.OpenerDirector
    opener.open = _wrap_sync(opener.open, _describe_urllib)
    try:
        import requests

        requests.Session.send = _wrap_sync(requests.Session.send, _describe_prepared)
    except ImportError:
        pass
    try:
        import httpx

        httpx.Client.send = _wrap_sync(httpx.Client.send, _describe_prepared)
        httpx.AsyncClient.send = _wrap_async(httpx.AsyncClient.send, _describe_prepared)
    except ImportError:
        pass
