"""Incident detection and correlation. Deterministic and explainable: no ML, no LLM.

Every tick:
  1. snapshot()  - compare the current window with the preceding baseline window
  2. symptoms()  - observed anomalies (facts, each with baseline -> current numbers)
  3. rules       - hypotheses about the cause, each scored from explicit evidence
  4. reconcile   - open / update / resolve at most one incident per scope

Facts (IncidentSignal rows) and inference (probable_cause + confidence) are stored
separately and never mixed in the UI.
"""
import datetime

from django.db.models import Count
from django.utils import timezone

from .. import conf, context, internal, metrics
from ..models import Incident, IncidentSignal, RequestRecord
from ..models import ObservabilityConfiguration as Cfg

SECURITY_KINDS = {"login_failures", "permission_denied"}


def fmt_ms(v):
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms"


class Snapshot:
    """Current-vs-baseline view of all built-in metrics."""

    def __init__(self, now=None):
        cfg = conf.get("INCIDENTS")
        self.now = now or timezone.now()
        self.window = cfg["WINDOW_MINUTES"]
        self.baseline = cfg["BASELINE_MINUTES"]
        self.min_requests = cfg["MIN_REQUESTS"]
        self.cur_start = self.now - datetime.timedelta(minutes=self.window)
        self.base_start = self.cur_start - datetime.timedelta(minutes=self.baseline)

        req = self._split("http.requests", lambda lb: (lb.get("route", ""), lb.get("status", "")))
        self.routes = {}
        self.req_cur = self.req_base = self.err_cur = self.err_base = 0.0
        for (route, status), (cur, base) in req.items():
            r = self.routes.setdefault(route, {"req": 0.0, "err": 0.0, "req_base": 0.0, "err_base": 0.0})
            r["req"] += cur.sum
            r["req_base"] += base.sum
            self.req_cur += cur.sum
            self.req_base += base.sum
            if status == "5xx":
                r["err"] += cur.sum
                r["err_base"] += base.sum
                self.err_cur += cur.sum
                self.err_base += base.sum
        self.has_baseline = self.req_base >= self.min_requests

        lat = self._split("http.duration", lambda lb: lb.get("route", ""))
        self.lat_cur, self.lat_base = metrics.Agg(), metrics.Agg()
        for route, (cur, base) in lat.items():
            r = self.routes.setdefault(route, {"req": 0.0, "err": 0.0, "req_base": 0.0, "err_base": 0.0})
            r["lat"], r["lat_base"] = cur, base
            for target, a in ((self.lat_cur, cur), (self.lat_base, base)):
                target.merge(a.count, a.sum, a.min, a.max, None, a.buckets)

        self.ext = {}
        for service, (cur, base) in self._split("ext.duration", lambda lb: lb.get("service", "")).items():
            self.ext[service] = {"lat": cur, "lat_base": base, "calls": 0.0, "err": 0.0, "calls_base": 0.0,
                                 "err_base": 0.0}
        for (service, outcome), (cur, base) in self._split(
                "ext.calls", lambda lb: (lb.get("service", ""), lb.get("outcome", ""))).items():
            e = self.ext.setdefault(service, {"lat": metrics.Agg(), "lat_base": metrics.Agg(), "calls": 0.0,
                                              "err": 0.0, "calls_base": 0.0, "err_base": 0.0})
            e["calls"] += cur.sum
            e["calls_base"] += base.sum
            if outcome == "error":
                e["err"] += cur.sum
                e["err_base"] += base.sum

        self.db_cur, self.db_base = self._split("db.duration", lambda lb: 0).get(0, (metrics.Agg(), metrics.Agg()))
        self.exc = {t: (c.sum, b.sum) for t, (c, b) in self._split("exceptions", lambda lb: lb.get("type", "")).items()}
        self.sec = {e: (c.sum, b.sum) for e, (c, b) in
                    self._split("security.events", lambda lb: lb.get("event", "")).items()}

    def _split(self, name, labelfn):
        """{label key: (Agg current, Agg baseline)} in one scan."""
        cur_start = self.cur_start
        raw = metrics._scan(name, self.base_start, self.now, lambda n, ts, lb: (labelfn(lb), ts >= cur_start))
        out = {}
        for (key, is_cur), agg in raw.items():
            pair = out.setdefault(key, [metrics.Agg(), metrics.Agg()])
            pair[0 if is_cur else 1] = agg
        return out

    def per_min(self, cur, base):
        """Counts -> comparable per-minute rates."""
        return cur / self.window, base / self.baseline

    @property
    def error_rate(self):
        return self.err_cur / self.req_cur * 100 if self.req_cur else 0.0

    @property
    def error_rate_base(self):
        return self.err_base / self.req_base * 100 if self.req_base else 0.0


