"""Correlation rules. Each rule looks at the snapshot + observed symptoms and may return
a Hypothesis whose score is the sum of named pieces of evidence. Nothing is opaque: the
facts that produced the score are stored with the incident and shown to the operator.

To add a rule: write ``def rule_x(s, kinds, signals, affected) -> Hypothesis | None``
and append it to RULES.
"""
import datetime
from collections import Counter

from ..models import DatabaseQuery, ExternalCall
from . import engine


class Hypothesis:
    def __init__(self, kind, subject, title, cause, recommendation, ref_kind="", ref_id=""):
        self.kind, self.subject, self.title, self.cause = kind, subject, title, cause
        self.recommendation = recommendation
        self.ref_kind, self.ref_id = ref_kind, ref_id or subject
        self.score = 0.0
        self.facts = []
        self.cause_signals = set()  # (kind, source) of symptoms this hypothesis explains as the cause

    def add(self, points, fact):
        """Evidence: an observed fact and how much it supports this hypothesis."""
        self.score = min(self.score + points, 1.0)
        self.facts.append(fact)
        return self

    @property
    def confidence(self):
        return "HIGH" if self.score >= 0.75 else "MEDIUM" if self.score >= 0.5 else "LOW"


def _impact_present(kinds):
    return "error_rate" in kinds or "request_latency" in kinds


def rule_external(s, kinds, signals, affected):
    best = None
    for sig in signals:
        if sig["kind"] not in ("external_latency", "external_failures"):
            continue
        service = sig["source"]
        h = Hypothesis(
            "external", service, f"{service} integration degradation",
            f"{service} API {'latency increase' if sig['kind'] == 'external_latency' else 'failures'}",
            f"Investigate the {service} integration (latency, availability, timeouts, network path).",
            "external", service)
        for other in signals:
            if other["source"] == service and other["kind"].startswith("external_"):
                h.add(0.2 if other is sig else 0.1, other["description"])
                h.cause_signals.add((other["kind"], service))
        h.score += 0.15
        if _impact_present(kinds):
            h.add(0.2, "Request latency / error rate degraded in the same time window")
        ids = [r["request_id"] for r in affected]
        if ids:
            with_call = (ExternalCall.objects.filter(request_id__in=ids, service=service)
                         .values("request_id").distinct().count())
            share = with_call / len(ids)
            if share >= 0.2:
                h.add(0.3 * share, f"{service} is called in {share:.0%} of the {len(ids)} failing/slow requests captured")
            ext_share = sum(r["ext_ms"] for r in affected) / max(sum(r["duration_ms"] for r in affected), 1)
            if share >= 0.2 and ext_share >= 0.5:
                h.add(0.1, f"External calls account for {ext_share:.0%} of the time spent in those requests")
        timeouts = sum(cur for t, (cur, _) in s.exc.items() if "timeout" in t.lower())
        if timeouts:
            h.add(0.1, f"{timeouts:.0f} timeout exception(s) raised in the window")
        if best is None or h.score > best.score:
            best = h
    return best


def rule_database(s, kinds, signals, affected):
    sig = next((x for x in signals if x["kind"] == "database_latency"), None)
    if sig is None:
        return None
    h = Hypothesis("database", "database", "Database latency degradation", "Database query latency increase",
                   "Inspect the slowest SQL fingerprints, missing indexes, locks and database load.", "database", "db")
    h.add(0.3, sig["description"])
    h.cause_signals.add(("database_latency", "database"))
    if _impact_present(kinds):
        h.add(0.2, "Request latency / error rate degraded in the same time window")
    if affected:
        share = sum(r["db_ms"] for r in affected) / max(sum(r["duration_ms"] for r in affected), 1)
        if share >= 0.3:
            h.add(0.35 * share, f"SQL accounts for {share:.0%} of the time spent in failing/slow requests")
    top = (DatabaseQuery.objects.filter(timestamp__gte=s.cur_start, is_slow=True)
           .order_by("-duration_ms").values("normalized_sql", "duration_ms", "fingerprint").first())
    if top:
        h.add(0.1, f"Slowest query {engine.fmt_ms(top['duration_ms'])}: {top['normalized_sql'][:120]}")
        h.ref_kind, h.ref_id = "query", top["fingerprint"]
    return h


def rule_deployment(s, kinds, signals, affected):
    seen = getattr(s, "release_seen", None)
    if not seen or not getattr(s, "release_has_predecessor", False) or not _impact_present(kinds):
        return None
    age = s.now - seen
    if age > datetime.timedelta(minutes=s.window + 30):
        return None
    h = Hypothesis("deployment", s.release, f"Regression after release {s.release}",
                   f"Release {s.release} deployed shortly before the degradation",
                   f"Review the changes in release {s.release}; consider rolling back.", "release", s.release)
    h.add(0.35, f"Release {s.release} was first seen {age.total_seconds() / 60:.0f} min before detection")
    if s.has_baseline and s.error_rate_base < max(s.error_rate / 2, 1):
        h.add(0.2, f"Before the release the 5xx rate was {s.error_rate_base:.1f}% (now {s.error_rate:.1f}%)")
    if affected:
        on_new = sum(1 for r in affected if r["release"] == s.release) / len(affected)
        if on_new >= 0.9:
            h.add(0.15, f"{on_new:.0%} of failing/slow requests ran on release {s.release}")
    return h


