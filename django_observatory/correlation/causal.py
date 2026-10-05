"""Per-request causal analysis: "Why is this slow?" / "Why did this fail?".

Returns observed evidence and a separately-labelled inference with a confidence level.
"""
import datetime

from django.utils import timezone

from .. import conf, metrics
from ..models import ExternalCall, RequestRecord
from .engine import fmt_ms


def breakdown(req, externals):
    """[{label, kind, ms, pct}] sorted by time: external services, database, application code."""
    parts = {}
    for e in externals:
        parts[e.service] = parts.get(e.service, 0.0) + e.duration_ms
    rows = [{"label": f"{svc} API", "kind": "http", "ms": ms} for svc, ms in parts.items()]
    if req.db_ms:
        rows.append({"label": "Database", "kind": "db", "ms": req.db_ms})
    # ponytail: assumes external/SQL time does not overlap (true for sync code); concurrent
    # async calls can sum past the wall clock, hence the clamp. Use span intervals if it matters.
    other = max(req.duration_ms - sum(r["ms"] for r in rows), 0.0)
    rows.append({"label": "Django / application code", "kind": "app", "ms": other})
    total = max(sum(r["ms"] for r in rows), 0.001)
    for r in rows:
        r["pct"] = round(r["ms"] / total * 100, 1)
        r["text"] = fmt_ms(r["ms"])
    return sorted(rows, key=lambda r: -r["ms"])


def _typical(route, now):
    agg = metrics.total("http.duration", now - datetime.timedelta(hours=24), now, route=route)
    return agg.quantile(0.5) if agg.count >= 20 else None


def explain(req, externals, queries, exceptions):
    """{"question", "breakdown", "facts", "inference", "confidence", "suspect"} or None if unremarkable."""
    failed = req.status_code >= 500 or bool(exceptions)
    slow_ms = conf.get("REQUESTS", "SLOW_MS")
    typical = _typical(req.route, timezone.now()) if req.route else None
    slow = req.duration_ms >= slow_ms or (typical and req.duration_ms >= 3 * typical and req.duration_ms >= 200)
    if not (failed or slow):
        return None
    parts = breakdown(req, externals)
    top = parts[0]
    facts, score, suspect = [], 0.0, None

    if failed:
        question = "Why did this fail?"
        exc = exceptions[0] if exceptions else None
        if exc:
            facts.append(f"{exc.exc_type} raised in {exc.function or 'unknown'} "
                         f"({exc.file_name.replace(chr(92), '/').rsplit('/', 1)[-1]}:{exc.line_number})")
        bad_ext = [e for e in externals if e.error or e.timeout]
        bad_sql = [q for q in queries if not q.success]
        if bad_ext:
            e = bad_ext[0]
            what = "timed out" if e.timeout else f"failed ({e.status_code or e.exc_type})"
            facts.append(f"Call to {e.service} {what} after {fmt_ms(e.duration_ms)} inside this request")
            suspect, score = f"{e.service} API {'timeout' if e.timeout else 'failure'}", 0.55
            if exc and ("timeout" in exc.exc_type.lower() or e.exc_type == exc.exc_type):
                facts.append(f"The exception type ({exc.exc_type}) matches the failed external call")
                score += 0.15
            since = req.timestamp - datetime.timedelta(hours=1)
            failing = list(RequestRecord.objects.filter(route=req.route, status_code__gte=500, timestamp__gte=since)
                           .values_list("request_id", flat=True)[:200])
            if len(failing) >= 3:
                share = (ExternalCall.objects.filter(request_id__in=failing, service=e.service, error=True)
                         .values("request_id").distinct().count()) / len(failing)
                if share >= 0.5:
                    facts.append(f"The same service failed in {share:.0%} of {len(failing)} failures of this "
                                 "endpoint in the preceding hour")
                    score += 0.2 * share
        elif bad_sql:
            facts.append(f"A SQL statement failed: {bad_sql[0].normalized_sql[:120]}")
            suspect, score = "Database error", 0.65
        elif exc:
            suspect, score = f"Application error ({exc.exc_type})", 0.5
            if any(f.get("in_app") for f in exc.frames or []):
                facts.append("The exception originates in application code, with no failed dependency in the trace")
                score += 0.2
        else:
            suspect, score = "Unknown (no exception was captured for this response)", 0.2
    else:
        question = "Why is this slow?"
        facts.append(f"{top['label']} took {top['text']} of {fmt_ms(req.duration_ms)} ({top['pct']:.0f}%)")
        suspect, score = f"{top['label']} latency", 0.3 + 0.5 * top["pct"] / 100
        if top["kind"] == "db":
            slowest = max(queries, key=lambda q: q.duration_ms, default=None)
            if req.db_count >= 20 and slowest and slowest.duration_ms < req.db_ms / 4:
                fps = {}
                for q in queries:
                    fps[q.fingerprint] = fps.get(q.fingerprint, 0) + 1
                repeats = max(fps.values(), default=0)
                facts.append(f"{req.db_count} queries were executed; the most repeated statement ran {repeats}× "
                             "(possible N+1 pattern)")
                suspect = "Many small queries (possible N+1)"
            elif slowest:
                facts.append(f"Slowest query took {fmt_ms(slowest.duration_ms)}: {slowest.normalized_sql[:120]}")
        if top["kind"] == "http":
            slowest = max(externals, key=lambda e: e.duration_ms, default=None)
            if slowest:
                facts.append(f"Slowest external call: {slowest.method} {slowest.host}{slowest.path} "
                             f"took {fmt_ms(slowest.duration_ms)}")

    if typical:
        facts.append(f"This endpoint's median over the last 24h is {fmt_ms(typical)}; "
                     f"this request took {fmt_ms(req.duration_ms)}")
        if not failed and req.duration_ms >= 3 * typical:
            score += 0.1
    score = min(score, 1.0)
    return {
        "question": question, "breakdown": parts, "facts": facts, "suspect": suspect,
        "confidence": "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.5 else "LOW",
    }
