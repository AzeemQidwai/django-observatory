"""Declarative list pages. One definition drives the HTML table, the query-language
field whitelist, sorting, export and the JSON API for a resource."""
from django.urls import reverse

from .models import (
    Alert, AuditEvent, DatabaseQuery, ExceptionRecord, ExternalCall, Incident, Issue, ObservabilityEvent,
    RequestRecord, SecurityEvent, TraceRecord,
)
from .search import Field as F

_CORR = {"request_id": F("request_id"), "trace_id": F("trace_id")}
_DEPLOY = {"service": F("service"), "environment": F("environment"), "release": F("release")}
_TIME = {"timestamp": F("timestamp", "datetime")}


def tone(kind, value):
    """Semantic status class for a badge: good / warning / serious / critical / '' (neutral)."""
    v = str(value).upper()
    if kind == "status":
        try:
            code = int(value)
        except (TypeError, ValueError):
            return ""
        return "critical" if code >= 500 else "warning" if code >= 400 else "good" if code >= 200 else ""
    if kind == "level":
        return {"CRITICAL": "critical", "ERROR": "critical", "WARNING": "warning"}.get(v, "plain")
    return {
        "OPEN": "critical", "REGRESSED": "critical", "FIRING": "critical", "UNHEALTHY": "critical", "ERROR": "critical",
        "CRITICAL": "critical", "FAILURE": "critical", "FAILED": "critical", "DENIED": "critical",
        "HIGH": "serious", "ACKNOWLEDGED": "warning", "WARNING": "warning", "DEGRADED": "warning",
        "MEDIUM": "warning", "NOTICE": "warning",
        "RESOLVED": "good", "HEALTHY": "good", "SUCCESS": "good", "OK": "good", "TRUE": "", "FALSE": "",
    }.get(v, "plain")


class Table:
    def __init__(self, key, title, model, perm, columns, fields, text=(), time_field="timestamp",
                 detail=None, chips=(), subtitle="", chart=None):
        self.key, self.title, self.model, self.perm = key, title, model, perm
        self.columns = columns  # (label, attribute, kind)
        self.fields, self.text = fields, text
        self.time_field, self.detail, self.chips, self.subtitle = time_field, detail, chips, subtitle
        # volume-over-time chart: (column | None, value -> series name, [(series name, colour)])
        self.chart = chart
        self.sortable = {f.attname for f in model._meta.concrete_fields}

    def url(self, obj):
        if not self.detail:
            return ""
        name, attr = self.detail
        return reverse(f"django_observatory:{name}", args=[getattr(obj, attr)])

    def cells(self, obj):
        out = []
        for i, (label, attr, kind) in enumerate(self.columns):
            value = attr(obj) if callable(attr) else getattr(obj, attr)
            cell = {"kind": kind, "value": value}
            if kind in ("badge", "level", "status"):
                cell["tone"] = tone(kind, value)
            if i == 0:
                cell["href"] = self.url(obj)
            out.append(cell)
        return out


def _obj(o):
    return f"{o.object_type} #{o.object_id}" if o.object_id else o.object_type


