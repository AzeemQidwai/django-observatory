"""Request observability middleware (WSGI and ASGI, sync and async views).

Place it as early as possible in MIDDLEWARE so it times the whole stack. Every hook is
wrapped: an internal failure is reported on the internal logger and the request goes on.
"""
import hashlib
import time

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.core.signals import got_request_exception
from django.urls import NoReverseMatch, reverse
from django.utils.functional import LazyObject, empty

from . import conf, context, internal, metrics, pipeline, redaction, security
from .instrumentation import otel
from .instrumentation.exceptions import capture_exception
from .tracing import finish_trace, keep_decision

_ui_prefix = None


def ui_prefix():
    """URL prefix where the observability UI is mounted ('' when it is not mounted)."""
    global _ui_prefix
    if _ui_prefix is None:
        try:
            _ui_prefix = reverse("django_observatory:dashboard")
        except NoReverseMatch:
            _ui_prefix = ""
    return _ui_prefix


def _resolved_user(request):
    """The user only if something already loaded it: we never trigger a session/DB hit."""
    user = getattr(request, "_cached_user", None)
    if user is None:
        user = request.__dict__.get("user")
        if isinstance(user, LazyObject) and user._wrapped is empty:
            return None
    return user if user is not None and getattr(user, "is_authenticated", False) else None


def _client_ip(request):
    if conf.get("REQUESTS", "TRUST_PROXY_HEADERS"):
        fwd = request.META.get("HTTP_X_FORWARDED_FOR")
        if fwd:
            return fwd.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR", "") or ""


def _view_identity(view_func):
    target = getattr(view_func, "view_class", None) or getattr(view_func, "cls", None) or view_func
    return getattr(target, "__qualname__", type(target).__name__), getattr(target, "__module__", "")


class ObservabilityMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    # -- entry points ---------------------------------------------------------
    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        state = self._begin(request)
        if state is None:
            return self.get_response(request)
        try:
            response = self.get_response(request)
        except BaseException as exc:
            self._end(request, state, None, exc)
            raise
        return self._end(request, state, response)

    async def __acall__(self, request):
        state = self._begin(request)
        if state is None:
            return await self.get_response(request)
        try:
            response = await self.get_response(request)
        except BaseException as exc:
            self._end(request, state, None, exc)
            raise
        return self._end(request, state, response)

    def process_view(self, request, view_func, view_args, view_kwargs):
        try:
            ctx = context.current()
            if ctx is not None:
                name, module = _view_identity(view_func)
                ctx.view = (name, module, time.perf_counter())
                match = request.resolver_match
                if match is not None:
                    ctx.route = "/" + (match.route or "").lstrip("^/").rstrip("$")
        except Exception as exc:  # noqa: BLE001
            internal.warn("middleware.process_view", "failed", exc)
        return None

    # -- lifecycle ------------------------------------------------------------
    def _begin(self, request):
        """Returns None (not observed) or ("suppress", token) or ("trace", ctx, tokens)."""
        try:
            if not conf.enabled("REQUESTS"):
                return None
            path = request.path
            prefix = ui_prefix()
            if prefix and path.startswith(prefix):
                # the observability UI must not observe itself
                return ("suppress", context._suppress.set(True))
            if path.startswith(tuple(conf.get("REQUESTS", "IGNORE_PATHS"))):
                return None
            meta = request.META
            trace_id, parent = context.parse_traceparent(meta.get("HTTP_TRACEPARENT"))
            if trace_id is None:
                trace_id, parent = otel.active_ids()
            rid = None
            if conf.get("REQUESTS", "TRUST_REQUEST_ID_HEADER"):
                rid = context.safe_request_id(meta.get("HTTP_X_REQUEST_ID"))
            ctx = context.TraceContext(path, "request", trace_id, parent or "", rid)
            ctx.method = request.method or ""
            ctx.ip = _client_ip(request)
            ctx.crumb("request", f"{ctx.method} {path}")
            request.observability = ctx  # request.observability.request_id for application code
            return ("trace", ctx, context.activate(ctx))
        except Exception as exc:  # noqa: BLE001
            internal.warn("middleware.begin", "failed", exc)
            return None

    def _end(self, request, state, response, exc=None):
        if state[0] == "suppress":
            context._suppress.reset(state[1])
            return response
        _, ctx, tokens = state
        t0 = time.perf_counter()
        try:
            self._finish(request, ctx, response, exc)
        except Exception as e:  # noqa: BLE001
            internal.warn("middleware.end", "failed", e)
        finally:
            context.deactivate(tokens)
            # our own synchronous cost per request, visible on the Health page
            metrics.histogram("obs.overhead", (time.perf_counter() - t0) * 1000, unit="ms")
        return response

    def _finish(self, request, ctx, response, exc):
        t_end = time.perf_counter()
        duration = (t_end - ctx.start) * 1000
        status = response.status_code if response is not None else 500
        if exc is not None:
            ctx.error = True
            capture_exception(exc, route=ctx.route, method=ctx.method)
        if status >= 500:
            ctx.error = True
        route = ctx.route or "(unmatched)"
        method = ctx.method

        user = _resolved_user(request)
        if user is not None:
            ctx.user_id, ctx.username = str(user.pk), user.get_username()
        session = getattr(request, "session", None)
        key = getattr(session, "session_key", None)
        if key:
            ctx.session = hashlib.sha256(key.encode()).hexdigest()[:16]

        # Aggregates are recorded for every request, sampled or not.
        metrics.increment("http.requests", labels={"route": route, "method": method, "status": f"{status // 100}xx"})
        metrics.histogram("http.duration", duration, {"route": route}, "ms")

        if status == 403 and not ctx.security_recorded:
            security.record("PERMISSION_DENIED", resource=request.path, reason="HTTP 403", request=request)

        if response is not None and conf.get("REQUESTS", "RESPONSE_HEADERS"):
            response["X-Request-ID"] = ctx.request_id
            response["X-Trace-ID"] = ctx.trace_id

        # Everything below is serialisation (SQL normalisation, redaction, row building):
        # hand it to the worker so the response is not held up by it.
        cfg = conf.get("REQUESTS")
        snap = {
            "t_end": t_end, "duration": duration, "status": status, "route": route, "method": method,
            "path": request.path, "meta": request.META, "keep": keep_decision(ctx.error, duration),
            "query": request.GET.dict() if cfg["CAPTURE_QUERY_PARAMS"] else None,
            "body": self._body(request, cfg["MAX_BODY_BYTES"])
            if cfg["CAPTURE_BODY"] and method in ("POST", "PUT", "PATCH") else None,
            "content_type": response.get("Content-Type", "") if response is not None else "",
            "size": None, "headers": cfg["CAPTURE_HEADERS"],
        }
        if response is not None and not getattr(response, "streaming", False):
            try:
                snap["size"] = len(response.content)
            except Exception:  # noqa: BLE001
                pass
        pipeline.enqueue("deferred", lambda: _persist(ctx, snap), high=ctx.error)

    @staticmethod
    def _body(request, limit):
        """Opt-in only. Form fields are redacted by key; other payloads by pattern."""
        try:
            ctype = request.META.get("CONTENT_TYPE", "")
            if ctype.startswith("multipart/"):
                return "[multipart body not captured]"
            if ctype.startswith("application/x-www-form-urlencoded"):
                return redaction.clean(request.POST.dict())
            return redaction.text(request.body[:limit].decode("utf-8", "replace"), limit)
        except Exception:  # noqa: BLE001 - body already consumed as a stream
            return "[unavailable]"


