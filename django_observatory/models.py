"""Storage models for the Django ORM backend.

Design notes
- High-volume telemetry is correlated by indexed ids (request_id / trace_id), not
  foreign keys: rows are bulk-inserted asynchronously and may live in a separate
  database from the application's tables (see routers.ObservabilityRouter).
- No FK to the user model for the same reason; user_id/username are denormalised.
- Only portable column types: works on SQLite, PostgreSQL and SQL Server.
"""
import hashlib
import json

from django.db import models
from django.utils import timezone


class Correlated(models.Model):
    request_id = models.CharField(max_length=40, blank=True, default="")
    trace_id = models.CharField(max_length=32, blank=True, default="")
    span_id = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        abstract = True


class Deployed(models.Model):
    service = models.CharField(max_length=64, blank=True, default="")
    environment = models.CharField(max_length=32, blank=True, default="")
    release = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        abstract = True


class ObservabilityEvent(Correlated, Deployed):
    """A log record / structured event."""

    timestamp = models.DateTimeField(default=timezone.now)
    event_type = models.CharField(max_length=32, default="log")
    level = models.CharField(max_length=10, default="INFO")
    level_no = models.PositiveSmallIntegerField(default=20)
    logger_name = models.CharField(max_length=200, blank=True, default="")
    message = models.TextField(blank=True, default="")
    category = models.CharField(max_length=32, blank=True, default="application")
    application = models.CharField(max_length=64, blank=True, default="")
    user_id = models.CharField(max_length=64, blank=True, default="")
    session_id = models.CharField(max_length=16, blank=True, default="")  # hash, never the key
    ip_address = models.CharField(max_length=45, blank=True, default="")
    hostname = models.CharField(max_length=128, blank=True, default="")
    process_id = models.IntegerField(null=True, blank=True)
    thread_id = models.BigIntegerField(null=True, blank=True)
    module = models.CharField(max_length=200, blank=True, default="")
    function = models.CharField(max_length=200, blank=True, default="")
    file_name = models.CharField(max_length=300, blank=True, default="")
    line_number = models.IntegerField(null=True, blank=True)
    exception_uid = models.CharField(max_length=32, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    fingerprint = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "log event"
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["level_no", "timestamp"]),
            models.Index(fields=["category", "timestamp"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["trace_id"]),
            models.Index(fields=["fingerprint"]),
        ]

    def __str__(self):
        return f"{self.level} {self.message[:80]}"


class RequestRecord(Correlated, Deployed):
    timestamp = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    route = models.CharField(max_length=300, blank=True, default="")
    query_params = models.JSONField(default=dict, blank=True)
    status_code = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.FloatField(default=0)
    user_id = models.CharField(max_length=64, blank=True, default="")
    username = models.CharField(max_length=150, blank=True, default="")
    session_id = models.CharField(max_length=16, blank=True, default="")
    ip_address = models.CharField(max_length=45, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    referrer = models.CharField(max_length=500, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    response_size = models.IntegerField(null=True, blank=True)
    view_name = models.CharField(max_length=200, blank=True, default="")
    view_module = models.CharField(max_length=200, blank=True, default="")
    exception_uid = models.CharField(max_length=32, blank=True, default="")
    db_count = models.IntegerField(default=0)
    db_ms = models.FloatField(default=0)
    slow_queries = models.IntegerField(default=0)
    ext_count = models.IntegerField(default=0)
    ext_ms = models.FloatField(default=0)
    headers = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["route", "timestamp"]),
            models.Index(fields=["status_code", "timestamp"]),
            models.Index(fields=["user_id", "timestamp"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["trace_id"]),
        ]

    def __str__(self):
        return f"{self.method} {self.path} {self.status_code}"

    @property
    def severity(self):
        """Semantic status: ok / redirect / client / error."""
        s = self.status_code
        return "error" if s >= 500 else "client" if s >= 400 else "redirect" if s >= 300 else "ok"


class TraceRecord(Deployed):
    trace_id = models.CharField(max_length=32)
    request_id = models.CharField(max_length=40, blank=True, default="")
    timestamp = models.DateTimeField(default=timezone.now)
    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=20, default="request")  # request | task | manual
    duration_ms = models.FloatField(default=0)
    span_count = models.IntegerField(default=0)
    dropped_spans = models.IntegerField(default=0)
    error = models.BooleanField(default=False)
    user_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["trace_id"]),
            models.Index(fields=["name", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.trace_id})"


