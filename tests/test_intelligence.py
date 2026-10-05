"""Alerts, incident detection, correlation rules, causal analysis, graph, retention, commands."""
import datetime
import io
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from django_observatory import alerts, maintenance, metrics
from django_observatory.correlation import causal, engine, graph
from django_observatory.metrics import BOUNDS, _bucket_index
from django_observatory.models import (
    Alert, AlertRule, AuditEvent, DatabaseQuery, ExceptionRecord, ExternalCall, Incident, Issue,
    ObservabilityEvent, RequestRecord, SecurityEvent, SpanRecord,
)
from django_observatory.models import ObservabilityConfiguration as Cfg
from django_observatory.storage import get_backend

BASE = {"PIPELINE": {"SYNC": True}, "SCHEDULER": {"ENABLED": False}, "ALERTS": {"DEFAULT_RULES": False}, "RELEASE": "1.0"}


class Scenario:
    """Writes synthetic telemetry at chosen times (minutes ago) through the storage backend."""

    def __init__(self):
        self.now = timezone.now().replace(second=30, microsecond=0)
        self.rows = []
        self.n = 0

    def at(self, minutes_ago):
        return (self.now - datetime.timedelta(minutes=minutes_ago)).replace(second=0)

    def metric(self, name, minutes_ago, value, count=1, kind="histogram", **labels):
        buckets = None
        if kind == "histogram":
            buckets = [0] * (len(BOUNDS) + 1)
            buckets[_bucket_index(value)] = count
        self.rows.append({"name": name, "kind": kind, "timestamp": self.at(minutes_ago), "resolution": 60,
                          "labels": labels, "labels_key": ",".join(f"{k}={v}" for k, v in sorted(labels.items())),
                          "count": count, "sum": value * count if kind == "histogram" else count,
                          "min": value, "max": value, "last": value, "buckets": buckets})

    def minute(self, minutes_ago, *, requests=20, errors=0, latency=300, sap=None, sap_errors=0, route="/api/materials/"):
        self.metric("http.requests", minutes_ago, 1, requests - errors, "counter", route=route, method="GET", status="2xx")
        if errors:
            self.metric("http.requests", minutes_ago, 1, errors, "counter", route=route, method="GET", status="5xx")
        self.metric("http.duration", minutes_ago, latency, requests, route=route)
        if sap is not None:
            self.metric("ext.duration", minutes_ago, sap, requests, service="SAP")
            self.metric("ext.calls", minutes_ago, 1, requests - sap_errors, "counter", service="SAP", outcome="ok")
            if sap_errors:
                self.metric("ext.calls", minutes_ago, 1, sap_errors, "counter", service="SAP", outcome="error")

    def request(self, minutes_ago, *, status=200, duration=300, ext_ms=0, db_ms=0, user="", sap=None, timeout=False,
                route="/api/materials/", release="1.0"):
        self.n += 1
        rid = f"req_{self.n:024d}"
        ts = self.at(minutes_ago)
        RequestRecord.objects.create(request_id=rid, trace_id=f"{self.n:032x}", timestamp=ts, method="GET", path=route,
                                     route=route, status_code=status, duration_ms=duration, ext_ms=ext_ms, db_ms=db_ms,
                                     user_id=user, username=user, release=release)
        if sap is not None:
            ExternalCall.objects.create(request_id=rid, timestamp=ts, service="SAP", host="sap", method="GET",
                                        path="/stock", duration_ms=sap, error=timeout, timeout=timeout, route=route,
                                        exc_type="TimeoutError" if timeout else "")
        return rid

    def flush(self):
        get_backend().write("metric", self.rows)
        self.rows = []


def sap_outage():
    """Healthy hour (SAP ~400ms, requests ~300ms), then 4 minutes of SAP at ~4.8s with timeouts."""
    s = Scenario()
    for m in range(6, 60):
        s.minute(m, sap=400)
    for m in range(0, 4):
        s.minute(m, errors=8, latency=4800, sap=4800, sap_errors=8)
        s.metric("exceptions", m, 1, 8, "counter", type="TimeoutError")
        for i in range(8):
            s.request(m, status=500, duration=4800, ext_ms=4700, sap=4700, timeout=True, user=f"u{i % 5}")
    s.flush()
    return s


