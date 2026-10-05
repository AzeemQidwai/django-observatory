"""Per-request / per-task trace context carried in contextvars (never thread-locals),
so it is correct under WSGI threads and ASGI tasks alike."""
import contextlib
import datetime
import os
import re
import threading
import time
from collections import deque
from contextvars import ContextVar

from django.conf import settings

from . import conf

_ctx = ContextVar("obs_ctx", default=None)
_span = ContextVar("obs_span", default="")
_suppress = ContextVar("obs_suppress", default=False)

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]{8,64}$")


def new_trace_id():
    return os.urandom(16).hex()


def new_span_id():
    return os.urandom(8).hex()


def new_request_id():
    return "req_" + os.urandom(12).hex()


def to_dt(epoch):
    """Epoch seconds -> datetime consistent with the project's USE_TZ."""
    if settings.USE_TZ:
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(epoch)


def parse_traceparent(header):
    """W3C Trace Context -> (trace_id, parent_span_id) or (None, "")."""
    m = _TRACEPARENT.match((header or "").strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None, ""
    return m.group(1), m.group(2)


def safe_request_id(value):
    return value if value and _SAFE_ID.match(value) else None


class TraceContext:
    """Mutable state of one logical operation (request, task or manual root span)."""

    def __init__(self, name, kind="request", trace_id=None, parent_span_id="", request_id=None):
        self.name = name
        self.kind = kind
        self.request_id = request_id or new_request_id()
        self.trace_id = trace_id or new_trace_id()
        self.root_span_id = new_span_id()
        self.parent_span_id = parent_span_id
        self.start_wall = time.time()
        self.start = time.perf_counter()
        self.user_id = ""
        self.username = ""
        self.ip = ""
        self.session = ""
        self.extra = {}  # bind(tenant=...) values, merged into log metadata
        self.spans = []
        self.queries = []
        self.externals = []
        self.dropped_spans = 0
        self.db_count = 0
        self.db_ms = 0.0
        self.slow_queries = 0
        self.ext_count = 0
        self.ext_ms = 0.0
        self.exception_uid = ""
        self.error = False
        self.security_recorded = False
        self.route = ""
        self.method = ""
        self.view = None  # (name, module, perf_counter at view start)
        self.breadcrumbs = deque(maxlen=conf.get("BREADCRUMBS") or 30)
        self._lock = threading.Lock()

    def crumb(self, category, message, level="info"):
        from . import redaction

        clean_msg = redaction.text(message, 300)
        with self._lock:
            self.breadcrumbs.append(
                {"t": round(time.time(), 3), "category": category, "message": clean_msg, "level": level}
            )

    def add_span(self, span):
        with self._lock:
            if len(self.spans) < conf.get("TRACING", "MAX_SPANS_PER_TRACE"):
                self.spans.append(span)
            else:
                self.dropped_spans += 1


def current():
    return _ctx.get()


def activate(ctx):
    """Returns tokens to pass to deactivate()."""
    return _ctx.set(ctx), _span.set(ctx.root_span_id)


def deactivate(tokens):
    _ctx.reset(tokens[0])
    _span.reset(tokens[1])


def current_span_id():
    return _span.get()


def ids():
    """(request_id, trace_id, span_id) for enrichment; empty strings outside any trace."""
    c = _ctx.get()
    if c is None:
        return "", "", ""
    return c.request_id, c.trace_id, _span.get()


def bind(**values):
    """Attach values (e.g. tenant="acme") to everything recorded in the current trace."""
    c = _ctx.get()
    if c is not None:
        c.extra.update(values)


def is_suppressed():
    return _suppress.get()


@contextlib.contextmanager
def suppressed():
    """Nothing done inside is observed. Used for our own writes, UI and notifications."""
    token = _suppress.set(True)
    try:
        yield
    finally:
        _suppress.reset(token)


def suppress_thread():
    """Permanently suppress the current thread (worker thread)."""
    _suppress.set(True)
