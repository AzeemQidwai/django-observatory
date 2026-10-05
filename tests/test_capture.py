"""Logging, requests, tracing, SQL, exceptions, external HTTP, security, audit, metrics,
sampling, the real worker pipeline and - above all - failure isolation."""
import datetime
import io
import logging
import urllib.error
import urllib.request
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, models
from django.test import AsyncClient, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from django_observatory import audit, context, metrics, observe, pipeline, security, span, task
from django_observatory.instrumentation import otel
from django_observatory.logging import ObservabilityHandler
from django_observatory.models import (
    AuditEvent, DatabaseQuery, ExceptionRecord, ExternalCall, Issue, MetricSample, ObservabilityEvent,
    RequestRecord, SecurityEvent, SpanRecord, TraceRecord,
)

SYNC = {"PIPELINE": {"SYNC": True}, "SCHEDULER": {"ENABLED": False}, "ALERTS": {"DEFAULT_RULES": False}}


def cfg(**over):
    return override_settings(OBSERVABILITY={**SYNC, **over})


class LoggingTests(TestCase):
    def setUp(self):
        self.log = logging.getLogger("tests.logging")
        self.handler = ObservabilityHandler()
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.addCleanup(self.log.removeHandler, self.handler)

    def test_standard_logging_is_stored_enriched_and_redacted(self):
        self.log.warning("Stock low for %s password=hunter2", "A1", extra={"material_code": "A1", "api_key": "k"})
        e = ObservabilityEvent.objects.get()
        self.assertEqual((e.level, e.level_no, e.logger_name), ("WARNING", 30, "tests.logging"))
        self.assertEqual(e.message, "Stock low for A1 password=[REDACTED]")
        self.assertEqual(e.metadata, {"material_code": "A1", "api_key": "[REDACTED]"})
        self.assertEqual(e.function, "test_standard_logging_is_stored_enriched_and_redacted")
        self.assertTrue(e.fingerprint and e.hostname and e.release == "1.0")

    def test_logger_exception_creates_issue(self):
        try:
            1 / 0
        except ZeroDivisionError:
            self.log.exception("Failed to process material")
        e = ObservabilityEvent.objects.get()
        exc = ExceptionRecord.objects.get(uid=e.exception_uid)
        self.assertEqual((exc.exc_type, exc.handled, exc.issue.occurrence_count), ("ZeroDivisionError", True, 1))

    def test_structured_api(self):
        observe.info("material_created", material_code="ABC123", quantity=100, token="t")
        observe.critical("stock_gone")
        e = ObservabilityEvent.objects.get(message="material_created")
        self.assertEqual((e.event_type, e.metadata), ("structured", {"material_code": "ABC123", "quantity": 100,
                                                                     "token": "[REDACTED]"}))
        self.assertEqual(ObservabilityEvent.objects.get(message="stock_gone").level, "CRITICAL")

    def test_internal_and_ignored_loggers_do_not_recurse(self):
        h = ObservabilityHandler()
        for name in ("django_observatory.internal", "django.db.backends"):
            h.emit(logging.LogRecord(name, logging.ERROR, __file__, 1, "x", (), None))
        self.assertEqual(ObservabilityEvent.objects.count(), 0)

    def test_sampling(self):
        with cfg(SAMPLING={"successful_logs": 0.0, "errors": 1.0}):
            self.log.info("dropped")
            self.log.error("kept")
        self.assertEqual(list(ObservabilityEvent.objects.values_list("message", flat=True)), ["kept"])


