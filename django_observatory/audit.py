"""Audit trail API. Records are append-only and hash-chained (see models.AuditEvent).

    from django_observatory import audit
    audit.record(action="UPDATE", object_type="Material", object_id="A1023",
                 changes={"quantity": {"before": 120, "after": 80}})
"""
import random
import time

from . import conf, context, internal, metrics, pipeline, redaction


@internal.safe
def record(action, object_type="", object_id="", *, changes=None, before=None, after=None,
           result="SUCCESS", reason="", user=None, username="", **metadata):
    if not conf.enabled("AUDIT") or context.is_suppressed():
        return
    rate = conf.get("SAMPLING", "audit", default=1.0)
    if rate < 1.0 and random.random() >= rate:
        return
    ctx = context.current()
    rid, tid, sid = context.ids()
    user_id = ctx.user_id if ctx else ""
    uname = username or (ctx.username if ctx else "")
    if user is not None and getattr(user, "pk", None) is not None:
        user_id, uname = str(user.pk), uname or user.get_username()
    if changes and before is None and after is None:
        # derive before/after from {"field": {"before": x, "after": y}}
        pairs = {k: v for k, v in changes.items() if isinstance(v, dict) and {"before", "after"} <= set(v)}
        if pairs:
            before = {k: v["before"] for k, v in pairs.items()}
            after = {k: v["after"] for k, v in pairs.items()}
    action = str(action).upper()
    metrics.increment("audit.events", labels={"action": action})
    if ctx is not None:
        ctx.crumb("audit", f"{action} {object_type} #{object_id}")
    clean = redaction.clean
    pipeline.enqueue("audit", {
        "timestamp": context.to_dt(time.time()), "user_id": user_id, "username": uname, "action": action,
        "object_type": str(object_type), "object_id": str(object_id),
        "before": clean(before) if before is not None else None,
        "after": clean(after) if after is not None else None,
        "changes": clean(changes) if changes is not None else None,
        "ip_address": ctx.ip if ctx else "", "result": str(result).upper(),
        "reason": redaction.text(reason, 500), "metadata": clean(metadata),
        "request_id": rid, "trace_id": tid, "span_id": sid,
    }, high=True)