class SpanRecord(models.Model):
    trace_id = models.CharField(max_length=32)
    span_id = models.CharField(max_length=16)
    parent_span_id = models.CharField(max_length=16, blank=True, default="")
    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=20, default="custom")  # request view db http template task custom
    timestamp = models.DateTimeField(default=timezone.now)
    offset_ms = models.FloatField(default=0)  # start relative to the trace start
    duration_ms = models.FloatField(default=0)
    status = models.CharField(max_length=10, default="ok")
    attributes = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["trace_id"]), models.Index(fields=["timestamp"])]

    def __str__(self):
        return self.name


class Issue(models.Model):
    """A group of exceptions sharing a fingerprint."""

    class Status(models.TextChoices):
        OPEN = "OPEN"
        ACKNOWLEDGED = "ACKNOWLEDGED"
        RESOLVED = "RESOLVED"
        IGNORED = "IGNORED"
        REGRESSED = "REGRESSED"

    fingerprint = models.CharField(max_length=40, unique=True)
    exc_type = models.CharField(max_length=200)
    title = models.CharField(max_length=300)
    culprit = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    severity = models.CharField(max_length=10, default="error")
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    occurrence_count = models.BigIntegerField(default=0)
    assignee = models.CharField(max_length=150, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    first_release = models.CharField(max_length=64, blank=True, default="")
    last_release = models.CharField(max_length=64, blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "last_seen"]), models.Index(fields=["last_seen"])]

    def __str__(self):
        return f"#{self.pk} {self.title}"

    @property
    def is_active(self):
        return self.status in (self.Status.OPEN, self.Status.REGRESSED, self.Status.ACKNOWLEDGED)


class ExceptionRecord(Correlated, Deployed):
    uid = models.CharField(max_length=32)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="occurrences")
    timestamp = models.DateTimeField(default=timezone.now)
    exc_type = models.CharField(max_length=200)
    message = models.TextField(blank=True, default="")
    stacktrace = models.TextField(blank=True, default="")
    frames = models.JSONField(default=list, blank=True)
    file_name = models.CharField(max_length=300, blank=True, default="")
    line_number = models.IntegerField(null=True, blank=True)
    function = models.CharField(max_length=200, blank=True, default="")
    handled = models.BooleanField(default=False)
    user_id = models.CharField(max_length=64, blank=True, default="")
    endpoint = models.CharField(max_length=300, blank=True, default="")
    method = models.CharField(max_length=10, blank=True, default="")
    breadcrumbs = models.JSONField(default=list, blank=True)
    fingerprint = models.CharField(max_length=40)

    class Meta:
        verbose_name = "exception"
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["issue", "timestamp"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["trace_id"]),
            models.Index(fields=["uid"]),
        ]

    def __str__(self):
        return f"{self.exc_type}: {self.message[:80]}"


class DatabaseQuery(Correlated):
    timestamp = models.DateTimeField(default=timezone.now)
    alias = models.CharField(max_length=64, default="default")
    vendor = models.CharField(max_length=32, blank=True, default="")
    duration_ms = models.FloatField(default=0)
    sql = models.TextField()  # as sent to the driver: placeholders, no parameter values
    normalized_sql = models.TextField(blank=True, default="")
    fingerprint = models.CharField(max_length=40)
    params = models.JSONField(null=True, blank=True)  # only with DATABASE.CAPTURE_PARAMS
    success = models.BooleanField(default=True)
    is_slow = models.BooleanField(default=False)
    many = models.BooleanField(default=False)
    route = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        verbose_name_plural = "database queries"
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["fingerprint", "timestamp"]),
            models.Index(fields=["is_slow", "timestamp"]),
            models.Index(fields=["request_id"]),
        ]

    def __str__(self):
        return self.normalized_sql[:80]


class ExternalCall(Correlated):
    timestamp = models.DateTimeField(default=timezone.now)
    service = models.CharField(max_length=100)
    host = models.CharField(max_length=255)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500, blank=True, default="")  # no query string
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.FloatField(default=0)
    error = models.BooleanField(default=False)
    timeout = models.BooleanField(default=False)
    exc_type = models.CharField(max_length=200, blank=True, default="")
    response_size = models.IntegerField(null=True, blank=True)
    route = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["service", "timestamp"]),
            models.Index(fields=["request_id"]),
        ]

    def __str__(self):
        return f"{self.method} {self.host}{self.path}"


class Metric(models.Model):
    name = models.CharField(max_length=150, unique=True)
    kind = models.CharField(max_length=10, default="counter")  # counter gauge histogram
    unit = models.CharField(max_length=20, blank=True, default="")
    description = models.CharField(max_length=300, blank=True, default="")

    def __str__(self):
        return self.name