class IncidentTests(TestCase):
    def test_external_degradation_is_identified_with_evidence(self):
        s = sap_outage()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.cause_subject, incident.confidence), ("external", "SAP", "HIGH"))
        self.assertEqual(incident.title, "SAP integration degradation")
        self.assertIn("SAP", incident.recommendation)
        self.assertEqual((incident.affected_requests, incident.error_count, incident.affected_users), (80, 32, 5))
        self.assertEqual(incident.affected_endpoints, [{"route": "/api/materials/"}])
        self.assertTrue(s.at(4) <= incident.started_at <= s.at(2))  # inferred start is near the real one
        by_role = {}
        for sig in incident.signals.all():
            by_role.setdefault(sig.relationship, []).append(sig)
        self.assertEqual({x.kind for x in by_role["cause"]}, {"external_latency", "external_failures"})
        self.assertTrue({"error_rate", "request_latency"} <= {x.kind for x in by_role["effect"]})
        latency = next(x for x in by_role["cause"] if x.kind == "external_latency")
        self.assertTrue(latency.baseline < 500 < 2500 < latency.current)  # the numbers are stored facts
        facts = " | ".join(x.description for x in by_role["evidence"])
        self.assertIn("SAP is called in 100% of the 32 failing/slow requests", facts)
        self.assertIn("timeout exception", facts)
        self.assertTrue(incident.alternatives)  # other hypotheses are kept, not hidden

    def test_incident_is_deduplicated_then_resolved(self):
        s = sap_outage()
        first, = engine.detect(s.now)
        again, = engine.detect(s.now)
        self.assertEqual((first.pk, Incident.objects.count()), (again.pk, 1))
        later = s.now + datetime.timedelta(minutes=30)
        self.assertEqual(engine.detect(later), [])
        self.assertEqual(Incident.objects.get().status, "RESOLVED")

    def test_healthy_traffic_creates_no_incident(self):
        s = Scenario()
        for m in range(0, 60):
            s.minute(m, sap=400, errors=0)
        s.flush()
        self.assertEqual(engine.detect(s.now), [])

    def test_small_samples_do_not_trigger(self):
        s = Scenario()
        s.minute(1, requests=4, errors=4, latency=9000)
        s.flush()
        self.assertEqual(engine.detect(s.now), [])

    def test_database_degradation(self):
        s = Scenario()
        for m in range(6, 60):
            s.minute(m)
            s.metric("db.duration", m, 8, 100)
        for m in range(0, 4):
            s.minute(m, latency=3000)
            s.metric("db.duration", m, 2400, 100)
            for _ in range(6):
                s.request(m, duration=3000, db_ms=2700)
        DatabaseQuery.objects.create(timestamp=s.at(1), duration_ms=2400, sql="SELECT 1", is_slow=True,
                                     normalized_sql="SELECT * FROM inventory WHERE x = ?", fingerprint="fp1")
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.confidence), ("database", "HIGH"))
        self.assertIn("inventory", " ".join(x.description for x in incident.signals.filter(relationship="evidence")))

    @override_settings(OBSERVABILITY={**BASE, "RELEASE": "2.0"})
    def test_deployment_regression(self):
        s = Scenario()
        Cfg.objects.create(key="release:1.0", updated_at=s.at(600))
        Cfg.objects.create(key="release:2.0", updated_at=s.at(4))
        for m in range(6, 60):
            s.minute(m)
        for m in range(0, 4):
            s.minute(m, errors=10)
            for _ in range(5):
                s.request(m, status=500, release="2.0")
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.cause_subject), ("deployment", "2.0"))
        self.assertIn("roll", incident.recommendation.lower())

    def test_exception_spike_and_unexplained_fallback(self):
        s = Scenario()
        for m in range(6, 60):
            s.minute(m)
        for m in range(0, 4):
            s.minute(m, errors=10)
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.confidence), ("unexplained", "LOW"))  # honest when unsure
        issue = Issue.objects.create(fingerprint="f", exc_type="KeyError", title="KeyError: 'sku'", first_seen=s.at(3))
        for i in range(12):
            ExceptionRecord.objects.create(issue=issue, uid=str(i), timestamp=s.at(1), exc_type="KeyError", fingerprint="f")
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, Incident.objects.count()), ("exception", 1))  # upgraded in place
        self.assertIn(f"#{issue.pk}", incident.recommendation)

    def test_security_incident_is_separate(self):
        s = Scenario()
        for m in range(0, 3):
            s.metric("security.events", m, 1, 20, "counter", event="LOGIN_FAILURE")
        for i in range(60):
            SecurityEvent.objects.create(event="LOGIN_FAILURE", timestamp=s.at(1), ip_address="10.0.0.9" if i < 50 else f"10.0.1.{i}")
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.dedup_key.split(":")[0]), ("security", "security"))
        self.assertIn("10.0.0.9", incident.probable_cause)

    def test_graph_and_incident_page(self):
        from django.contrib.auth.models import User

        s = sap_outage()
        incident, = engine.detect(s.now)
        data = graph.incident_graph(incident)
        kinds = {n["kind"] for n in data["nodes"]}
        self.assertTrue({"incident", "external", "metric", "request", "trace"} <= kinds, kinds)
        ids = {n["id"] for n in data["nodes"]}
        self.assertTrue(all(a in ids and b in ids for a, b in data["edges"]))
        self.client.force_login(User.objects.create_superuser("root", password="pw"))
        resp = self.client.get(f"/observability/incidents/{incident.pk}/")
        self.assertContains(resp, "Inference")
        self.assertContains(resp, "observed facts")
        self.assertContains(resp, "not a certainty")
        self.client.post(f"/observability/incidents/{incident.pk}/", {"status": "ACKNOWLEDGED", "notes": "on it"})
        self.assertEqual(Incident.objects.get().status, "ACKNOWLEDGED")