TABLES = {t.key: t for t in (
    Table("logs", "Logs", ObservabilityEvent, "view_logs",
          [("Time", "timestamp", "time"), ("Level", "level", "level"), ("Logger", "logger_name", "mono"),
           ("Message", "message", "clip"), ("Category", "category", "text"), ("Request", "request_id", "rid")],
          {"level": F("level", "level"), "logger": F("logger_name"), "category": F("category"),
           "message": F("message", "text"), "user_id": F("user_id"), "module": F("module"),
           "function": F("function"), "type": F("event_type"), "fingerprint": F("fingerprint"),
           "host": F("hostname"), "ip": F("ip_address"), **_CORR, **_DEPLOY, **_TIME},
          text=("message", "logger_name"), detail=("log_detail", "pk"),
          chart=("level", lambda v: v if v in ("WARNING", "ERROR", "CRITICAL") else "INFO / DEBUG",
                 [("INFO / DEBUG", "s1"), ("WARNING", "warning"), ("ERROR", "serious"), ("CRITICAL", "critical")]),
          chips=("level:error", "level:>=warning", "category:security", "NOT logger:django.*")),
    Table("requests", "Requests", RequestRecord, "view_requests",
          [("Time", "timestamp", "time"), ("Method", "method", "mono"), ("Route", lambda o: o.route or o.path, "mono"),
           ("Status", "status_code", "status"), ("Duration", "duration_ms", "ms"), ("SQL", "db_count", "num"),
           ("User", "username", "text"), ("Request ID", "request_id", "rid")],
          {"status": F("status_code", "int"), "method": F("method"), "path": F("path"), "route": F("route"),
           "duration": F("duration_ms", "float"), "user_id": F("user_id"), "user": F("username"),
           "ip": F("ip_address"), "view": F("view_name"), "db_count": F("db_count", "int"),
           "db_ms": F("db_ms", "float"), "ext_ms": F("ext_ms", "float"), **_CORR, **_DEPLOY, **_TIME},
          text=("path", "route", "view_name"), detail=("request_detail", "request_id"),
          chart=("status_code", lambda v: f"{v // 100}xx" if 200 <= v < 600 else "other",
                 [("2xx", "good"), ("3xx", "neutral"), ("4xx", "warning"), ("5xx", "critical"), ("other", "neutral")]),
          chips=("status:>=500", "duration:>1000", "status:>=400 AND status:<500", "method:POST")),
    Table("traces", "Traces", TraceRecord, "view_traces",
          [("Time", "timestamp", "time"), ("Name", "name", "mono"), ("Kind", "kind", "text"),
           ("Duration", "duration_ms", "ms"), ("Spans", "span_count", "num"),
           ("Result", lambda o: "ERROR" if o.error else "OK", "badge")],
          {"name": F("name"), "kind": F("kind"), "duration": F("duration_ms", "float"), "error": F("error", "bool"),
           "spans": F("span_count", "int"), "user_id": F("user_id"), **_CORR, **_DEPLOY, **_TIME},
          text=("name",), detail=("trace_detail", "trace_id"),
          chart=("error", lambda v: "Error" if v else "OK", [("OK", "good"), ("Error", "critical")]),
          chips=("error:true", "duration:>1000", "kind:task")),
    Table("exceptions", "Exceptions", ExceptionRecord, "view_exceptions",
          [("Time", "timestamp", "time"), ("Type", "exc_type", "mono"), ("Message", "message", "clip"),
           ("Endpoint", "endpoint", "mono"), ("Issue", "issue_id", "issue"),
           ("Handled", lambda o: "handled" if o.handled else "unhandled", "text")],
          {"type": F("exc_type"), "message": F("message", "text"), "endpoint": F("endpoint"),
           "issue": F("issue_id", "int"), "handled": F("handled", "bool"), "user_id": F("user_id"),
           "function": F("function"), "fingerprint": F("fingerprint"), **_CORR, **_DEPLOY, **_TIME},
          text=("message", "exc_type", "endpoint"), detail=("exception_detail", "pk"),
          chart=("handled", lambda v: "Handled" if v else "Unhandled",
                 [("Handled", "warning"), ("Unhandled", "critical")]),
          chips=("handled:false", "type:*Timeout*")),
    Table("issues", "Issues", Issue, "view_exceptions",
          [("Last seen", "last_seen", "time"), ("Issue", "title", "clip"), ("Status", "status", "badge"),
           ("Events", "occurrence_count", "num"), ("First seen", "first_seen", "time"),
           ("Culprit", "culprit", "mono"), ("Assignee", "assignee", "text")],
          {"status": F("status"), "type": F("exc_type"), "title": F("title", "text"), "assignee": F("assignee"),
           "events": F("occurrence_count", "int"), "severity": F("severity"), "release": F("last_release"),
           "first_seen": F("first_seen", "datetime"), "last_seen": F("last_seen", "datetime")},
          text=("title", "culprit"), time_field="last_seen", detail=("issue_detail", "pk"),
          chips=("status:open OR status:regressed", "status:resolved", "events:>100"),
          subtitle="Exceptions grouped by fingerprint"),
    Table("security", "Security Events", SecurityEvent, "view_security_events",
          [("Time", "timestamp", "time"), ("Event", "event", "mono"), ("Severity", "severity", "badge"),
           ("User", "username", "text"), ("IP", "ip_address", "mono"), ("Resource", "resource", "clip"),
           ("Reason", "reason", "clip"), ("Request", "request_id", "rid")],
          {"event": F("event"), "severity": F("severity"), "user": F("username"), "user_id": F("user_id"),
           "ip": F("ip_address"), "resource": F("resource"), "reason": F("reason", "text"),
           **_CORR, **_DEPLOY, **_TIME},
          text=("resource", "reason", "username"),
          chart=("severity", lambda v: "Warning" if v == "warning" else "Notice" if v == "notice" else "Info",
                 [("Info", "s1"), ("Notice", "neutral"), ("Warning", "warning")]),
          chips=("event:LOGIN_FAILURE", "event:PERMISSION_DENIED", "event:SUPERUSER_LOGIN")),
    Table("audit", "Audit Trail", AuditEvent, "view_audit_events",
          [("Time", "timestamp", "time"), ("User", "username", "text"), ("Action", "action", "mono"),
           ("Object", _obj, "text"), ("Changes", "changes", "json"), ("Result", "result", "badge"),
           ("IP", "ip_address", "mono"), ("Request", "request_id", "rid")],
          {"user": F("username"), "user_id": F("user_id"), "action": F("action"), "object": F("object_type"),
           "object_id": F("object_id"), "result": F("result"), "ip": F("ip_address"),
           "reason": F("reason", "text"), **_CORR, **_TIME},
          text=("object_type", "object_id", "username", "reason"),
          chart=("result", lambda v: "Success" if v == "SUCCESS" else "Other",
                 [("Success", "s1"), ("Other", "critical")]),
          chips=("action:DELETE", "result:FAILURE"),
          subtitle="Append-only, hash-chained. Not editable or deletable from this interface."),
    Table("queries", "Captured SQL", DatabaseQuery, "view_metrics",
          [("Time", "timestamp", "time"), ("Duration", "duration_ms", "ms"), ("Database", "alias", "text"),
           ("Statement", "normalized_sql", "clip"), ("Slow", lambda o: "SLOW" if o.is_slow else "", "flag"),
           ("Result", lambda o: "OK" if o.success else "FAILED", "badge"), ("Request", "request_id", "rid")],
          {"duration": F("duration_ms", "float"), "alias": F("alias"), "slow": F("is_slow", "bool"),
           "success": F("success", "bool"), "fingerprint": F("fingerprint"), "sql": F("normalized_sql", "text"),
           "route": F("route"), **_CORR, **_TIME},
          text=("normalized_sql",), chart=("is_slow", lambda v: "Slow" if v else "Normal", [("Normal", "s1"), ("Slow", "warning")]),
          chips=("slow:true", "success:false", "duration:>100"),
          subtitle="Statements as sent to the driver; parameter values are not stored"),
    Table("external_calls", "External Calls", ExternalCall, "view_metrics",
          [("Time", "timestamp", "time"), ("Service", "service", "text"), ("Method", "method", "mono"),
           ("Path", "path", "clip"), ("Status", "status_code", "status"), ("Duration", "duration_ms", "ms"),
           ("Outcome", lambda o: "TIMEOUT" if o.timeout else "ERROR" if o.error else "OK", "badge"),
           ("Request", "request_id", "rid")],
          {"service": F("service"), "host": F("host"), "method": F("method"), "path": F("path"),
           "status": F("status_code", "int"), "duration": F("duration_ms", "float"), "error": F("error", "bool"),
           "timeout": F("timeout", "bool"), "route": F("route"), **_CORR, **_TIME},
          text=("service", "host", "path"), chart=("error", lambda v: "Error" if v else "OK", [("OK", "good"), ("Error", "critical")]),
          chips=("error:true", "timeout:true", "duration:>1000")),
    Table("incidents", "Incidents", Incident, "view_observability",
          [("Detected", "detected_at", "time"), ("Incident", "title", "clip"), ("Status", "status", "badge"),
           ("Severity", "severity", "badge"), ("Probable cause", "probable_cause", "clip"),
           ("Confidence", "confidence", "text"), ("Requests", "affected_requests", "num"),
           ("Users", "affected_users", "num")],
          {"status": F("status"), "severity": F("severity"), "cause": F("cause_kind"),
           "confidence": F("confidence"), "title": F("title", "text"), "requests": F("affected_requests", "int")},
          text=("title", "probable_cause"), time_field="detected_at", detail=("incident_detail", "pk"),
          chips=("status:open", "severity:critical OR severity:high"),
          subtitle="Correlated operational problems with their evidence"),
    Table("alert_history", "Alert History", Alert, "view_observability",
          [("Started", "started_at", "time"), ("Alert", "title", "text"), ("Status", "status", "badge"),
           ("Severity", "severity", "badge"), ("Detail", "message", "clip"), ("Resolved", "resolved_at", "time")],
          {"status": F("status"), "severity": F("severity"), "title": F("title", "text")},
          text=("title", "message"), time_field="started_at", chips=("status:firing",)),
)}