def _sig(kind, source, description, baseline, current, unit, score, ref_kind="", ref_id=""):
    return {"kind": kind, "source": source, "description": description[:300], "baseline": baseline,
            "current": current, "unit": unit, "score": score, "ref_kind": ref_kind, "ref_id": str(ref_id)[:200]}


def symptoms(s):
    """Observed anomalies only. Thresholds combine a relative jump with an absolute floor,
    so quiet systems and tiny samples do not page anyone."""
    out = []
    slow_ms = conf.get("REQUESTS", "SLOW_MS")

    if s.req_cur >= s.min_requests and s.err_cur >= 5:
        cur, base = s.error_rate, s.error_rate_base
        if cur >= 5 and (not s.has_baseline or cur >= 2 * base + 1):
            base_txt = f"{base:.1f}%" if s.has_baseline else "no baseline"
            out.append(_sig("error_rate", "http", f"HTTP 5xx rate: {base_txt} → {cur:.1f}% "
                            f"({s.err_cur:.0f} of {s.req_cur:.0f} requests)", base if s.has_baseline else None,
                            cur, "%", min(cur / 25, 1.0)))

    if s.lat_cur.count >= s.min_requests:
        cur = s.lat_cur.quantile(0.95)
        base = s.lat_base.quantile(0.95) if s.lat_base.count >= s.min_requests else None
        if cur >= 500 and (cur >= 2 * base if base else cur >= 2 * slow_ms):
            out.append(_sig("request_latency", "http", f"Request P95: {fmt_ms(base) if base else 'no baseline'} → "
                            f"{fmt_ms(cur)}", base, cur, "ms", min(cur / (base * 8) if base else 0.5, 1.0)))

    for service, e in s.ext.items():
        if e["calls"] < 5:
            continue
        cur = e["lat"].quantile(0.95)
        base = e["lat_base"].quantile(0.95) if e["lat_base"].count >= 5 else None
        if cur >= 500 and (cur >= 2 * base if base else cur >= 2000):
            out.append(_sig("external_latency", service, f"{service} latency P95: "
                            f"{fmt_ms(base) if base else 'no baseline'} → {fmt_ms(cur)}", base, cur, "ms",
                            min(cur / (base * 8) if base else 0.5, 1.0), "external", service))
        rate = e["err"] / e["calls"] * 100
        rate_base = e["err_base"] / e["calls_base"] * 100 if e["calls_base"] >= 5 else None
        if rate >= 20 and (rate_base is None or rate >= 2 * rate_base + 5):
            out.append(_sig("external_failures", service, f"{service} failure rate: "
                            f"{f'{rate_base:.1f}%' if rate_base is not None else 'no baseline'} → {rate:.1f}% "
                            f"({e['err']:.0f} of {e['calls']:.0f} calls)", rate_base, rate, "%",
                            min(rate / 60, 1.0), "external", service))

    if s.db_cur.count >= 20:
        cur = s.db_cur.quantile(0.95)
        base = s.db_base.quantile(0.95) if s.db_base.count >= 20 else None
        if cur >= 100 and (cur >= 3 * base if base else cur >= conf.get("DATABASE", "SLOW_QUERY_MS")):
            out.append(_sig("database_latency", "database", f"SQL P95: {fmt_ms(base) if base else 'no baseline'} → "
                            f"{fmt_ms(cur)}", base, cur, "ms", min(cur / (base * 10) if base else 0.5, 1.0)))

    for exc_type, (cur, base) in s.exc.items():
        rate, rate_base = s.per_min(cur, base)
        if cur >= 5 and rate >= 3 * rate_base:
            pct = f"+{(rate / rate_base - 1) * 100:.0f}%" if rate_base else "new"
            out.append(_sig("exception_spike", exc_type, f"{exc_type} occurrences: {pct} ({cur:.0f} in "
                            f"{s.window} min)", rate_base, rate, "/min", min(cur / 50, 1.0), "exception", exc_type))

    # Server resources: sustained (window average) saturation, or a nearly full volume.
    from .. import system

    limits = conf.get("SYSTEM")
    res = s.resources = system.worst(s.cur_start, s.now + datetime.timedelta(minutes=1))
    base = system.worst(s.base_start, s.cur_start)
    for key, label, limit in (("cpu", "CPU", max(limits["CPU_PERCENT"], 50)), ("memory", "Memory", limits["MEMORY_PERCENT"])):
        if res[key] is not None and res[key] >= limit:
            before = f"{base[key]:.0f}%" if base[key] is not None else "no baseline"
            out.append(_sig(f"resource_{key}", res[f"{key}_host"], f"{label} usage on {res[f'{key}_host']}: {before} → "
                            f"{res[key]:.0f}% (average over {s.window} min)", base[key], res[key], "%",
                            min(res[key] / 100, 1.0), "host", res[f"{key}_host"]))
    if res["disk"] is not None and res["disk"] >= max(limits["DISK_PERCENT"], 50):
        out.append(_sig("resource_disk", res["disk_host"], f"Disk {res['disk_mount']} on {res['disk_host']} is "
                        f"{res['disk']:.1f}% full", base["disk"], res["disk"], "%", min(res["disk"] / 100, 1.0),
                        "host", res["disk_host"]))

    for event, kind, floor in (("LOGIN_FAILURE", "login_failures", 10), ("PERMISSION_DENIED", "permission_denied", 20)):
        cur, base = s.sec.get(event, (0, 0))
        rate, rate_base = s.per_min(cur, base)
        if cur >= floor and rate >= 3 * rate_base:
            out.append(_sig(kind, "security", f"{event.replace('_', ' ').title()} events: {cur:.0f} in {s.window} min "
                            f"(baseline {rate_base * s.window:.1f})", rate_base, rate, "/min", min(cur / (floor * 5), 1.0)))
    return out