class MetricSample(models.Model):
    """Pre-aggregated bucket: one row per (metric, labels, bucket start, writer process)."""

    metric = models.ForeignKey(Metric, on_delete=models.CASCADE, related_name="samples")
    timestamp = models.DateTimeField()  # bucket start
    resolution = models.IntegerField(default=60)  # seconds: 60 -> 3600 -> 86400 by rollup
    labels = models.JSONField(default=dict, blank=True)
    labels_key = models.CharField(max_length=300, blank=True, default="")
    count = models.BigIntegerField(default=0)
    sum = models.FloatField(default=0)
    min = models.FloatField(null=True, blank=True)
    max = models.FloatField(null=True, blank=True)
    last = models.FloatField(null=True, blank=True)
    buckets = models.JSONField(null=True, blank=True)  # histogram counts per BOUNDS entry

    class Meta:
        indexes = [
            models.Index(fields=["metric", "resolution", "timestamp"]),
            models.Index(fields=["resolution", "timestamp"]),
        ]


class SecurityEvent(Correlated, Deployed):
    timestamp = models.DateTimeField(default=timezone.now)
    event = models.CharField(max_length=40)
    severity = models.CharField(max_length=10, default="info")
    user_id = models.CharField(max_length=64, blank=True, default="")
    username = models.CharField(max_length=150, blank=True, default="")
    ip_address = models.CharField(max_length=45, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    resource = models.CharField(max_length=500, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["event", "timestamp"]),
            models.Index(fields=["ip_address", "timestamp"]),
            models.Index(fields=["user_id", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.event} {self.username}"


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionError("Audit events are immutable.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise PermissionError("Audit events are immutable.")

    def delete(self):
        raise PermissionError("Audit events can only be removed by retention cleanup.")


class AuditEvent(Correlated):
    """Append-only and hash-chained: each row commits to its predecessor's hash, so
    edits and deletions are detectable with ``AuditEvent.verify_chain()``."""

    timestamp = models.DateTimeField(default=timezone.now)
    user_id = models.CharField(max_length=64, blank=True, default="")
    username = models.CharField(max_length=150, blank=True, default="")
    action = models.CharField(max_length=40)
    object_type = models.CharField(max_length=100, blank=True, default="")
    object_id = models.CharField(max_length=100, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    changes = models.JSONField(null=True, blank=True)
    ip_address = models.CharField(max_length=45, blank=True, default="")
    result = models.CharField(max_length=20, default="SUCCESS")
    reason = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    prev_hash = models.CharField(max_length=64, blank=True, default="")
    hash = models.CharField(max_length=64, blank=True, default="")

    objects = ImmutableQuerySet.as_manager()

    HASHED = ("timestamp", "user_id", "username", "action", "object_type", "object_id", "before",
              "after", "changes", "ip_address", "result", "reason", "metadata", "request_id", "trace_id")

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["user_id", "timestamp"]),
            models.Index(fields=["hash"]),
        ]

    def __str__(self):
        return f"{self.username} {self.action} {self.object_type} #{self.object_id}"

    def compute_hash(self):
        payload = {f: getattr(self, f) for f in self.HASHED}
        payload["timestamp"] = self.timestamp.replace(microsecond=0).isoformat() if self.timestamp else ""
        body = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256((self.prev_hash + body).encode()).hexdigest()

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit events can only be removed by retention cleanup.")

    @classmethod
    def verify_chain(cls, limit=100000):
        """Returns (checked, problems). A row is valid when its hash recomputes and its
        predecessor matches the prior row's hash (the oldest surviving row is the anchor after retention)."""
        problems, checked = [], 0
        last_hash = None
        for row in cls.objects.order_by("pk")[:limit].iterator():
            checked += 1
            if row.compute_hash() != row.hash:
                problems.append((row.pk, "modified"))
            elif last_hash is not None and row.prev_hash != last_hash:
                problems.append((row.pk, "predecessor missing"))
            last_hash = row.hash
        return checked, problems


class AlertRule(models.Model):
    METRICS = [
        ("error_rate", "HTTP 5xx rate (%)"),
        ("p95_latency", "Request P95 latency (ms)"),
        ("p99_latency", "Request P99 latency (ms)"),
        ("request_rate", "Requests per minute"),
        ("exception_count", "Exceptions in window"),
        ("issue_occurrences", "Occurrences of the busiest issue in window"),
        ("external_failure_rate", "External API failure rate (%)"),
        ("external_p95", "External API P95 latency (ms)"),
        ("slow_query_count", "Slow queries in window"),
        ("db_max_ms", "Slowest database query (ms)"),
        ("login_failures", "Failed logins in window"),
        ("cpu_percent", "Server CPU usage, average (%)"),
        ("memory_percent", "Server memory usage, average (%)"),
        ("disk_percent", "Fullest disk volume (%)"),
        ("custom", "Custom metric (sum in window)"),
    ]
    OPERATORS = [(">", ">"), (">=", ">="), ("<", "<"), ("<=", "<=")]

    name = models.CharField(max_length=150, unique=True)
    metric = models.CharField(max_length=40, choices=METRICS)
    custom_metric = models.CharField(max_length=150, blank=True, default="")
    operator = models.CharField(max_length=2, choices=OPERATORS, default=">")
    threshold = models.FloatField()
    window_minutes = models.PositiveIntegerField(default=5)
    severity = models.CharField(max_length=10, default="warning")
    channels = models.JSONField(default=list, blank=True)  # ["email", "webhook"]; in-app is implicit
    cooldown_minutes = models.PositiveIntegerField(default=30)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Incident(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN"
        ACKNOWLEDGED = "ACKNOWLEDGED"
        RESOLVED = "RESOLVED"

    title = models.CharField(max_length=300)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    severity = models.CharField(max_length=10, default="medium")
    dedup_key = models.CharField(max_length=200)
    started_at = models.DateTimeField()  # probable start (inferred)
    detected_at = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    cause_kind = models.CharField(max_length=40, blank=True, default="")
    cause_subject = models.CharField(max_length=200, blank=True, default="")
    probable_cause = models.CharField(max_length=500, blank=True, default="")
    confidence = models.CharField(max_length=10, default="LOW")
    confidence_score = models.FloatField(default=0)
    recommendation = models.CharField(max_length=500, blank=True, default="")
    affected_requests = models.IntegerField(default=0)
    affected_users = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    affected_endpoints = models.JSONField(default=list, blank=True)
    alternatives = models.JSONField(default=list, blank=True)  # other hypotheses considered
    notes = models.TextField(blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["status", "last_seen"]), models.Index(fields=["dedup_key", "status"])]

    def __str__(self):
        return f"Incident #{self.pk}: {self.title}"


class IncidentSignal(models.Model):
    """An observed fact attached to an incident, with its inferred role."""

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="signals")
    kind = models.CharField(max_length=40)  # request_latency error_rate external_latency ...
    source = models.CharField(max_length=200, blank=True, default="")  # service / route / issue title
    relationship = models.CharField(max_length=12, default="correlated")  # cause effect correlated evidence
    timestamp = models.DateTimeField(default=timezone.now)
    description = models.CharField(max_length=300)
    baseline = models.FloatField(null=True, blank=True)
    current = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=10, blank=True, default="")
    score = models.FloatField(default=0)
    ref_kind = models.CharField(max_length=20, blank=True, default="")  # issue external route metric release
    ref_id = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["incident", "kind"])]


