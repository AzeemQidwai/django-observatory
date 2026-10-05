"""Alert rules: evaluation, deduplication, notification.

One open Alert per rule. While a condition holds the alert is updated, not duplicated;
external notifications go out when it opens and again only after the rule's cooldown.
Alerts opened during an active incident are attached to it and notified once, as the
incident, instead of individually.
"""
import datetime
import json
import operator
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count
from django.utils import timezone

from . import conf, context, internal, metrics
from .models import Alert, AlertRule, ExceptionRecord, Incident
from .models import ObservabilityConfiguration as Cfg

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
_UNITS = {"error_rate": "%", "p95_latency": " ms", "p99_latency": " ms", "request_rate": "/min",
          "external_failure_rate": "%", "external_p95": " ms", "db_max_ms": " ms",
          "cpu_percent": "%", "memory_percent": "%", "disk_percent": "%"}

DEFAULT_RULES = (
    {"name": "High error rate", "metric": "error_rate", "threshold": 5, "severity": "critical"},
    {"name": "Slow responses (P95)", "metric": "p95_latency", "threshold": 2000},
    {"name": "Issue spike", "metric": "issue_occurrences", "threshold": 100, "window_minutes": 60},
    {"name": "External API failures", "metric": "external_failure_rate", "threshold": 10},
    {"name": "Very slow database query", "metric": "db_max_ms", "threshold": 5000},
    {"name": "Disk almost full", "metric": "disk_percent", "threshold": 90, "severity": "critical"},
    {"name": "Server memory pressure", "metric": "memory_percent", "threshold": 90, "window_minutes": 10},
    {"name": "Server CPU saturated", "metric": "cpu_percent", "threshold": 90, "window_minutes": 10},
)
_SYSTEM_RULES = ("Disk almost full", "Server memory pressure", "Server CPU saturated")


def ensure_default_rules():
    if not conf.get("ALERTS", "DEFAULT_RULES"):
        return
    # two flags, so installations created before server monitoring existed gain its rules once
    for key, wanted in (("defaults:alert_rules", lambda n: n not in _SYSTEM_RULES),
                        ("defaults:alert_rules:system", lambda n: n in _SYSTEM_RULES)):
        _, created = Cfg.objects.get_or_create(key=key)
        if created:
            for spec in DEFAULT_RULES:
                if wanted(spec["name"]):
                    AlertRule.objects.get_or_create(name=spec["name"],
                                                    defaults={k: v for k, v in spec.items() if k != "name"})


# -- measurement ----------------------------------------------------------------
def measure(rule, now=None):
    """(value, detail) for the rule's window, or None when there is too little data to judge."""
    now = now or timezone.now()
    start = now - datetime.timedelta(minutes=rule.window_minutes)
    m = rule.metric
    if m == "error_rate":
        by = metrics.by_label("http.requests", "status", start, now)
        total = sum(a.sum for a in by.values())
        if total < 10:
            return None
        errors = by["5xx"].sum if "5xx" in by else 0
        return errors / total * 100, f"{errors:.0f} of {total:.0f} requests failed"
    if m in ("p95_latency", "p99_latency"):
        agg = metrics.total("http.duration", start, now)
        if agg.count < 10:
            return None
        return agg.quantile(0.95 if m == "p95_latency" else 0.99), f"over {agg.count} requests"
    if m == "request_rate":
        return metrics.total("http.requests", start, now).sum / rule.window_minutes, ""
    if m == "exception_count":
        return metrics.total("exceptions", start, now).sum, ""
    if m == "issue_occurrences":
        top = (ExceptionRecord.objects.filter(timestamp__gte=start).values("issue_id", "issue__title")
               .annotate(n=Count("id")).order_by("-n").first())
        return (top["n"], top["issue__title"]) if top else (0, "")
    if m in ("external_failure_rate", "external_p95"):
        worst = None
        if m == "external_p95":
            for service, agg in metrics.by_label("ext.duration", "service", start, now).items():
                if agg.count >= 5 and (worst is None or agg.quantile(0.95) > worst[0]):
                    worst = (agg.quantile(0.95), f"service {service}")
        else:
            calls = metrics._scan("ext.calls", start, now, lambda n, t, lb: (lb.get("service"), lb.get("outcome")))
            for service in {s for s, _ in calls}:
                ok = calls.get((service, "ok"))
                err = calls.get((service, "error"))
                total = (ok.sum if ok else 0) + (err.sum if err else 0)
                rate = (err.sum if err else 0) / total * 100 if total else 0
                if total >= 5 and (worst is None or rate > worst[0]):
                    worst = (rate, f"service {service}")
        return worst
    if m == "slow_query_count":
        return metrics.total("db.slow_queries", start, now).sum, ""
    if m == "db_max_ms":
        agg = metrics.total("db.duration", start, now)
        return (agg.max, "") if agg.count else None
    if m == "login_failures":
        return metrics.total("security.events", start, now, event="LOGIN_FAILURE").sum, ""
    if m in ("cpu_percent", "memory_percent", "disk_percent"):
        from . import system

        w = system.worst(start, now + datetime.timedelta(minutes=1))
        if m == "disk_percent":
            return (w["disk"], f"volume {w['disk_mount']} on {w['disk_host']}") if w["disk"] is not None else None
        key = "cpu" if m == "cpu_percent" else "memory"
        return (w[key], f"host {w[key + '_host']}") if w[key] is not None else None
    if m == "custom" and rule.custom_metric:
        return metrics.total(rule.custom_metric, start, now).sum, rule.custom_metric
    return None


