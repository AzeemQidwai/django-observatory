"""Permissions, UI smoke tests, API, exports and web security (XSS, IDOR, CSRF, traversal)."""
import json

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase, override_settings

from django_observatory import audit, metrics, observe
from django_observatory.models import (
    AlertRule, AuditEvent, ExceptionRecord, Issue, ObservabilityEvent, RequestRecord, TraceRecord,
)

XSS = "<script>alert(1)</script>"
BASE = {"PIPELINE": {"SYNC": True}, "SCHEDULER": {"ENABLED": False}, "ALERTS": {"DEFAULT_RULES": False}}


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))


class UiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("root", password="pw")
        cls.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        cls.plain = User.objects.create_user("plain", password="pw")
        c = Client(raise_request_exception=False)
        c.get("/ok/")
        c.get("/sql/")
        c.get("/boom/7/")
        c.get("/denied/")
        observe.error(XSS, note=XSS)
        audit.record("UPDATE", "Material", "A1", changes={"quantity": {"before": 1, "after": 2}})
        metrics.flush()  # in-memory aggregates are reset whenever a test overrides settings


class PermissionTests(UiBase):
    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/observability/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("next=/observability/", resp["Location"])
        self.assertEqual(self.client.get("/observability/api/logs/").status_code, 401)

    def test_non_staff_is_forbidden(self):
        self.client.force_login(self.plain)
        for url in ("/observability/", "/observability/logs/", "/observability/requests/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.get("/observability/api/requests/").status_code, 403)

    def test_staff_sees_telemetry_but_not_elevated_sections(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/observability/logs/").status_code, 200)
        for url in ("/observability/audit/", "/observability/settings/", "/observability/api/audit/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertNotContains(self.client.get("/observability/"), "Audit events")
        grant(self.staff, "view_audit_events")
        self.staff = User.objects.get(pk=self.staff.pk)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/observability/audit/").status_code, 200)

    def test_idor_changing_an_id_does_not_bypass_section_permission(self):
        self.client.force_login(self.staff)
        pk = AuditEvent.objects.get().pk
        self.assertEqual(self.client.get(f"/observability/api/audit/{pk}/").status_code, 403)
        req = RequestRecord.objects.filter(path="/ok/").get()
        self.assertNotContains(self.client.get(f"/observability/requests/{req.request_id}/"), "AUDIT UPDATE")

    @override_settings(OBSERVABILITY={**BASE, "PERMISSION_MODE": "permissions"})
    def test_granular_mode(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/observability/").status_code, 403)
        grant(self.staff, "view_observability", "view_logs")
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get("/observability/logs/").status_code, 200)
        self.assertEqual(self.client.get("/observability/requests/").status_code, 403)

    def test_export_and_management_need_their_permissions(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/observability/logs/?export=csv").status_code, 200)
        with override_settings(OBSERVABILITY={**BASE, "PERMISSION_MODE": "permissions"}):
            grant(self.staff, "view_observability", "view_logs", "view_exceptions")
            self.client.force_login(User.objects.get(pk=self.staff.pk))
            self.assertEqual(self.client.get("/observability/logs/?export=csv").status_code, 403)
            issue = Issue.objects.get()
            self.assertEqual(self.client.post(f"/observability/issues/{issue.pk}/", {"status": "RESOLVED"}).status_code, 403)
            self.assertEqual(Issue.objects.get().status, "OPEN")


class SmokeTests(UiBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_every_page_renders(self):
        pages = ["", "logs/", "requests/", "traces/", "exceptions/", "issues/?range=all", "security/", "audit/",
                 "audit/?verify=1", "performance/", "database/", "database/queries/", "external/", "external/calls/",
                 "metrics/", "metrics/?name=http.duration", "alerts/", "alerts/history/", "incidents/?range=all",
                 "health/", "settings/", "search/?q=boom", "logs/?q=level:error&sort=-level_no&page=2",
                 "requests/?q=status:>=500 AND duration:>0", "?range=7d", "?range=bogus"]
        for page in pages:
            self.assertEqual(self.client.get("/observability/" + page).status_code, 200, page)

    def test_detail_pages_and_causal_analysis(self):
        req = RequestRecord.objects.get(status_code=500)
        resp = self.client.get(f"/observability/requests/{req.request_id}/")
        self.assertContains(resp, "Why did this fail?")
        self.assertContains(resp, "Observed evidence")
        self.assertContains(resp, "Inference")
        exc = ExceptionRecord.objects.get()
        for url in (f"/observability/traces/{req.trace_id}/", f"/observability/exceptions/{exc.pk}/",
                    f"/observability/issues/{exc.issue_id}/", f"/observability/logs/{ObservabilityEvent.objects.first().pk}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for url in (f"/observability/exceptions/{exc.pk}/", f"/observability/issues/{exc.issue_id}/"):
            html = self.client.get(url).content.decode()
            self.assertIn('data-copy="#traceback"', html)  # copy-to-clipboard button and its source
            self.assertIn('id="traceback"', html)
            self.assertIn("Traceback (most recent call last)", html)
        self.assertEqual(self.client.get("/observability/requests/req_doesnotexist/").status_code, 404)
        self.assertEqual(self.client.get("/observability/traces/" + "0" * 32 + "/").status_code, 404)

    def test_visualisations_are_rendered_with_data(self):
        import re

        def charts(url):
            html = self.client.get(url).content.decode()
            found = {}
            for cid, payload in re.findall(r'<script id="([\w-]+)" type="application/json">(.*?)</script>', html, re.S):
                data = json.loads(payload)
                if "series" in data:
                    self.assertIn(f'data-chart="{cid}"', html)
                    self.assertTrue(all(len(s["values"]) == len(data["labels"]) for s in data["series"]), cid)
                    found[cid] = data
            return html, found

        html, dash = charts("/observability/?range=1h")
        self.assertTrue({"c-req", "c-err", "c-lat", "c-exc", "c-db", "c-ext", "c-log", "c-dist"} <= set(dash))
        self.assertEqual({s["name"] for s in dash["c-req"]["series"]}, {"2xx", "4xx", "5xx"})  # ok, denied, boom
        self.assertEqual(dash["c-req"]["type"], "bar")
        self.assertEqual(sum(sum(s["values"]) for s in dash["c-req"]["series"]), 4)
        self.assertTrue(dash["c-dist"]["categorical"] and sum(dash["c-dist"]["series"][0]["values"]) == 4)
        self.assertEqual(html.count('class="bars rank"'), 3)
        self.assertIn('data-tip="/boom/&lt;int:pk&gt;/ |', html)  # ranked-bar tooltip text, escaped

        _, logs = charts("/observability/logs/?range=1h")
        levels = {s["name"]: sum(s["values"]) for s in logs["c-vol"]["series"]}
        self.assertEqual(levels["ERROR"], ObservabilityEvent.objects.filter(level="ERROR").count())
        _, filtered = charts("/observability/logs/?range=1h&q=level:error")
        self.assertEqual([s["name"] for s in filtered["c-vol"]["series"]], ["ERROR"])  # follows the filter
        _, reqs = charts("/observability/requests/?range=all")
        self.assertEqual(sum(sum(s["values"]) for s in reqs["c-vol"]["series"]), RequestRecord.objects.count())
        for url, expected in (("/observability/performance/", {"p-lat", "p-dist"}),
                              ("/observability/database/", {"c-dbl", "c-dbv", "c-dbd"}),
                              ("/observability/metrics/?name=http.duration", {"c-met", "c-metd"})):
            self.assertTrue(expected <= set(charts(url)[1]), url)
        self.assertRegex(html, r"assets/app\.js\?v=[0-9a-f]+")  # cache-busted assets

    def test_query_errors_are_shown_not_raised(self):
        resp = self.client.get("/observability/logs/?q=nosuchfield:1")
        self.assertContains(resp, "Unknown field")

    def test_issue_triage_and_alert_rules(self):
        issue = Issue.objects.get()
        self.client.post(f"/observability/issues/{issue.pk}/", {"status": "RESOLVED", "assignee": "ahmed", "notes": "fixed"})
        issue.refresh_from_db()
        self.assertEqual((issue.status, issue.assignee, issue.notes), ("RESOLVED", "ahmed", "fixed"))
        self.client.post("/observability/alerts/", {"action": "create", "name": "Too slow", "metric": "p95_latency",
                                                    "operator": ">", "threshold": "2000", "window_minutes": "5"})
        rule = AlertRule.objects.get()
        self.client.post("/observability/alerts/", {"action": "toggle", "id": rule.pk})
        self.assertFalse(AlertRule.objects.get().enabled)
        bad = self.client.post("/observability/alerts/", {"action": "create", "name": "x", "metric": "evil", "threshold": "1"})
        self.assertContains(bad, "Could not create the rule")

    def test_runtime_settings_override(self):
        from django_observatory import conf

        self.client.post("/observability/settings/", {"action": "runtime", "SAMPLING.requests": "0.25",
                                                      "DATABASE.SLOW_QUERY_MS": "123", "SAMPLING.successful_logs": "7"})
        self.assertEqual(conf.get("SAMPLING", "requests"), 0.25)
        self.assertEqual(conf.get("DATABASE", "SLOW_QUERY_MS"), 123)
        self.assertEqual(conf.get("SAMPLING", "successful_logs"), 1.0)  # invalid value rejected
        conf.set_runtime({})

    def test_assets_served_offline_without_traversal(self):
        self.assertEqual(self.client.get("/observability/assets/app.css")["Content-Type"], "text/css")
        for evil in ("../models.py", "..%2F..%2Fsettings.py", "app.css/../../views.py"):
            self.assertEqual(self.client.get("/observability/assets/" + evil).status_code, 404)
        html = self.client.get("/observability/").content.decode()
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))  # no CDN / external asset
        self.assertNotIn("https://", html)


class WebSecurityTests(UiBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_xss_stored_payloads_are_escaped(self):
        event = ObservabilityEvent.objects.get(message=XSS)
        for url in ("/observability/logs/", f"/observability/logs/{event.pk}/", f"/observability/search/?q={XSS}",
                    f"/observability/logs/?q={XSS}"):
            html = self.client.get(url).content.decode()
            self.assertNotIn(XSS, html, url)
            self.assertIn("&lt;script&gt;", html, url)

    def test_csrf_is_enforced_on_state_changes(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        issue = Issue.objects.get()
        self.assertEqual(c.post(f"/observability/issues/{issue.pk}/", {"status": "RESOLVED"}).status_code, 403)
        self.assertEqual(c.post("/observability/alerts/", {"action": "create"}).status_code, 403)
        self.assertEqual(Issue.objects.get().status, "OPEN")

    def test_lists_are_read_only(self):
        self.assertEqual(self.client.post("/observability/audit/", {}).status_code, 405)
        self.assertEqual(self.client.delete("/observability/logs/").status_code, 405)

    def test_api_and_filter_injection(self):
        data = self.client.get("/observability/api/requests/?limit=2").json()
        self.assertEqual(len(data["results"]), 2)
        self.assertIsNotNone(data["next_offset"])
        self.assertEqual(self.client.get("/observability/api/requests/?q=user__password:x").status_code, 400)
        self.assertEqual(self.client.get("/observability/api/requests/?limit=abc").status_code, 400)
        self.assertEqual(self.client.get("/observability/api/nosuch/").status_code, 404)
        self.assertEqual(self.client.get("/observability/requests/?sort=user__password").status_code, 200)
        self.assertEqual(self.client.get("/observability/api/health/").json()["status"] in ("HEALTHY", "DEGRADED"), True)
        self.assertIn("points", self.client.get("/observability/api/metrics/?name=http.requests").json())

    def test_exports_are_redacted_and_formula_safe(self):
        observe.info("=HYPERLINK(evil)", note="Authorization: Bearer leak123")
        ObservabilityEvent.objects.filter(message="=HYPERLINK(evil)").update(message="=cmd password=late-secret")
        body = b"".join(self.client.get("/observability/logs/?export=csv&range=all").streaming_content).decode()
        self.assertIn("'=cmd", body)  # spreadsheet formula neutralised
        self.assertNotIn("late-secret", body)  # redacted again on the way out
        self.assertNotIn("leak123", body)
        nd = b"".join(self.client.get("/observability/logs/?export=ndjson").streaming_content).decode()
        self.assertTrue(all(json.loads(line) for line in nd.splitlines()))
        js = b"".join(self.client.get("/observability/requests/?export=json").streaming_content)
        self.assertEqual(len(json.loads(js)), RequestRecord.objects.count())
        self.assertEqual(self.client.get("/observability/logs/?export=exe").status_code, 404)

    def test_no_stack_traces_or_secrets_leak_to_unauthorised_users(self):
        self.client.force_login(self.plain)
        exc = ExceptionRecord.objects.get()
        resp = self.client.get(f"/observability/exceptions/{exc.pk}/")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(b"Traceback", resp.content)
        self.assertEqual(TraceRecord.objects.filter(name__contains="observability").count(), 0)