class CausalTests(TestCase):
    def test_why_is_this_slow(self):
        req = RequestRecord(route="/api/materials/", status_code=200, duration_ms=4820, db_ms=120, db_count=2, ext_ms=4620)
        ext = [ExternalCall(service="SAP", host="sap", path="/stock", method="GET", duration_ms=4620)]
        why = causal.explain(req, ext, [], [])
        self.assertEqual(why["question"], "Why is this slow?")
        self.assertEqual([(b["label"], round(b["pct"])) for b in why["breakdown"]],
                         [("SAP API", 96), ("Database", 2), ("Django / application code", 2)])
        self.assertEqual((why["suspect"], why["confidence"]), ("SAP API latency", "HIGH"))

    def test_why_did_this_fail(self):
        s = Scenario()
        rid = s.request(1, status=500, duration=4800, ext_ms=4700, sap=4700, timeout=True)
        for _ in range(4):
            s.request(2, status=500, sap=4700, timeout=True)
        req = RequestRecord.objects.get(request_id=rid)
        exc = ExceptionRecord(exc_type="TimeoutError", function="materials", file_name="views.py", line_number=3, frames=[])
        why = causal.explain(req, list(ExternalCall.objects.filter(request_id=rid)), [], [exc])
        self.assertEqual((why["question"], why["suspect"], why["confidence"]),
                         ("Why did this fail?", "SAP API timeout", "HIGH"))
        self.assertTrue(any("same service failed in 100%" in f for f in why["facts"]), why["facts"])

    def test_n_plus_one_and_healthy(self):
        req = RequestRecord(route="/r/", status_code=200, duration_ms=1500, db_ms=1300, db_count=60)
        queries = [DatabaseQuery(fingerprint="same", duration_ms=20, normalized_sql="SELECT ?") for _ in range(60)]
        self.assertIn("N+1", causal.explain(req, [], queries, [])["suspect"])
        self.assertIsNone(causal.explain(RequestRecord(route="/r/", status_code=200, duration_ms=12), [], [], []))


