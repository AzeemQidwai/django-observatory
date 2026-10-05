"""Redaction, sampling, query language, fingerprints, configuration checks."""
from django.db.models import Q
from django.test import SimpleTestCase, TestCase, override_settings

from django_observatory import conf, fingerprints, redaction, search
from django_observatory.checks import check_configuration
from django_observatory.models import ObservabilityEvent, RequestRecord
from django_observatory.tables import TABLES


class RedactionTests(SimpleTestCase):
    def test_spec_payload(self):
        payload = {"username": "ahmed", "password": "SuperSecret123", "token": "abc123",
                   "authorization": "Bearer xyz", "normal_field": "hello"}
        self.assertEqual(redaction.clean(payload), {
            "username": "ahmed", "password": "[REDACTED]", "token": "[REDACTED]",
            "authorization": "[REDACTED]", "normal_field": "hello"})

    def test_nested_and_variants(self):
        out = redaction.clean({"a": [{"X-Api-Key": "k", "csrfmiddlewaretoken": "t", "client_secret": "s"}],
                               "user": {"sessionid": "abc", "name": "n"}})
        self.assertEqual(out["a"][0], {"X-Api-Key": "[REDACTED]", "csrfmiddlewaretoken": "[REDACTED]",
                                       "client_secret": "[REDACTED]"})
        self.assertEqual(out["user"], {"sessionid": "[REDACTED]", "name": "n"})

    def test_free_text(self):
        self.assertEqual(redaction.text("Authorization: Bearer abc123"), "Authorization: [REDACTED]")
        for secret, sample in [
            ("hunter2", "login failed password=hunter2 for bob"),
            ("s3cr3t", '{"api_key": "s3cr3t"}'),
            ("p4ss", "postgres://app:p4ss@db.local/prod"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij", "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"),
            ("MIIEvQ", "-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----"),
        ]:
            self.assertNotIn(secret, redaction.text(sample), sample)

    def test_bounds_and_unserialisable(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
        self.assertIn("[TRUNCATED]", str(redaction.clean(deep)))
        self.assertLessEqual(len(redaction.clean("x" * 100000)), redaction.MAX_STR + 1)
        self.assertEqual(redaction.clean({"o": object}), {"o": "<class 'object'>"})
        self.assertEqual(len(redaction.clean(list(range(500)))), redaction.MAX_ITEMS + 1)

    def test_headers(self):
        h = redaction.headers({"HTTP_AUTHORIZATION": "Bearer x", "HTTP_COOKIE": "sessionid=1", "HTTP_ACCEPT": "*/*",
                               "HTTP_X_CSRFTOKEN": "t", "SERVER_NAME": "ignored"})
        self.assertEqual(h, {"Authorization": "[REDACTED]", "Cookie": "[REDACTED]", "Accept": "*/*",
                             "X-Csrftoken": "[REDACTED]"})

    def test_compound_keys_and_financial(self):
        self.assertEqual(redaction.clean({"cvv": "123", "cvc": "456", "ssn": "123-45-6789"}),
                         {"cvv": "[REDACTED]", "cvc": "[REDACTED]", "ssn": "[REDACTED]"})
        for secret, sample in [
            ("sec123", "auth_token=sec123"),
            ("pass456", "db_password=pass456"),
            ("mysecret", "app_secret: mysecret"),
            ("tok789", "user_token = tok789"),
        ]:
            self.assertNotIn(secret, redaction.text(sample), sample)

    @override_settings(OBSERVABILITY_REDACTION={"keys": ["national_id"], "patterns": [r"\b\d{5}-\d{7}-\d\b"]})
    def test_custom_rules(self):
        self.assertEqual(redaction.clean({"national_id": "x"}), {"national_id": "[REDACTED]"})
        self.assertNotIn("42101-1234567-1", redaction.text("cnic 42101-1234567-1"))


class FingerprintTests(SimpleTestCase):
    def test_sql_normalisation(self):
        a = fingerprints.normalize_sql("SELECT * FROM users WHERE id = 123")
        b = fingerprints.normalize_sql("SELECT *  FROM users\nWHERE id = 456")
        self.assertEqual(a, "SELECT * FROM users WHERE id = ?")
        self.assertEqual(fingerprints.sql_fingerprint(a), fingerprints.sql_fingerprint(b))
        self.assertEqual(fingerprints.normalize_sql("SELECT a FROM t1 WHERE n IN (%s, %s, %s) AND s = 'it''s'"),
                         "SELECT a FROM t1 WHERE n IN (?) AND s = ?")

    def test_exception_fingerprint_ignores_volatile_values(self):
        frames = [{"module": "app.views", "function": "pay", "in_app": True, "line": 10}]
        moved = [{"module": "app.views", "function": "pay", "in_app": True, "line": 99}]
        fp = fingerprints.exception_fingerprint
        self.assertEqual(fp("ValueError", "order 17 failed", frames, "/pay/"), fp("ValueError", "order 99 failed", moved, "/pay/"))
        self.assertNotEqual(fp("ValueError", "order 17 failed", frames, "/pay/"), fp("KeyError", "order 17 failed", frames, "/pay/"))


class ConfigTests(SimpleTestCase):
    def test_defaults_and_flat_settings(self):
        self.assertEqual(conf.get("RETENTION", "audit"), 365)
        with override_settings(OBSERVABILITY_SLOW_QUERY_THRESHOLD_MS=42, OBSERVABILITY_RETENTION={"logs": 7},
                               OBSERVABILITY_SAMPLING={"requests": 0.1}):
            self.assertEqual(conf.get("DATABASE", "SLOW_QUERY_MS"), 42)
            self.assertEqual(conf.get("RETENTION", "logs"), 7)
            self.assertEqual(conf.get("RETENTION", "audit"), 365)
            self.assertEqual(conf.get("SAMPLING", "requests"), 0.1)
            self.assertEqual(conf.get("SAMPLING", "audit"), 1.0)
        with override_settings(OBSERVABILITY={"RETENTION": {"LOGS_DAYS": 3}}):
            self.assertEqual(conf.get("RETENTION", "logs"), 3)

    def test_valid_configuration_has_no_errors(self):
        self.assertEqual([c for c in check_configuration(None) if c.level >= 40], [])

    def test_invalid_configuration_is_reported(self):
        bad = {"SAMPLING": {"requests": 1.5}, "RETENTION": {"logs": 0}, "STORAGE": {"BACKEND": "nope"},
               "PERMISSION_MODE": "x", "REDACTION": {"patterns": ["("]}, "OTEL": {"enabled": True}}
        with override_settings(OBSERVABILITY=bad, MIDDLEWARE=[]):
            ids = {c.id for c in check_configuration(None)}
        self.assertTrue({"observability.E002", "observability.E003", "observability.E004", "observability.E007",
                         "observability.E008", "observability.E009", "observability.W003"} <= ids, ids)


class SearchTests(TestCase):
    F, T = TABLES["requests"].fields, TABLES["requests"].text

    @classmethod
    def setUpTestData(cls):
        for path, status, ms in [("/a/", 200, 10), ("/b/", 500, 1500), ("/c/", 404, 700), ("/health/x", 200, 5)]:
            RequestRecord.objects.create(method="GET", path=path, route=path, status_code=status, duration_ms=ms)
        ObservabilityEvent.objects.create(level="ERROR", level_no=40, message="connection refused", category="database")
        ObservabilityEvent.objects.create(level="INFO", level_no=20, message="all good", category="application")

    def paths(self, q):
        return sorted(RequestRecord.objects.filter(search.parse(q, self.F, self.T)).values_list("path", flat=True))

    def test_operators(self):
        self.assertEqual(self.paths("status:500"), ["/b/"])
        self.assertEqual(self.paths("duration:>500"), ["/b/", "/c/"])
        self.assertEqual(self.paths("duration>=700 AND status!=404"), ["/b/"])
        self.assertEqual(self.paths("status:500 OR status:404"), ["/b/", "/c/"])
        self.assertEqual(self.paths("NOT status:200"), ["/b/", "/c/"])
        self.assertEqual(self.paths("(status:500 OR status:404) duration:<1000"), ["/c/"])
        self.assertEqual(self.paths("path:/health*"), ["/health/x"])
        self.assertEqual(self.paths("health"), ["/health/x"])
        self.assertEqual(self.paths(""), ["/a/", "/b/", "/c/", "/health/x"])

    def test_levels(self):
        f, t = TABLES["logs"].fields, TABLES["logs"].text
        q = lambda s: list(ObservabilityEvent.objects.filter(search.parse(s, f, t)).values_list("message", flat=True))  # noqa: E731
        self.assertEqual(q("level:error OR level:critical"), ["connection refused"])
        self.assertEqual(q("level:>=warning"), ["connection refused"])
        self.assertEqual(q('category:database AND "connection refused"'), ["connection refused"])

    def test_errors_are_user_presentable(self):
        for bad in ["bogus:1", "status:abc", "(status:500", "status:500)", "method:>GET", "level:loud"]:
            with self.assertRaises(search.QueryError, msg=bad):
                search.parse(bad, {**self.F, **TABLES["logs"].fields}, self.T)

    def test_injection_is_inert(self):
        for evil in ["'; DROP TABLE auth_user; --", "path:' OR 1=1 --", 'path__regex:".*"', "user__password:x",
                     "status:1 OR 1=1"]:
            try:
                q = search.parse(evil, self.F, self.T)
            except search.QueryError:
                continue
            self.assertIsInstance(q, Q)
            list(RequestRecord.objects.filter(q))  # executes as bound parameters only
        self.assertEqual(RequestRecord.objects.count(), 4)

    def test_datetime_awareness(self):
        f = {"timestamp": search.Field("timestamp", kind="datetime")}
        q = search.parse("timestamp:>2026-10-04", f, {})
        self.assertIsInstance(q, Q)


class ConfTests(SimpleTestCase):
    def test_conf_enabled_safe_access(self):
        self.assertTrue(conf.enabled())
        self.assertTrue(conf.enabled("REQUESTS"))
        self.assertTrue(conf.enabled("SAMPLING"))
        self.assertFalse(conf.enabled("NON_EXISTENT_SECTION"))