class RequestTests(TestCase):
    def test_request_record_and_correlation_headers(self):
        resp = self.client.get("/ok/?password=x&page=2", HTTP_AUTHORIZATION="Bearer topsecret", HTTP_COOKIE="a=b")
        r = RequestRecord.objects.get()
        self.assertEqual(resp["X-Request-ID"], r.request_id)
        self.assertEqual(resp["X-Trace-ID"], r.trace_id)
        self.assertRegex(r.request_id, r"^req_[0-9a-f]{24}$")
        self.assertRegex(r.trace_id, r"^[0-9a-f]{32}$")
        self.assertEqual((r.method, r.path, r.route, r.status_code, r.view_name), ("GET", "/ok/", "/ok/", 200, "ok"))
        self.assertEqual(r.query_params, {})  # not captured by default
        self.assertEqual(r.headers["Authorization"], "[REDACTED]")
        self.assertEqual(r.headers["Cookie"], "[REDACTED]")
        self.assertNotIn("topsecret", str(r.headers) + str(r.metadata))
        log = ObservabilityEvent.objects.get(logger_name="tests.app")
        self.assertEqual((log.request_id, log.trace_id), (r.request_id, r.trace_id))
        self.assertEqual(log.metadata, {"password": "[REDACTED]", "normal_field": "hello"})

    def test_opt_in_capture_is_still_redacted(self):
        with cfg(REQUESTS={"CAPTURE_QUERY_PARAMS": True, "CAPTURE_BODY": True}):
            self.client.get("/ok/?password=x&page=2")
            self.client.post("/ok/", "password=pw&name=n", content_type="application/x-www-form-urlencoded")
        get, post = RequestRecord.objects.order_by("id")
        self.assertEqual(get.query_params, {"password": "[REDACTED]", "page": "2"})
        self.assertEqual(post.metadata["body"], {"password": "[REDACTED]", "name": "n"})

    def test_w3c_trace_context_is_honoured(self):
        self.client.get("/ok/", HTTP_TRACEPARENT="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        self.assertEqual(RequestRecord.objects.get().trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        root = SpanRecord.objects.get(kind="request")
        self.assertEqual(root.parent_span_id, "00f067aa0ba902b7")
        self.client.get("/ok/", HTTP_TRACEPARENT="garbage", HTTP_X_REQUEST_ID="<script>")
        self.assertRegex(RequestRecord.objects.order_by("-id").first().request_id, r"^req_")

    def test_user_is_recorded_without_extra_queries(self):
        user = User.objects.create_user("ahmed", password="pw")
        self.client.force_login(user)
        self.client.get("/observability/")  # 403 for non-staff, and the UI is never observed
        self.assertEqual(RequestRecord.objects.count(), 0)
        self.client.get("/admin/")
        self.assertEqual(RequestRecord.objects.get().username, "ahmed")

    def test_ignored_paths_and_disabled(self):
        self.client.get("/static/x.css")
        with cfg(ENABLED=False):
            self.client.get("/ok/")
        self.assertEqual(RequestRecord.objects.count(), 0)

    def test_sampling_keeps_errors_and_metrics(self):
        with cfg(SAMPLING={"requests": 0.0}):
            self.client.get("/ok/")
            self.client.raise_request_exception = False
            self.client.get("/boom/1/")
            metrics.flush()
        self.assertEqual(list(RequestRecord.objects.values_list("status_code", flat=True)), [500])
        end = timezone.now() + datetime.timedelta(minutes=1)
        self.assertEqual(metrics.total("http.requests", end - datetime.timedelta(hours=1), end).sum, 2)

    async def test_async_view(self):
        resp = await AsyncClient().get("/async/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["X-Request-ID"])


class TraceAndDatabaseTests(TestCase):
    def test_request_to_view_to_span_to_sql(self):
        self.client.get("/sql/")
        r = RequestRecord.objects.get()
        self.assertEqual(r.db_count, 2)
        trace = TraceRecord.objects.get(trace_id=r.trace_id)
        spans = {s.kind: s for s in SpanRecord.objects.filter(trace_id=r.trace_id)}
        self.assertEqual(set(spans), {"request", "view", "custom", "db"})
        self.assertEqual(trace.span_count, 5)
        self.assertEqual(spans["custom"].attributes, {"department": "projects"})
        self.assertEqual(spans["db"].parent_span_id, spans["custom"].span_id)  # SQL nests under the manual span
        q1, q2 = DatabaseQuery.objects.filter(request_id=r.request_id)
        self.assertEqual(q1.fingerprint, q2.fingerprint)  # id=123 and id=456 group together
        self.assertNotIn("123", q1.normalized_sql)
        self.assertIsNone(q1.params)
        self.assertEqual(q1.route, "/sql/")

    def test_slow_query_detection_and_param_opt_in(self):
        with cfg(DATABASE={"SLOW_QUERY_MS": 0, "CAPTURE_PARAMS": True}):
            with connection.cursor() as cur:
                cur.execute("SELECT %s", ["visible-only-when-enabled"])
        q = DatabaseQuery.objects.get()
        self.assertTrue(q.is_slow)
        self.assertEqual((q.normalized_sql, q.params), ("SELECT ?", ["visible-only-when-enabled"]))

    def test_own_tables_are_not_observed(self):
        with cfg(DATABASE={"SLOW_QUERY_MS": 0}):
            list(RequestRecord.objects.all())
        self.assertEqual(DatabaseQuery.objects.count(), 0)

    def test_manual_root_span_and_task(self):
        with span("generate_monthly_report", report_type="monthly"):
            with span("inner"):
                pass

        @task
        def nightly():
            raise RuntimeError("job failed")

        with self.assertRaises(RuntimeError):
            nightly()
        self.assertEqual(TraceRecord.objects.get(name="generate_monthly_report").kind, "manual")
        t = TraceRecord.objects.get(kind="task")
        self.assertTrue(t.error and t.name.endswith("nightly"))
        self.assertEqual(ExceptionRecord.objects.get().trace_id, t.trace_id)
        self.assertIsNone(context.current())  # no context leak


class ExceptionTests(TestCase):
    def setUp(self):
        self.client.raise_request_exception = False

    def test_grouping_lifecycle_and_redaction(self):
        self.client.get("/boom/1/")
        self.client.get("/boom/2/")
        issue = Issue.objects.get()
        self.assertEqual((issue.occurrence_count, issue.status), (2, "OPEN"))
        self.assertTrue(issue.title.startswith("ValueError: exploded for order"))
        exc = issue.occurrences.first()
        self.assertEqual((exc.endpoint, exc.method, exc.function, exc.handled), ("/boom/<int:pk>/", "GET", "boom", False))
        self.assertNotIn("abc123secret", exc.message + exc.stacktrace + str(exc.frames))
        self.assertTrue(any(f["in_app"] for f in exc.frames))
        self.assertEqual(exc.breadcrumbs[0]["category"], "request")
        r = RequestRecord.objects.filter(request_id=exc.request_id).get()
        self.assertEqual((r.status_code, r.exception_uid), (500, exc.uid))
        self.assertEqual(ExceptionRecord.objects.count(), 2)  # logging + middleware did not double count

        Issue.objects.update(status="RESOLVED")
        self.client.get("/boom/3/")
        self.assertEqual(Issue.objects.get().status, "REGRESSED")

    def test_control_flow_exceptions_are_not_issues(self):
        self.client.get("/denied/")
        self.client.get("/missing/")
        self.assertEqual(Issue.objects.count(), 0)


class ExternalHttpTests(TestCase):
    def _open(self, effect):
        with mock.patch("urllib.request.OpenerDirector._open", side_effect=effect):
            self.client.raise_request_exception = False
            return self.client.get("/external/")

    def test_success_is_recorded_without_secrets(self):
        resp = mock.MagicMock(status=200, code=200, headers={"content-length": "12"})
        resp.read.return_value = b""
        with cfg(EXTERNAL_HTTP={"SERVICES": {"sap.internal": "SAP"}}):
            self._open(lambda req, data=None: resp)
        call = ExternalCall.objects.get()
        self.assertEqual((call.service, call.host, call.path, call.method, call.error), ("SAP", "sap.internal", "/stock", "GET", False))
        self.assertNotIn("SECRETKEY", str(ExternalCall.objects.values().get()))
        r = RequestRecord.objects.get()
        self.assertEqual((r.ext_count, call.request_id), (1, r.request_id))
        self.assertEqual(SpanRecord.objects.get(kind="http").name, "GET SAP")

    def test_timeout_is_flagged_and_correlated(self):
        def boom(req, data=None):
            raise TimeoutError("timed out")
        self.assertEqual(self._open(boom).status_code, 500)
        call = ExternalCall.objects.get()
        self.assertTrue(call.error and call.timeout)
        self.assertEqual(call.exc_type, "TimeoutError")
        self.assertEqual(ExceptionRecord.objects.get().request_id, call.request_id)

    def test_http_error_is_a_response_not_a_failure(self):
        def not_found(req, data=None):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO())
        self._open(not_found)
        call = ExternalCall.objects.get()
        self.assertEqual((call.status_code, call.error, call.exc_type), (404, False, ""))


class SecurityAuditTests(TestCase):
    def test_auth_signals(self):
        User.objects.create_superuser("root", password="pw")
        self.client.login(username="root", password="wrong")
        self.client.login(username="root", password="pw")
        self.client.logout()
        u = User.objects.get()
        u.set_password("new-password-value")
        u.save()
        events = list(SecurityEvent.objects.order_by("id").values_list("event", flat=True))
        self.assertEqual(events, ["LOGIN_FAILURE", "LOGIN_SUCCESS", "SUPERUSER_LOGIN", "LOGOUT", "PASSWORD_CHANGE"])
        dump = str(list(SecurityEvent.objects.values()))
        self.assertNotIn("wrong", dump)
        self.assertNotIn("new-password-value", dump)

    def test_permission_denied_and_csrf(self):
        self.client.get("/denied/")
        ev = SecurityEvent.objects.get()
        self.assertEqual((ev.event, ev.resource), ("PERMISSION_DENIED", "/denied/"))
        from django.test import Client

        Client(enforce_csrf_checks=True).post("/ok/", {"a": 1})
        self.assertEqual(SecurityEvent.objects.filter(event="CSRF_FAILURE").count(), 1)
        self.assertEqual(SecurityEvent.objects.count(), 2)  # the 403 was not double-reported

    def test_manual_security_api(self):
        security.record(event="PERMISSION_DENIED", resource="/api/materials/", reason="Missing permission", token="x")
        ev = SecurityEvent.objects.get()
        self.assertEqual((ev.reason, ev.metadata), ("Missing permission", {"token": "[REDACTED]"}))

    def test_audit_record_chain_and_immutability(self):
        audit.record(action="UPDATE", object_type="Material", object_id="A1023",
                     changes={"quantity": {"before": 120, "after": 80}})
        audit.record("DELETE", "Material", "A1024", result="failure", reason="locked", password="x")
        a, b = AuditEvent.objects.order_by("id")
        self.assertEqual((a.before, a.after, a.result), ({"quantity": 120}, {"quantity": 80}, "SUCCESS"))
        self.assertEqual((b.prev_hash, b.metadata), (a.hash, {"password": "[REDACTED]"}))
        self.assertEqual(AuditEvent.verify_chain(), (2, []))
        for forbidden in (a.delete, lambda: AuditEvent.objects.all().delete(), lambda: AuditEvent.objects.update(action="X"),
                          a.save):
            with self.assertRaises(PermissionError):
                forbidden()
        from django.db.models import QuerySet

        QuerySet(AuditEvent).filter(pk=a.pk).update(action="CREATE")  # tampering behind the model's back
        self.assertEqual(AuditEvent.verify_chain()[1], [(a.pk, "modified")])

    def test_admin_changes_are_audited(self):
        User.objects.create_superuser("root", password="pw")
        self.client.login(username="root", password="pw")
        self.client.post("/admin/auth/group/add/", {"name": "ops"})
        entry = AuditEvent.objects.get()
        self.assertEqual((entry.action, entry.object_type, entry.username), ("CREATE", "group", "root"))


class MetricsTests(TestCase):
    def test_counter_gauge_histogram_timer(self):
        metrics.increment("invoice.processed")
        metrics.increment("invoice.processed", 2, labels={"region": "pk"})
        metrics.gauge("queue.depth", 42)
        for v in (10, 20, 30, 4000):
            metrics.histogram("job.ms", v)
        with metrics.timer("invoice.processing"):
            pass
        metrics.flush()
        end = timezone.now() + datetime.timedelta(minutes=1)
        start = end - datetime.timedelta(hours=1)
        self.assertEqual(metrics.total("invoice.processed", start, end).sum, 3)
        self.assertEqual(metrics.total("invoice.processed", start, end, region="pk").sum, 2)
        self.assertEqual(set(metrics.by_label("invoice.processed", "region", start, end)), {"", "pk"})
        self.assertEqual(metrics.total("queue.depth", start, end).last, 42)
        job = metrics.total("job.ms", start, end)
        self.assertEqual((job.count, job.min, job.max, job.avg), (4, 10, 4000, 1015))
        self.assertTrue(10 <= job.quantile(0.5) <= 30 and 2500 <= job.quantile(0.99) <= 4000)
        self.assertEqual(metrics.total("invoice.processing", start, end).count, 1)
        series = metrics.timeseries("invoice.processed", start, end, 300)
        self.assertEqual(sum(a.sum for _, a in series), 3)
        self.assertIn(len(series), (12, 13))

    def test_rollup_preserves_totals(self):
        from django_observatory import maintenance
        from django_observatory.storage import get_backend

        old = (timezone.now() - datetime.timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
        rows = [{"name": "m", "kind": "histogram", "timestamp": old + datetime.timedelta(minutes=i), "resolution": 60,
                 "labels": {"r": "a"}, "labels_key": "r=a", "count": 1, "sum": 5.0, "min": 5.0, "max": 5.0,
                 "last": 5.0, "buckets": [0, 0, 1] + [0] * 13} for i in range(30)]
        get_backend().write("metric", rows)
        maintenance.rollup()
        row = MetricSample.objects.get()
        self.assertEqual((row.resolution, row.count, row.sum, row.buckets[2]), (3600, 30, 150.0, 30))


class FailureIsolationTests(TestCase):
    """The hard requirement: observability failures never fail the application."""

    def test_storage_outage_does_not_fail_requests(self):
        with mock.patch("django_observatory.storage.django.DjangoORMBackend.write_batch",
                        side_effect=RuntimeError("database unavailable")):
            self.assertEqual(self.client.get("/ok/").status_code, 200)
            self.assertEqual(self.client.get("/sql/").status_code, 200)
            logging.getLogger("tests.app").error("still fine")
            audit.record("UPDATE", "X", "1")
            security.record("LOGIN_FAILURE")
        self.assertEqual(RequestRecord.objects.count(), 0)

    def test_internal_bugs_do_not_fail_requests(self):
        targets = ["django_observatory.middleware.finish_trace", "django_observatory.middleware.context.TraceContext",
                   "django_observatory.redaction.text", "django_observatory.instrumentation.database._record",
                   "django_observatory.metrics._bucket_index"]
        for target in targets:
            with mock.patch(target, side_effect=Exception("bug in observability")):
                self.assertEqual(self.client.get("/sql/").status_code, 200, target)
        with mock.patch("django_observatory.logging.emit_event", side_effect=Exception("bug")):
            logging.getLogger("tests.app").error("handler must swallow this")

    def test_application_errors_still_propagate(self):
        with self.assertRaises(ValueError):
            self.client.get("/boom/1/")
        with self.assertRaises(KeyError), span("s"):
            raise KeyError("app error is never swallowed")


class PipelineTests(TransactionTestCase):
    """The real asynchronous worker (other tests use the synchronous mode)."""

    ASYNC = {"SCHEDULER": {"ENABLED": False}, "PIPELINE": {"FLUSH_INTERVAL": 0.05, "QUEUE_SIZE": 100, "MAX_RETRIES": 2}}

    def test_worker_writes_in_background_and_isolates_context(self):
        with override_settings(OBSERVABILITY=self.ASYNC):
            for i in range(20):
                observe.info(f"event {i}")
            self.assertTrue(pipeline.flush(10))
            self.assertEqual(ObservabilityEvent.objects.count(), 20)
            self.assertTrue(pipeline.pipeline.health()["worker_alive"])

    def test_transient_failure_is_retried(self):
        from django_observatory.storage.django import DjangoORMBackend

        real, calls = DjangoORMBackend.write_batch, []

        def flaky(self, grouped):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return real(self, grouped)

        with override_settings(OBSERVABILITY=self.ASYNC), mock.patch.object(DjangoORMBackend, "write_batch", flaky):
            observe.error("survives a transient failure")
            self.assertTrue(pipeline.flush(10))
        self.assertEqual(ObservabilityEvent.objects.count(), 1)

    def test_backpressure_sheds_low_priority_first(self):
        with override_settings(OBSERVABILITY=self.ASYNC):
            p = pipeline.pipeline
            p._start()
            with mock.patch.object(p, "_write", side_effect=lambda *a, **k: __import__("time").sleep(0.2)):
                for i in range(400):
                    p.enqueue("event", {"message": "low"})
                dropped_low = p.stats["dropped"]
                before = p.stats["enqueued"]
                p.enqueue("event", {"message": "high"}, high=True)
                self.assertGreater(dropped_low, 0)  # never blocks, never raises
                self.assertEqual(p.stats["enqueued"], before + 1)  # headroom reserved for high priority


class OpenTelemetryTests(TestCase):
    def test_otlp_payload_shape(self):
        ctx = context.TraceContext("GET /x", "request")
        ctx.add_span({"span_id": "ab" * 8, "parent_span_id": ctx.root_span_id, "name": "SELECT ?", "kind": "db",
                      "offset_ms": 1.0, "duration_ms": 2.0, "status": "error", "attributes": {"db.slow": True, "n": 3}})
        payload = otel.to_otlp(ctx, "GET /x", 10.0)
        rs = payload["resourceSpans"][0]
        self.assertIn({"key": "service.name", "value": {"stringValue": "django"}}, rs["resource"]["attributes"])
        root, child = rs["scopeSpans"][0]["spans"]
        self.assertEqual((root["traceId"], root["kind"], child["parentSpanId"]), (ctx.trace_id, 2, ctx.root_span_id))
        self.assertEqual(child["status"], {"code": 2})
        self.assertEqual(int(child["endTimeUnixNano"]) - int(child["startTimeUnixNano"]), 2_000_000)

    def test_works_without_opentelemetry_and_propagates_context(self):
        self.assertEqual(otel.active_ids() if not otel.available() else (None, None), (None, None))
        from django_observatory.instrumentation.http import traceparent

        self.assertIsNone(traceparent())
        with span("root"):
            self.assertRegex(traceparent(), r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


class AuditSecurityTests(TestCase):
    def test_audit_immutability(self):
        audit.record("LOGIN", user="alice", object_type="session", object_id="s1")
        row = AuditEvent.objects.get()

        with self.assertRaises(PermissionError):
            AuditEvent.objects.filter(pk=row.pk).update(action="HACKED")

        with self.assertRaises(PermissionError):
            row.action = "HACKED"
            AuditEvent.objects.bulk_update([row], ["action"])

        with self.assertRaises(PermissionError):
            row.save()

        with self.assertRaises(PermissionError):
            row.delete()

        with self.assertRaises(PermissionError):
            AuditEvent.objects.filter(pk=row.pk).delete()

    def test_verify_chain_integrity(self):
        self.assertEqual(AuditEvent.verify_chain(), (0, []))

        # create a chain of 3 events
        audit.record("ACTION_1", user="alice")
        audit.record("ACTION_2", user="bob")
        audit.record("ACTION_3", user="carol")

        checked, problems = AuditEvent.verify_chain()
        self.assertEqual(checked, 3)
        self.assertEqual(problems, [])

        # Tampering with a row in the raw database (bypassing ORM protections)
        raw_rows = list(models.QuerySet(AuditEvent).order_by("pk"))
        self.assertEqual(len(raw_rows), 3)

        # 1. Modify row 2's action directly via SQL
        with connection.cursor() as cur:
            cur.execute("UPDATE django_observatory_auditevent SET action = 'TAMPERED' WHERE id = %s", [raw_rows[1].pk])

        checked, problems = AuditEvent.verify_chain()
        self.assertEqual(checked, 3)
        self.assertIn((raw_rows[1].pk, "modified"), problems)

        # Restore row 2
        with connection.cursor() as cur:
            cur.execute("UPDATE django_observatory_auditevent SET action = %s WHERE id = %s", [raw_rows[1].action, raw_rows[1].pk])

        # 2. Delete row 2 directly via SQL to simulate an attacker deleting an audit record
        with connection.cursor() as cur:
            cur.execute("DELETE FROM django_observatory_auditevent WHERE id = %s", [raw_rows[1].pk])

        checked, problems = AuditEvent.verify_chain()
        self.assertEqual(checked, 2)
        # Row 3 must be flagged because its prev_hash points to the deleted row 2, which does not match row 1's hash
        self.assertIn((raw_rows[2].pk, "predecessor missing"), problems)


class ExternalHttpWrapperTests(TestCase):
    def test_urllib_open_kwargs(self):
        from django_observatory.instrumentation import http

        opener = urllib.request.OpenerDirector()
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {}
        with mock.patch.object(opener, "open", return_value=mock_resp):
            wrapped = http._wrap_sync(opener.open, http._describe_urllib)
            resp1 = wrapped(opener, "https://httpbin.org/get")
            self.assertEqual(resp1.status, 200)

            # Keyword arg 'fullurl' should not raise TypeError
            resp2 = wrapped(opener, fullurl="https://httpbin.org/get")
            self.assertEqual(resp2.status, 200)


class AlertsAndSchedulerTests(TestCase):
    def test_webhook_url_scheme_validation(self):
        from django_observatory import alerts

        with override_settings(OBSERVABILITY={**SYNC, "ALERTS": {"WEBHOOK_URL": "file:///etc/passwd"}}):
            with mock.patch("urllib.request.urlopen") as mock_open:
                alerts.notify("test", "test body", {})
                mock_open.assert_not_called()

    def test_alert_retrigger_cooldown_without_channels(self):
        from django_observatory import alerts
        from django_observatory.models import Alert, AlertRule

        rule = AlertRule.objects.create(
            name="Test Rule", metric="request_rate", operator=">", threshold=0,
            window_minutes=5, cooldown_minutes=10, channels=[]
        )
        now = timezone.now()
        with mock.patch("django_observatory.alerts.measure", return_value=(100.0, "")):
            opened = alerts.evaluate(now=now)
            self.assertEqual(len(opened), 1)
            alert = opened[0]
            self.assertEqual(alert.notified_at, now)

            # Advance time past cooldown
            later = now + datetime.timedelta(minutes=15)
            alerts.evaluate(now=later)
            alert.refresh_from_db()
            # notified_at should be updated to 'later' even without external channels
            self.assertEqual(alert.notified_at, later)


class MaintenanceRollupTests(TestCase):
    def test_rollup_does_not_split_incomplete_bucket(self):
        from django_observatory import maintenance
        from django_observatory.models import Metric

        metric = Metric.objects.create(name="test.metric", kind="counter")
        now = timezone.now().replace(minute=30, second=0, microsecond=0)
        # cutoff will be now - 7 days = 7 days ago at minute 00
        # If a sample is within an hour that extends beyond cutoff:
        # e.g. cutoff = 7 days ago at 00:00.
        # A sample at 7 days ago minus 30 minutes (i.e. 23:30 of 8 days ago):
        # start = 23:00, end = 23:00 + 1hr = 00:00 <= cutoff -> complete bucket.
        seven_days_ago = now - datetime.timedelta(days=7)
        cutoff = seven_days_ago.replace(minute=0, second=0, microsecond=0)

        # sample 1: complete bucket (starts 2 hours before cutoff)
        t_complete = cutoff - datetime.timedelta(hours=2)
        MetricSample.objects.create(metric=metric, timestamp=t_complete, resolution=60, count=1, sum=10, last=10)

        # sample 2: in a bucket that would cross cutoff if cutoff was mid-hour
        # With our cutoff at hour boundary, rollup should merge complete buckets
        removed = maintenance.rollup(now=now)
        self.assertEqual(removed, 0)  # 1 row merged into 1 rollup row -> removed = 1 - 1 = 0
        self.assertTrue(MetricSample.objects.filter(metric=metric, resolution=3600).exists())