@override_settings(OBSERVABILITY={**BASE, "ALERTS": {"DEFAULT_RULES": False, "EMAIL_TO": ["ops@example.com"]}})
class AlertTests(TestCase):
    def setUp(self):
        self.s = Scenario()
        self.rule = AlertRule.objects.create(name="High error rate", metric="error_rate", threshold=5, cooldown_minutes=30)

    def fail(self, minutes=(0, 1, 2)):
        for m in minutes:
            self.s.minute(m, errors=10)
        self.s.flush()

    def test_fire_deduplicate_cooldown_resolve(self):
        self.assertEqual(alerts.evaluate(self.s.now), [])  # no data: nothing fires
        self.fail()
        opened = alerts.evaluate(self.s.now)
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("High error rate", mail.outbox[0].subject)
        for i in range(1, 4):  # still failing: same alert, no notification storm
            alerts.evaluate(self.s.now + datetime.timedelta(seconds=i))
        alert = Alert.objects.get()
        self.assertEqual((alert.evaluations, len(mail.outbox), round(alert.value)), (4, 1, 50))
        self.s.now += datetime.timedelta(minutes=31)
        self.fail()
        alerts.evaluate(self.s.now)
        self.assertEqual(len(mail.outbox), 2)  # reminder only after the cooldown
        self.s.now += datetime.timedelta(minutes=30)
        for m in range(0, 5):
            self.s.minute(m)
        self.s.flush()
        alerts.evaluate(self.s.now)
        self.assertEqual(Alert.objects.get().status, "RESOLVED")

    def test_acknowledged_alerts_do_not_renotify_and_incidents_group_alerts(self):
        self.fail()
        alerts.evaluate(self.s.now)
        Alert.objects.update(status="ACKNOWLEDGED")
        self.s.now += datetime.timedelta(minutes=2)
        alerts.evaluate(self.s.now + datetime.timedelta(minutes=45))
        self.assertEqual(len(mail.outbox), 1)
        Alert.objects.all().delete()
        mail.outbox.clear()
        incident, = engine.detect(self.s.now)
        self.assertEqual(len(mail.outbox), 1)  # one message for the incident...
        self.assertIn(f"Incident #{incident.pk}", mail.outbox[0].subject)
        alerts.evaluate(self.s.now)
        self.assertEqual(len(mail.outbox), 1)  # ...and the alert is grouped under it silently
        self.assertEqual(Alert.objects.get().incident_id, incident.pk)

    def test_all_rule_metrics_measure(self):
        self.fail()
        self.s.metric("db.duration", 1, 6000, 3)
        self.s.metric("ext.duration", 1, 900, 10, service="SAP")
        self.s.metric("ext.calls", 1, 1, 6, "counter", service="SAP", outcome="error")
        self.s.metric("ext.calls", 1, 1, 4, "counter", service="SAP", outcome="ok")
        self.s.metric("orders.failed", 1, 1, 7, "counter")
        self.s.flush()
        expect = {"p95_latency": 300, "request_rate": 12, "external_failure_rate": 60, "external_p95": 900,
                  "db_max_ms": 6000, "custom": 7, "exception_count": 0, "issue_occurrences": 0, "slow_query_count": 0,
                  "login_failures": 0}
        for metric, value in expect.items():
            rule = AlertRule(metric=metric, window_minutes=5, custom_metric="orders.failed")
            self.assertAlmostEqual(alerts.measure(rule, self.s.now)[0], value, delta=value * 0.35 + 0.01, msg=metric)

    def test_webhook_failure_is_isolated(self):
        with override_settings(OBSERVABILITY={**BASE, "ALERTS": {"DEFAULT_RULES": False, "WEBHOOK_URL": "http://127.0.0.1:1/x",
                                                                   "EMAIL_TO": ["ops@example.com"]}}):
            self.assertEqual(alerts.notify("s", "b", {}), ["email"])