def affected_requests(s, limit=400):
    """Captured requests in the current window that failed or were abnormally slow."""
    base = s.lat_base.quantile(0.95) if s.lat_base.count >= s.min_requests else None
    slow = max(2 * base if base else 0, conf.get("REQUESTS", "SLOW_MS"))
    qs = RequestRecord.objects.filter(timestamp__gte=s.cur_start, timestamp__lte=s.now)
    qs = qs.filter(status_code__gte=500) | qs.filter(duration_ms__gte=slow)
    return list(qs.order_by("-timestamp").values(
        "request_id", "route", "user_id", "status_code", "duration_ms", "db_ms", "ext_ms", "release")[:limit])


def track_release(now):
    """Remember when each release was first seen; returns (current, first_seen, previous_exists)."""
    release = conf.get("RELEASE")
    if not release:
        return "", None, False
    row, _ = Cfg.objects.get_or_create(key=f"release:{release}"[:100], defaults={"updated_at": now})
    older = Cfg.objects.filter(key__startswith="release:", updated_at__lt=row.updated_at).exists()
    return release, row.updated_at, older


def _probable_start(s, hyp):
    """Walk back minute by minute while the leading signal stays anomalous (inference)."""
    start = s.now - datetime.timedelta(minutes=s.window + 30)
    if hyp.kind == "external":
        series = metrics.timeseries("ext.duration", start, s.now, 60, service=hyp.subject)
        base = s.ext.get(hyp.subject, {}).get("lat_base")
        floor = max(2 * base.avg if base is not None and base.count else 0, 300)
        errs = dict(metrics.timeseries("ext.calls", start, s.now, 60, service=hyp.subject, outcome="error"))
        bad = lambda t, a: (a.count and a.avg >= floor) or errs[t].sum > 0  # noqa: E731
        empty = lambda t, a: not a.count  # noqa: E731
    elif hyp.kind == "database":
        series = metrics.timeseries("db.duration", start, s.now, 60)
        floor = max(3 * s.db_base.avg if s.db_base.count else 0, 50)
        bad = lambda t, a: a.count and a.avg >= floor  # noqa: E731
        empty = lambda t, a: not a.count  # noqa: E731
    else:
        series = metrics.timeseries("http.requests", start, s.now, 60, status="5xx")
        bad = lambda t, a: a.sum > 0  # noqa: E731
        empty = lambda t, a: False  # noqa: E731
    earliest = None
    for t, a in reversed(series):
        if bad(t, a):
            earliest = t
        elif not empty(t, a):
            break
    return earliest or s.cur_start


def _impact(s, start, routes):
    since = metrics._scan("http.requests", start, s.now + datetime.timedelta(minutes=1),
                          lambda n, ts, lb: (lb.get("route", ""), lb.get("status", "")))
    reqs = sum(a.sum for (route, _), a in since.items() if route in routes)
    errs = sum(a.sum for (route, status), a in since.items() if route in routes and status == "5xx")
    users = (RequestRecord.objects.filter(timestamp__gte=start, route__in=list(routes)[:50])
             .exclude(user_id="").values("user_id").distinct().count())
    return int(reqs), int(errs), users


def _severity(s, scope):
    if scope == "security":
        return "high"
    rate = s.error_rate if s.req_cur >= s.min_requests else 0
    return "critical" if rate >= 25 else "high" if rate >= 10 or s.err_cur >= 50 else "medium"


