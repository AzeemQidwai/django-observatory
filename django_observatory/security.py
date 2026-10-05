"""Security event API (stored separately from application logs).

    from django_observatory import security
    security.record(event="PERMISSION_DENIED", resource="/api/materials/", reason="Missing permission")
"""
import random
import time

from . import conf, context, internal, metrics, pipeline, redaction

EVENTS = ("LOGIN_SUCCESS", "LOGIN_FAILURE", "LOGOUT", "PASSWORD_CHANGE", "PASSWORD_RESET",
          "PERMISSION_DENIED", "CSRF_FAILURE", "SUPERUSER_LOGIN", "SESSION_CREATED", "SESSION_REVOKED")
_SEVERITY = {"LOGIN_FAILURE": "warning", "PERMISSION_DENIED": "warning", "CSRF_FAILURE": "warning",
             "SUPERUSER_LOGIN": "notice", "PASSWORD_CHANGE": "notice", "PASSWORD_RESET": "notice"}


@internal.safe
def record(event, *, resource="", reason="", user=None, username="", ip="", severity=None, request=None, **metadata):
    if not conf.enabled("SECURITY") or context.is_suppressed():
        return
    event = str(event).upper()
    metrics.increment("security.events", labels={"event": event})
    rate = conf.get("SAMPLING", "security", default=1.0)
    if rate < 1.0 and random.random() >= rate:
        return
    ctx = context.current()
    rid, tid, sid = context.ids()
    user_id = ctx.user_id if ctx else ""
    uname = username or (ctx.username if ctx else "")
    if user is not None and getattr(user, "pk", None) is not None:
        user_id, uname = str(user.pk), uname or user.get_username()
    agent = ""
    if request is not None:
        agent = request.META.get("HTTP_USER_AGENT", "")
        ip = ip or request.META.get("REMOTE_ADDR", "")
        resource = resource or request.path
    if ctx is not None:
        ctx.security_recorded = True
        ctx.crumb("security", f"{event} {resource}".strip(), "warning")
    pipeline.enqueue("security", {
        "timestamp": context.to_dt(time.time()), "event": event,
        "severity": severity or _SEVERITY.get(event, "info"), "user_id": user_id, "username": uname,
        "ip_address": ip or (ctx.ip if ctx else ""), "user_agent": agent,
        "resource": redaction.text(resource, 500), "reason": redaction.text(reason, 500),
        "metadata": redaction.clean(metadata), "request_id": rid, "trace_id": tid, "span_id": sid,
        **conf.dims(),
    }, high=True)