class MaintenanceAndCommandTests(TestCase):
    def test_retention_is_per_kind_and_batched(self):
        now = timezone.now()
        old = now - datetime.timedelta(days=40)
        for ts in (old, now):
            ObservabilityEvent.objects.bulk_create([ObservabilityEvent(timestamp=ts, message="m") for _ in range(25)])
            SecurityEvent.objects.create(timestamp=ts, event="LOGIN_FAILURE")
            RequestRecord.objects.create(timestamp=ts, method="GET", path="/", request_id=str(ts))
            SpanRecord.objects.create(timestamp=ts, trace_id="t", span_id="s", name="n")
        get_backend().write("audit", [{"timestamp": old, "action": "A"}, {"timestamp": now, "action": "B"}])
        deleted = maintenance.cleanup(batch_size=10)
        self.assertEqual((deleted["logs"], deleted["requests"], deleted["traces"], deleted["security"], deleted["audit"]),
                         (25, 1, 1, 0, 0))  # security/audit are kept longer
        self.assertEqual(ObservabilityEvent.objects.count(), 25)
        with override_settings(OBSERVABILITY={**BASE, "RETENTION": {"audit": 30}}):
            self.assertEqual(maintenance.cleanup(["audit"])["audit"], 1)  # only retention may remove audit rows
        self.assertEqual(AuditEvent.verify_chain(), (1, []))

    def test_commands(self):
        out = io.StringIO()
        call_command("observability_health", stdout=out)
        self.assertIn("Database", out.getvalue())
        self.assertNotIn("UNHEALTHY", out.getvalue())
        call_command("observability_stats", stdout=out)
        call_command("observability_cleanup", stdout=out)
        call_command("observability_test", stdout=out)
        self.assertIn("Result: ", out.getvalue())
        self.assertNotIn("[FAIL]", out.getvalue())
        self.assertEqual(ObservabilityEvent.objects.count(), 0)  # the self test cleans up after itself
        ObservabilityEvent.objects.create(message="token=abc keep", level="ERROR", level_no=40)
        with mock.patch("sys.stdout", new=io.StringIO()) as exported:
            call_command("observability_export", "logs", "--format", "ndjson", "--query", "level:error")
        self.assertIn("keep", exported.getvalue())
        self.assertNotIn("abc", exported.getvalue())
        DatabaseQuery.objects.create(sql="SELECT * FROM t WHERE id = 5", fingerprint="stale", normalized_sql="")
        call_command("observability_rebuild_fingerprints", stdout=out)
        self.assertEqual(DatabaseQuery.objects.get().normalized_sql, "SELECT * FROM t WHERE id = ?")

    def test_scheduler_lease_runs_once_per_interval(self):
        from django_observatory import scheduler

        self.assertTrue(scheduler._claim("lease:test", 60))
        self.assertFalse(scheduler._claim("lease:test", 60))  # a second process loses the election
        with mock.patch("django_observatory.alerts.evaluate", side_effect=RuntimeError("job bug")), \
                mock.patch("django_observatory.correlation.engine.detect") as detect:
            scheduler.tick(force=True)
        detect.assert_called_once()  # one failing job does not block the others