def _persist(ctx, snap):
    """Runs in the pipeline worker (or inline in SYNC mode): build and enqueue the rows."""
    duration, status, route, method, meta = snap["duration"], snap["status"], snap["route"], snap["method"], snap["meta"]
    if ctx.view is not None:
        name, module, started = ctx.view
        ctx.add_span({
            "span_id": context.new_span_id(), "parent_span_id": ctx.root_span_id, "name": f"view {name}",
            "kind": "view", "offset_ms": (started - ctx.start) * 1000,
            "duration_ms": (snap["t_end"] - started) * 1000,
            "status": "error" if ctx.error else "ok", "attributes": {"view.module": module},
        })
    root_attrs = {"http.method": method, "http.route": route, "http.status_code": status}
    if not finish_trace(ctx, f"{method} {route}", duration, keep=snap["keep"], route=route, root_attributes=root_attrs):
        return
    metadata = dict(ctx.extra)
    if snap["body"] is not None:
        metadata["body"] = snap["body"]
    view_name, view_module = (ctx.view[0], ctx.view[1]) if ctx.view else ("", "")
    pipeline.enqueue("request", {
        "timestamp": context.to_dt(ctx.start_wall), "method": method, "path": redaction.text(snap["path"], 500),
        "route": route, "query_params": redaction.clean(snap["query"]) if snap["query"] is not None else {},
        "status_code": status, "duration_ms": duration, "user_id": ctx.user_id, "username": ctx.username,
        "session_id": ctx.session, "ip_address": ctx.ip, "user_agent": meta.get("HTTP_USER_AGENT", "")[:300],
        "referrer": redaction.text(meta.get("HTTP_REFERER", "").split("?")[0], 500),
        "content_type": snap["content_type"][:100], "response_size": snap["size"],
        "view_name": view_name, "view_module": view_module, "exception_uid": ctx.exception_uid,
        "db_count": ctx.db_count, "db_ms": ctx.db_ms, "slow_queries": ctx.slow_queries,
        "ext_count": ctx.ext_count, "ext_ms": ctx.ext_ms,
        "headers": redaction.headers(meta) if snap["headers"] else {},
        "metadata": redaction.clean(metadata), "request_id": ctx.request_id, "trace_id": ctx.trace_id,
        "span_id": ctx.root_span_id, **conf.dims(),
    }, high=ctx.error)


def _on_request_exception(sender, request=None, **kwargs):
    """Django turned an unhandled view exception into a 500: capture it with its context."""
    import sys

    try:
        exc = sys.exc_info()[1]
        ctx = context.current()
        if exc is not None and ctx is not None:
            ctx.error = True
            capture_exception(exc, route=ctx.route, method=ctx.method)
    except Exception as e:  # noqa: BLE001
        internal.warn("middleware.exception", "failed", e)


got_request_exception.connect(_on_request_exception, dispatch_uid="django_observatory.exc")