class Alert(models.Model):
    class Status(models.TextChoices):
        FIRING = "FIRING"
        ACKNOWLEDGED = "ACKNOWLEDGED"
        RESOLVED = "RESOLVED"

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="alerts")
    incident = models.ForeignKey(Incident, null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.FIRING)
    severity = models.CharField(max_length=10, default="warning")
    title = models.CharField(max_length=300)
    message = models.TextField(blank=True, default="")
    value = models.FloatField(null=True, blank=True)
    threshold = models.FloatField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    evaluations = models.IntegerField(default=1)
    acknowledged_by = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["status", "started_at"]), models.Index(fields=["rule", "status"])]

    def __str__(self):
        return self.title


class ObservabilityConfiguration(models.Model):
    """Runtime key/value state: setting overrides, scheduler lease."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)
    updated_by = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        default_permissions = ("change",)
        permissions = [
            ("view_observability", "Can open the observability UI"),
            ("view_logs", "Can view logs"),
            ("view_requests", "Can view requests"),
            ("view_traces", "Can view traces"),
            ("view_exceptions", "Can view exceptions and issues"),
            ("view_metrics", "Can view metrics and performance"),
            ("view_security_events", "Can view security events"),
            ("view_audit_events", "Can view audit events"),
            ("manage_alerts", "Can manage alerts, incidents and issues"),
            ("manage_observability_settings", "Can manage observability settings"),
            ("export_observability_data", "Can export observability data"),
            ("delete_observability_data", "Can delete observability data"),
        ]

    def __str__(self):
        return self.key