class ServerResourceTests(TestCase):
    """Host CPU / memory / disk monitoring."""

    def window(self):
        end = timezone.now() + datetime.timedelta(minutes=1)
        return end - datetime.timedelta(minutes=10), end

    def test_sampling_works_without_psutil(self):
        from django_observatory import system

        with mock.patch.object(system, "psutil", None), mock.patch.object(system, "_prev_cpu", None):
            self.assertIsNone(system.cpu_percent())  # first reading has nothing to compare with
            sum(i * i for i in range(200000))
            s = system.sample()
        self.assertEqual(s["source"], "stdlib")
        self.assertTrue(0 <= s["cpu"] <= 100)
        self.assertTrue(0 < s["memory"]["percent"] <= 100 and s["memory"]["total_mb"] > s["memory"]["used_mb"] > 0)
        self.assertTrue(s["disks"] and all(0 <= d["percent"] <= 100 and d["total_gb"] > 0 for d in s["disks"]))
        self.assertEqual(len({d["mount"] for d in s["disks"]}), len(s["disks"]))  # volumes de-duplicated

    def test_unreadable_platform_reports_nothing_and_never_raises(self):
        from django_observatory import system

        with mock.patch.object(system, "psutil", None), mock.patch.object(system.sys, "platform", "plan9"), \
                mock.patch.object(system.shutil, "disk_usage", side_effect=OSError("denied")):
            s = system.sample()
            self.assertIsNotNone(system.record())
        self.assertEqual((s["cpu"], s["memory"], s["disks"]), (None, None, []))

    def test_readings_are_stored_from_the_suppressed_worker_context(self):
        from django_observatory import context, system

        with context.suppressed():  # the scheduler runs inside the worker, where suppression is on
            system.record()
            system.record()
            metrics.flush()
        start, end = self.window()
        w = system.worst(start, end)
        self.assertTrue(0 <= w["memory"] <= 100 and 0 <= w["disk"] <= 100 and w["disk_mount"])
        self.assertEqual(w["memory_host"], system.HOST)
        self.assertGreater(metrics.total("process.threads", start, end).count, 0)

    @override_settings(OBSERVABILITY={**BASE, "SYSTEM": {"DISKS": ["/"]}})
    def test_configured_disks_and_disable(self):
        from django_observatory import system

        self.assertEqual(system.disk_paths(), ["/"])
        with override_settings(OBSERVABILITY={**BASE, "SYSTEM": {"ENABLED": False}}):
            self.assertIsNone(system.record())

    def _gauge(self, s, name, minutes, value, **labels):
        s.rows.append({"name": name, "kind": "gauge", "timestamp": s.at(minutes), "resolution": 60, "labels": labels,
                       "labels_key": ",".join(f"{k}={v}" for k, v in sorted(labels.items())), "count": 1,
                       "sum": value, "min": value, "max": value, "last": value, "buckets": None})

    def test_alert_rules_for_resources(self):
        s = Scenario()
        for m in range(0, 5):
            self._gauge(s, "system.disk_percent", m, 96.5, host="srv1", mount="D:\\")
            self._gauge(s, "system.disk_percent", m, 40.0, host="srv1", mount="C:\\")
            self._gauge(s, "system.cpu_percent", m, 35.0, host="srv1")
            self._gauge(s, "system.memory_percent", m, 93.0, host="srv1")
        s.flush()
        with override_settings(OBSERVABILITY={**BASE, "ALERTS": {"DEFAULT_RULES": True}}):
            opened = {a.title: a for a in alerts.evaluate(s.now)}
        self.assertEqual(set(opened), {"Disk almost full", "Server memory pressure"})  # CPU at 35% stays quiet
        self.assertIn("D:\\", opened["Disk almost full"].message)
        self.assertEqual(round(opened["Disk almost full"].value, 1), 96.5)
        self.assertTrue(AlertRule.objects.filter(metric="cpu_percent").exists())

    def test_existing_installations_gain_the_server_rules_once(self):
        Cfg.objects.create(key="defaults:alert_rules")  # installed before server monitoring existed
        with override_settings(OBSERVABILITY={**BASE, "ALERTS": {"DEFAULT_RULES": True}}):
            alerts.ensure_default_rules()
            AlertRule.objects.filter(name="Disk almost full").delete()  # operator removed it
            alerts.ensure_default_rules()
        self.assertEqual(set(AlertRule.objects.values_list("name", flat=True)),
                         {"Server memory pressure", "Server CPU saturated"})

    def test_incident_names_the_exhausted_resource(self):
        s = Scenario()
        for m in range(6, 60):
            s.minute(m)
            self._gauge(s, "system.cpu_percent", m, 22.0, host="srv1")
        for m in range(0, 4):
            s.minute(m, latency=4000, errors=6)
            self._gauge(s, "system.cpu_percent", m, 98.0, host="srv1")
            for _ in range(5):
                s.request(m, status=500, duration=4000)
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual((incident.cause_kind, incident.confidence), ("resources", "HIGH"))
        self.assertIn("CPU saturation on server srv1", incident.probable_cause)
        cause = incident.signals.get(relationship="cause")
        self.assertEqual((cause.kind, round(cause.baseline), round(cause.current)), ("resource_cpu", 22, 98))

    def test_full_disk_alone_opens_an_incident(self):
        s = Scenario()
        self._gauge(s, "system.disk_percent", 1, 99.0, host="srv1", mount="/data")
        s.flush()
        incident, = engine.detect(s.now)
        self.assertEqual(incident.cause_kind, "resources")
        self.assertIn("Disk almost full", incident.title)
        self.assertIn("Free disk space", incident.recommendation)
        self.assertNotEqual(incident.confidence, "HIGH")  # no user impact observed: stated honestly

    def test_external_cause_still_wins_when_resources_are_healthy(self):
        s = sap_outage()
        self._gauge(s, "system.cpu_percent", 1, 30.0, host="srv1")
        s.flush()
        self.assertEqual(engine.detect(s.now)[0].cause_kind, "external")

    def test_page_dashboard_and_health(self):
        from django.contrib.auth.models import User

        from django_observatory import health

        self.client.force_login(User.objects.create_superuser("root", password="pw"))
        resp = self.client.get("/observability/server/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Memory")
        self.assertContains(resp, "Disk space")
        self.assertContains(resp, 'data-chart="s-mem"')
        self.assertContains(self.client.get("/observability/"), "Disk used")
        check = next(c for c in health.report()["checks"] if c["name"] == "Server Resources")
        self.assertIn("disk", check["detail"])
        staff = User.objects.create_user("plain", password="pw")
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/observability/server/").status_code, 403)
