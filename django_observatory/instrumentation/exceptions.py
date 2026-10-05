"""Exception capture: stack, culprit, fingerprint, breadcrumbs. No local variables are
ever collected (they are the most common place for secrets to leak)."""
import linecache
import os
import random
import time
import traceback

from .. import conf, context, fingerprints, internal, metrics, pipeline, redaction

MAX_FRAMES = 60


def _frames(tb):
    frames = []
    for frame, lineno in traceback.walk_tb(tb):
        code = frame.f_code
        frames.append({
            "file": code.co_filename, "line": lineno, "function": code.co_name,
            "module": frame.f_globals.get("__name__", ""),
            "in_app": fingerprints.is_app_frame(code.co_filename),
            "code": redaction.text(linecache.getline(code.co_filename, lineno).strip(), 300),
        })
    return frames[-MAX_FRAMES:]


@internal.safe
def capture_exception(exc, handled=False, route="", method=""):
    """Record an exception once and return its uid (also when called again for the same object)."""
    uid = getattr(exc, "__obs_uid__", None)
    if uid:
        return uid
    if context.is_suppressed() or not conf.get("ENABLED"):
        return None
    exc_type = type(exc).__name__
    ignored = conf.get("EXCEPTIONS", "IGNORE")
    if ignored and any(c.__name__ in ignored for c in type(exc).__mro__):
        return None
    metrics.increment("exceptions", labels={"type": exc_type})
    uid = os.urandom(8).hex()
    try:
        exc.__obs_uid__ = uid  # dedupe: middleware + logging both see the same object
    except Exception:  # noqa: BLE001 - exceptions with __slots__
        pass
    ctx = context.current()
    if ctx is not None:
        ctx.exception_uid = ctx.exception_uid or uid
        ctx.crumb("exception", f"{exc_type}: {redaction.text(str(exc), 300)}", "error")
    rate = conf.get("SAMPLING", "exceptions", default=1.0)
    if rate < 1.0 and random.random() >= rate:
        return uid

    message = redaction.text(str(exc), 2000)
    frames = _frames(exc.__traceback__)
    culprit = fingerprints.culprit_frame(frames)
    route = route or (ctx.name if ctx is not None and ctx.kind != "request" else getattr(ctx, "route", "") or "")
    fp = fingerprints.exception_fingerprint(exc_type, message, frames, route)
    rid, tid, sid = context.ids()
    stack = redaction.text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), 20000)
    first_line = message.splitlines()[0] if message else ""
    pipeline.enqueue("exception", {
        "uid": uid, "timestamp": context.to_dt(time.time()), "exc_type": exc_type, "message": message,
        "stacktrace": stack, "frames": frames, "file_name": culprit.get("file", ""),
        "line_number": culprit.get("line"), "function": culprit.get("function", ""), "handled": handled,
        "user_id": ctx.user_id if ctx else "", "endpoint": route,
        "method": method or (getattr(ctx, "method", "") if ctx else ""),
        "breadcrumbs": list(ctx.breadcrumbs) if ctx else [], "fingerprint": fp,
        "request_id": rid, "trace_id": tid, "span_id": sid, **conf.dims(),
        "_issue": {
            "title": f"{exc_type}: {first_line}"[:300] if first_line else exc_type,
            "culprit": f"{culprit.get('module', '')}.{culprit.get('function', '')}".strip("."),
            "severity": "warning" if handled else "error",
        },
    }, high=True)
    return uid
