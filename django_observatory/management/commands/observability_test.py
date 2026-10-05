"""End-to-end self test against the real configured storage. Everything it writes is
tagged and removed afterwards."""
import logging
import sys
import uuid

from django.core.management.base import BaseCommand
from django.db import connection
from django.test import RequestFactory

from django_observatory import alerts, audit, conf, context, metrics, observe, pipeline, redaction, security
from django_observatory.correlation import engine
from django_observatory.instrumentation import otel
from django_observatory.instrumentation.exceptions import capture_exception
from django_observatory.middleware import ObservabilityMiddleware
from django_observatory.models import (
    AuditEvent, DatabaseQuery, ExceptionRecord, Issue, MetricSample, ObservabilityEvent, RequestRecord,
    SecurityEvent, SpanRecord, TraceRecord,
)
from django_observatory.tracing import span


class Command(BaseCommand):
    help = "Verify that every part of the observability pipeline works in this environment."

    def handle(self, *args, **opts):
        tag = "obs-selftest-" + uuid.uuid4().hex[:10]
        results = []

        def step(name, fn):
            try:
                ok, detail = fn(), ""
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(ok)
            mark = self.style.SUCCESS("[PASS]") if ok else self.style.ERROR("[FAIL]")
            self.stdout.write(f"{mark} {name}" + (f"  ({detail})" if detail else ""))

        def flush():
            metrics.flush()
            return pipeline.flush(10)

        self.stdout.write("Django Observability Self Test\n")
        step("Configuration", lambda: bool(conf.all()["ENABLED"]))

        def logging_():
            logging.getLogger("selftest.observability").info("selftest %s password=hunter2", tag)
            observe.info(tag, token="abc123", normal_field="hello")
            flush()
            return ObservabilityEvent.objects.filter(message__contains=tag).count() >= 2
        step("Logging + storage + queue", logging_)

        def redaction_():
            ev = ObservabilityEvent.objects.filter(message=tag).first()
            stored = "".join(ObservabilityEvent.objects.filter(message__contains=tag).values_list("message", flat=True))
            clean = redaction.clean({"password": "x", "authorization": "Bearer xyz", "normal_field": "hello"})
            return ("hunter2" not in stored and ev.metadata["token"] == redaction.REDACTED
                    and clean == {"password": redaction.REDACTED, "authorization": redaction.REDACTED,
                                  "normal_field": "hello"})
        step("Redaction", redaction_)
        step("Sampling", lambda: all(0 <= v <= 1 for v in conf.get("SAMPLING").values()))

        state = {}

        def request_():
            def view(request):
                with connection.cursor() as cur:
                    cur.execute("SELECT %s", [12345])
                with span("selftest-span", marker=tag):
                    pass
                raise RuntimeError(tag)

            request = RequestFactory().get(f"/{tag}/")
            mw = ObservabilityMiddleware(view)
            try:
                mw(request)
            except RuntimeError:
                pass
            state["rid"] = request.observability.request_id
            state["tid"] = request.observability.trace_id
            flush()
            return RequestRecord.objects.filter(request_id=state["rid"], status_code=500).exists()
        step("Request instrumentation", request_)
        step("Tracing", lambda: TraceRecord.objects.filter(trace_id=state["tid"]).exists()
             and SpanRecord.objects.filter(trace_id=state["tid"], name="selftest-span").exists())
        step("Database instrumentation",
             lambda: DatabaseQuery.objects.filter(request_id=state["rid"], normalized_sql="SELECT ?").exists())
        step("Exception handling + issue grouping",
             lambda: ExceptionRecord.objects.filter(request_id=state["rid"], issue__title__contains=tag).exists())

        def metrics_():
            metrics.increment(tag)
            with metrics.timer(tag + ".timer"):
                pass
            flush()
            return MetricSample.objects.filter(metric__name=tag).exists()
        step("Metrics", metrics_)

        def security_():
            security.record("PERMISSION_DENIED", resource=f"/{tag}/", reason="self test")
            flush()
            return SecurityEvent.objects.filter(resource=f"/{tag}/").exists()
        step("Security", security_)

        def audit_():
            audit.record("UPDATE", "SelfTest", tag, changes={"quantity": {"before": 120, "after": 80}})
            flush()
            row = AuditEvent.objects.filter(object_id=tag).first()
            return row is not None and row.hash == row.compute_hash() and row.after == {"quantity": 80}
        step("Audit", audit_)

        def isolation():
            with context.suppressed():
                pass
            capture_exception(object())  # invalid input must be swallowed, not raised
            return True
        step("Failure isolation", isolation)
        step("Alert engine", lambda: alerts.evaluate() is not None)
        step("Incident correlation", lambda: engine.detect() is not None)
        step("OpenTelemetry adapter", lambda: context.parse_traceparent(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")[0] == "4bf92f3577b34da6a3ce929d0e0e4736"
            and isinstance(otel.available(), bool))

        # remove everything this run created
        with context.suppressed():
            from django.db.models import QuerySet

            ObservabilityEvent.objects.filter(message__contains=tag).delete()
            ObservabilityEvent.objects.filter(request_id=state.get("rid", "-")).delete()
            for model in (RequestRecord, DatabaseQuery, ExceptionRecord):
                model.objects.filter(request_id=state.get("rid", "-")).delete()
            for model in (TraceRecord, SpanRecord):
                model.objects.filter(trace_id=state.get("tid", "-")).delete()
            Issue.objects.filter(title__contains=tag).delete()
            SecurityEvent.objects.filter(resource=f"/{tag}/").delete()
            MetricSample.objects.filter(metric__name__startswith=tag).delete()
            from django_observatory.models import Metric

            Metric.objects.filter(name__startswith=tag).delete()
            QuerySet(AuditEvent).filter(object_id=tag).delete()

        ok = all(results)
        self.stdout.write("\nResult: " + (self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")))
        if not ok:
            sys.exit(1)