def rule_exception(s, kinds, signals, affected):
    shares = engine.exception_shares(s)
    total = sum(n for *_, n in shares)
    if not shares or total < 5:
        return None
    issue_id, title, exc_type, status, first_seen, n = shares[0]
    share = n / total
    if share < 0.5:
        return None
    h = Hypothesis("exception", exc_type, f"Error spike: {title[:120]}", f"Application error {title[:160]}",
                   f"Open issue #{issue_id}: inspect the stack trace and the most recent occurrences.",
                   "issue", issue_id)
    h.add(0.2 + 0.2 * share, f"Issue #{issue_id} accounts for {share:.0%} of {total} exceptions in the window")
    h.cause_signals.add(("exception_spike", exc_type))
    if "error_rate" in kinds:
        h.add(0.2, "HTTP 5xx rate increased in the same time window")
    if status == "REGRESSED":
        h.add(0.15, f"Issue #{issue_id} had been resolved and has regressed")
    elif first_seen and first_seen >= s.cur_start - datetime.timedelta(minutes=15):
        h.add(0.15, f"Issue #{issue_id} is new (first seen {first_seen:%H:%M:%S})")
    return h


def rule_resources(s, kinds, signals, affected):
    """Server hardware/resource exhaustion: saturated CPU, memory pressure, full disk."""
    found = [x for x in signals if x["kind"].startswith("resource_")]
    if not found:
        return None
    sig = max(found, key=lambda x: (x["kind"] == "resource_disk", x["score"]))  # a full disk outranks the rest
    what = {"resource_cpu": ("CPU saturation", "Check for runaway processes, heavy reports or too few workers; "
                             "consider more CPU capacity."),
            "resource_memory": ("Memory exhaustion", "Check for memory leaks and worker counts; the server may be "
                                "swapping. Consider more RAM or recycling workers."),
            "resource_disk": ("Disk almost full", "Free disk space now: logs, temporary files, uploads, database "
                              "growth. Writes fail when the volume fills.")}[sig["kind"]]
    host = sig["source"]
    h = Hypothesis("resources", f"{sig['kind'][9:]}:{host}", f"Server resource problem: {what[0]} on {host}",
                   f"{what[0]} on server {host}", what[1], "host", host)
    for x in found:
        h.add(0.35 if x is sig else 0.1, x["description"])
        h.cause_signals.add((x["kind"], x["source"]))
    if sig["baseline"] is not None and sig["kind"] != "resource_disk" and sig["current"] - sig["baseline"] >= 30:
        h.add(0.1, f"Usage rose {sig['current'] - sig['baseline']:.0f} points above the preceding hour's level")
    if _impact_present(kinds):
        h.add(0.25, "Request latency / error rate degraded in the same time window")
    hardware = sum(cur for t, (cur, _) in s.exc.items()
                   if t in ("MemoryError", "OSError", "OperationalError", "DatabaseError", "IOError"))
    if hardware:
        h.add(0.15, f"{hardware:.0f} resource-related exception(s) (OSError / MemoryError / database errors) in the window")
    if "external_latency" not in kinds and "external_failures" not in kinds and _impact_present(kinds):
        h.add(0.1, "No external service degraded at the same time")
    return h


def rule_security(s, kinds, signals, affected):
    from ..models import SecurityEvent

    sig = max(signals, key=lambda x: x["score"])
    event = "LOGIN_FAILURE" if sig["kind"] == "login_failures" else "PERMISSION_DENIED"
    ips = Counter(SecurityEvent.objects.filter(event=event, timestamp__gte=s.cur_start)
                  .values_list("ip_address", flat=True)[:2000])
    top_ip, top_n = (ips.most_common(1) or [("", 0)])[0]
    total = sum(ips.values()) or 1
    concentrated = top_ip and top_n / total >= 0.5
    if event == "LOGIN_FAILURE":
        title = "Failed login spike"
        cause = f"Possible credential attack from {top_ip}" if concentrated else "Failed login spike across many sources"
        rec = "Review the security events; consider rate limiting or blocking the source address."
    else:
        title = "Permission denied spike"
        cause = f"Repeated forbidden access attempts from {top_ip}" if concentrated else "Permission denied spike"
        rec = "Check whether a release changed permissions, or whether an account is probing endpoints."
    h = Hypothesis("security", event, title, cause, rec, "security", event)
    h.add(0.4, sig["description"])
    h.cause_signals.add((sig["kind"], "security"))
    if concentrated:
        h.add(0.35, f"{top_n} of {total} events ({top_n / total:.0%}) come from a single address: {top_ip}")
    elif len(ips) > 1:
        h.add(0.1, f"Events are spread across {len(ips)} source addresses")
    return h


def rule_unexplained(s, kinds, signals, affected):
    sig = max(signals, key=lambda x: x["score"])
    h = Hypothesis("unexplained", sig["kind"], {"error_rate": "HTTP error rate spike",
                   "request_latency": "Request latency spike"}.get(sig["kind"], "Operational anomaly"),
                   "No single cause identified from the available telemetry",
                   "Open the affected requests and compare their traces with healthy ones.")
    h.add(0.2, sig["description"])
    routes = engine.top_error_routes(s, 3)
    if routes:
        h.facts.append("Most failing endpoints: " + ", ".join(f"{r} ({n:.0f})" for r, n in routes))
    return h


RULES = (rule_external, rule_database, rule_resources, rule_deployment, rule_exception, rule_unexplained)


def evaluate(s, signals, affected, scope="performance"):
    """All hypotheses that apply, unsorted. Always returns at least one."""
    if scope == "security":
        return [rule_security(s, set(), signals, affected)]
    kinds = {x["kind"] for x in signals}
    return [h for h in (rule(s, kinds, signals, affected) for rule in RULES) if h is not None]
