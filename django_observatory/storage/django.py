"""Default backend: the project's own database through the Django ORM."""
from collections import defaultdict

from django.db import connections, models, transaction
from django.db.models import F, Min

from .. import conf
from ..models import (
    Alert, AuditEvent, DatabaseQuery, ExceptionRecord, ExternalCall, Incident, Issue, Metric,
    MetricSample, ObservabilityEvent, RequestRecord, SecurityEvent, SpanRecord, TraceRecord,
)
from .base import ObservabilityBackend

MODELS = {
    "event": ObservabilityEvent, "request": RequestRecord, "trace": TraceRecord,
    "span": SpanRecord, "exception": ExceptionRecord, "query": DatabaseQuery,
    "external": ExternalCall, "security": SecurityEvent, "audit": AuditEvent,
    "metric": MetricSample,
}

# retention kind -> [(model, timestamp field, extra filter)]
RETENTION = {
    "logs": [(ObservabilityEvent, "timestamp", {})],
    "requests": [(RequestRecord, "timestamp", {}), (DatabaseQuery, "timestamp", {}), (ExternalCall, "timestamp", {})],
    "traces": [(TraceRecord, "timestamp", {}), (SpanRecord, "timestamp", {})],
    "metrics": [(MetricSample, "timestamp", {})],
    "exceptions": [(ExceptionRecord, "timestamp", {})],
    "audit": [(AuditEvent, "timestamp", {})],
    "security": [(SecurityEvent, "timestamp", {})],
    "alerts": [(Alert, "started_at", {"status": "RESOLVED"}), (Incident, "detected_at", {"status": "RESOLVED"})],
}

_limits = {}


def _fit(model, row):
    """Truncate strings to column size: SQL Server / PostgreSQL reject over-long values."""
    lim = _limits.get(model)
    if lim is None:
        lim = _limits[model] = {
            f.attname: f.max_length for f in model._meta.concrete_fields
            if isinstance(f, models.CharField) and f.max_length
        }
    for name, n in lim.items():
        v = row.get(name)
        if v is not None and len(v) > n:
            row[name] = v[:n]
    return row


class DjangoORMBackend(ObservabilityBackend):
    def __init__(self):
        self._prepared = set()  # connection objects already tuned (one per writer thread)

    def _prepare(self):
        """SQLite only: WAL lets application readers proceed while the worker writes
        (in the default rollback-journal mode every commit briefly locks readers out)."""
        conn = connections[self.alias]
        if conn.vendor != "sqlite" or id(conn) in self._prepared:
            return
        self._prepared.add(id(conn))
        if not conf.get("STORAGE", "SQLITE_WAL") or conn.in_atomic_block:
            return
        with conn.cursor() as cur:
            cur.execute("PRAGMA journal_mode=WAL")  # persistent property of the database file
            cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; one fsync per checkpoint, not per commit

    def write_batch(self, grouped):
        """One transaction (one commit) for the whole batch."""
        self._prepare()
        with transaction.atomic(using=self.alias):
            for kind, rows in grouped.items():
                self.write(kind, rows)

    @property
    def alias(self):
        return conf.get("STORAGE", "DATABASE_ALIAS") or "default"

    # -- writes -------------------------------------------------------------
    def write(self, kind, rows):
        if not rows:
            return
        with transaction.atomic(using=self.alias):
            special = getattr(self, f"_write_{kind}", None)
            if special:
                special(rows)
            else:
                model = MODELS[kind]
                model.objects.bulk_create([model(**_fit(model, r)) for r in rows], batch_size=200)

    def _write_exception(self, rows):
        by_fp = defaultdict(list)
        for r in rows:
            by_fp[r["fingerprint"]].append(r)
        records = []
        for fp, group in by_fp.items():
            first, last = group[0], group[-1]
            meta = first["_issue"]
            issue, created = Issue.objects.get_or_create(
                fingerprint=fp,
                defaults=_fit(Issue, {
                    "exc_type": first["exc_type"], "title": meta["title"], "culprit": meta["culprit"],
                    "severity": meta.get("severity", "error"), "first_seen": first["timestamp"],
                    "last_seen": last["timestamp"], "first_release": first.get("release", ""),
                    "last_release": last.get("release", ""), "occurrence_count": len(group),
                }),
            )
            if not created:
                update = {"occurrence_count": F("occurrence_count") + len(group),
                          "last_seen": last["timestamp"], "last_release": last.get("release", "")}
                if issue.status == Issue.Status.RESOLVED:  # seen again after resolution
                    update.update(status=Issue.Status.REGRESSED, resolved_at=None)
                Issue.objects.filter(pk=issue.pk).update(**update)
            records += [
                ExceptionRecord(issue=issue, **_fit(ExceptionRecord, {k: v for k, v in r.items() if k != "_issue"}))
                for r in group
            ]
        ExceptionRecord.objects.bulk_create(records, batch_size=100)

    def _write_audit(self, rows):
        base = models.QuerySet(AuditEvent, using=self.alias)
        last = base.order_by("-pk").select_for_update().first()
        prev = last.hash if last else ""
        objs = []
        for r in rows:
            obj = AuditEvent(**_fit(AuditEvent, r))
            obj.prev_hash = prev
            prev = obj.hash = obj.compute_hash()
            objs.append(obj)
        base.bulk_create(objs, batch_size=100)

    def _write_metric(self, rows):
        # ids resolved per batch (one query per flush): no cache to go stale
        ids = dict(Metric.objects.filter(name__in={r["name"][:150] for r in rows}).values_list("name", "id"))
        objs = []
        for r in rows:
            r = dict(r)  # rows must survive a pipeline retry untouched
            name = r.pop("name")[:150]
            kind, unit = r.pop("kind", "counter"), r.pop("unit", "")
            if name not in ids:
                ids[name] = Metric.objects.get_or_create(name=name, defaults={"kind": kind, "unit": unit})[0].pk
            objs.append(MetricSample(metric_id=ids[name], **_fit(MetricSample, r)))
        MetricSample.objects.bulk_create(objs, batch_size=200)

    # -- reads --------------------------------------------------------------
    def query(self, kind):
        return models.QuerySet(MODELS[kind])

    # -- retention ----------------------------------------------------------
    def delete_before(self, retention_kind, timestamp, batch_size=2000):
        deleted = 0
        for model, field, extra in RETENTION[retention_kind]:
            qs = models.QuerySet(model).filter(**{f"{field}__lt": timestamp}, **extra)
            while True:
                # pk-range batches: short transactions, no giant DELETE, no huge IN list
                ids = list(qs.order_by("pk").values_list("pk", flat=True)[:batch_size])
                if not ids:
                    break
                n, _ = qs.filter(pk__gte=ids[0], pk__lte=ids[-1]).delete()
                deleted += n
                if len(ids) < batch_size:
                    break
        if retention_kind == "exceptions":
            n, _ = Issue.objects.filter(last_seen__lt=timestamp, occurrences__isnull=True).delete()
            deleted += n
        return deleted

    def get_statistics(self):
        out = {}
        for kind, model in {**MODELS, "issue": Issue, "incident": Incident, "alert": Alert}.items():
            field = "started_at" if model is Alert else "detected_at" if model is Incident else \
                "first_seen" if model is Issue else "timestamp"
            qs = models.QuerySet(model)
            out[kind] = {"rows": qs.count(), "oldest": qs.aggregate(o=Min(field))["o"]}
        return out