def _reconcile(scope, signals, s, affected):
    from . import rules

    now = s.now
    active = (Incident.objects.filter(dedup_key__startswith=scope + ":")
              .exclude(status=Incident.Status.RESOLVED).order_by("-detected_at").first())
    if not signals:
        quiet = datetime.timedelta(minutes=conf.get("INCIDENTS", "RESOLVE_AFTER_MINUTES"))
        if active and now - active.last_seen >= quiet:
            active.status, active.resolved_at = Incident.Status.RESOLVED, now
            active.save(update_fields=["status", "resolved_at"])
        return None

    hyps = sorted(rules.evaluate(s, signals, affected, scope), key=lambda h: -h.score)
    best = hyps[0]
    if best.kind == "external":
        # precise: the endpoints that actually call the degraded service
        from ..models import ExternalCall

        routes = set(ExternalCall.objects.filter(timestamp__gte=s.cur_start, service=best.subject)
                     .exclude(route="").values_list("route", flat=True).distinct()[:50])
    else:
        routes = {r["route"] for r in affected} | {
            route for route, r in s.routes.items() if r["err"] > 0 and r["err"] / max(r["req"], 1) >= 0.05}
    cause_fields = dict(
        cause_kind=best.kind, cause_subject=best.subject[:200], probable_cause=best.cause[:500],
        confidence=best.confidence, confidence_score=round(best.score, 3), recommendation=best.recommendation[:500],
        title=best.title[:300], dedup_key=f"{scope}:{best.kind}:{best.subject}"[:200],
        alternatives=[{"cause": h.cause, "confidence": h.confidence, "score": round(h.score, 2)} for h in hyps[1:4]],
    )
    created = active is None
    if created:
        active = Incident(started_at=_probable_start(s, best), detected_at=now, **cause_fields)
    elif best.score >= active.confidence_score or best.kind == active.cause_kind:
        for k, v in cause_fields.items():  # better-supported explanation (or refreshed evidence)
            setattr(active, k, v)
    active.last_seen = now
    active.severity = _severity(s, scope)
    if scope != "security":
        active.affected_requests, active.error_count, active.affected_users = _impact(s, active.started_at, routes)
        known = {e["route"] for e in active.affected_endpoints}
        active.affected_endpoints = (active.affected_endpoints + [
            {"route": r} for r in sorted(routes - known) if r])[:25]
    active.save()

    # Signals are a live view of the evidence: replace, don't accumulate duplicates.
    active.signals.all().delete()
    rows = []
    for sig in signals:
        role = "cause" if (sig["kind"], sig["source"]) in best.cause_signals else "effect"
        rows.append(IncidentSignal(incident=active, relationship=role, timestamp=now, **sig))
    for fact in best.facts:
        rows.append(IncidentSignal(incident=active, kind="evidence", source=best.subject[:200], relationship="evidence",
                                   timestamp=now, description=fact[:300], score=best.score,
                                   ref_kind=best.ref_kind, ref_id=str(best.ref_id)[:200]))
    IncidentSignal.objects.bulk_create(rows)
    if created:
        try:
            from .. import alerts

            alerts.notify_incident(active)
            alerts.Alert.objects.filter(incident__isnull=True).exclude(status="RESOLVED").update(incident=active)
        except Exception as exc:  # noqa: BLE001
            internal.warn("incidents.notify", "failed", exc)
    return active


def detect(now=None):
    """Run one detection pass. Returns the incidents opened or updated."""
    with context.suppressed():
        s = Snapshot(now)
        s.release, s.release_seen, s.release_has_predecessor = track_release(s.now)
        sigs = symptoms(s)
        affected = affected_requests(s) if sigs else []
        perf = [x for x in sigs if x["kind"] not in SECURITY_KINDS]
        sec = [x for x in sigs if x["kind"] in SECURITY_KINDS]
        return [i for i in (_reconcile("performance", perf, s, affected), _reconcile("security", sec, s, [])) if i]


def top_error_routes(s, n=5):
    return sorted(((r, v["err"]) for r, v in s.routes.items() if v["err"]), key=lambda x: -x[1])[:n]


def exception_shares(s):
    """[(issue_id, title, exc_type, status, first_seen, count)] for the current window."""
    from ..models import ExceptionRecord

    rows = (ExceptionRecord.objects.filter(timestamp__gte=s.cur_start, timestamp__lte=s.now)
            .values("issue_id", "issue__title", "exc_type", "issue__status", "issue__first_seen")
            .annotate(n=Count("id")).order_by("-n")[:10])
    return [(r["issue_id"], r["issue__title"], r["exc_type"], r["issue__status"], r["issue__first_seen"], r["n"])
            for r in rows]