# -- notification ---------------------------------------------------------------
def _email(subject, body, payload):
    to = conf.get("ALERTS", "EMAIL_TO")
    if to:
        send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), list(to), fail_silently=False)


def _webhook(subject, body, payload):
    url = conf.get("ALERTS", "WEBHOOK_URL")
    if url:
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            internal.warn("alerts.webhook", f"invalid webhook scheme: {url}")
            return
        data = json.dumps({"text": f"{subject}\n{body}", **payload}, default=str).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).close()  # noqa: S310 - operator-configured URL


# Add channels here ("teams", "slack", "sms"): fn(subject, body, payload).
CHANNELS = {"email": _email, "webhook": _webhook}


def notify(subject, body, payload, channels=None):
    """Best effort on every channel; one failing channel never blocks another."""
    if channels is None:
        channels = [c for c, on in (("email", conf.get("ALERTS", "EMAIL_TO")),
                                    ("webhook", conf.get("ALERTS", "WEBHOOK_URL"))) if on]
    sent = []
    with context.suppressed():
        for name in channels:
            fn = CHANNELS.get(name)
            if fn is None:
                continue
            try:
                fn(subject, body, payload)
                sent.append(name)
            except Exception as exc:  # noqa: BLE001
                internal.warn(f"alerts.{name}", "notification failed", exc)
    return sent


def notify_incident(incident):
    body = (f"Probable cause ({incident.confidence} confidence, inferred): {incident.probable_cause}\n"
            f"Impact: {incident.affected_requests} requests, {incident.affected_users} users, "
            f"{incident.error_count} errors\nRecommended: {incident.recommendation}")
    notify(f"[{conf.get('SERVICE_NAME')}] Incident #{incident.pk}: {incident.title}", body,
           {"type": "incident", "id": incident.pk, "severity": incident.severity, "status": incident.status})


# -- evaluation -----------------------------------------------------------------
def evaluate(now=None):
    """Evaluate every enabled rule once. Returns the alerts opened in this run."""
    now = now or timezone.now()
    opened = []
    with context.suppressed():
        ensure_default_rules()
        incident = Incident.objects.exclude(status=Incident.Status.RESOLVED).order_by("-detected_at").first()
        for rule in AlertRule.objects.filter(enabled=True):
            try:
                result = measure(rule, now)
            except Exception as exc:  # noqa: BLE001
                internal.warn("alerts.measure", f"rule '{rule.name}' failed", exc)
                continue
            current = (Alert.objects.filter(rule=rule).exclude(status=Alert.Status.RESOLVED)
                       .order_by("-started_at").first())
            firing = result is not None and result[0] is not None and _OPS[rule.operator](result[0], rule.threshold)
            if not firing:
                if current and result is not None:  # "no data" never auto-resolves
                    current.status, current.resolved_at = Alert.Status.RESOLVED, now
                    current.save(update_fields=["status", "resolved_at"])
                continue
            value, detail = result
            unit = _UNITS.get(rule.metric, "")
            message = (f"{rule.get_metric_display()} is {value:.1f}{unit} "
                       f"({rule.operator} {rule.threshold:g}{unit}) over {rule.window_minutes} min"
                       + (f": {detail}" if detail else ""))
            if current:
                current.value, current.last_seen, current.message = value, now, message
                current.evaluations += 1
                due = (current.status == Alert.Status.FIRING and current.incident_id is None and current.notified_at
                       and now - current.notified_at >= datetime.timedelta(minutes=rule.cooldown_minutes))
                if due:
                    notify(f"[{conf.get('SERVICE_NAME')}] STILL FIRING: {rule.name}", message,
                           {"type": "alert", "id": current.pk, "severity": rule.severity}, rule.channels or None)
                    current.notified_at = now
                current.save()
                continue
            alert = Alert.objects.create(rule=rule, incident=incident, severity=rule.severity, title=rule.name,
                                         message=message, value=value, threshold=rule.threshold,
                                         started_at=now, last_seen=now)
            opened.append(alert)
            if incident is None:  # grouped under the incident otherwise: one notification, not many
                notify(f"[{conf.get('SERVICE_NAME')}] ALERT: {rule.name}", message,
                       {"type": "alert", "id": alert.pk, "severity": rule.severity}, rule.channels or None)
                alert.notified_at = now
                alert.save(update_fields=["notified_at"])
    return opened
