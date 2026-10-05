"""Built-in observability UI and JSON API. Server-rendered, works offline."""
import datetime
import platform
import sys
from pathlib import Path

import django
from django.db import connections
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import FileResponse, Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import __version__, alerts, conf, exporters, health, maintenance, metrics, permissions, redaction, search, system
from .correlation import causal, graph
from .correlation.engine import fmt_ms
from .models import (
    Alert, AlertRule, AuditEvent, DatabaseQuery, ExceptionRecord, ExternalCall, Incident, Issue, Metric,
    ObservabilityEvent, RequestRecord, SecurityEvent, SpanRecord, TraceRecord,
)
from .models import ObservabilityConfiguration as Cfg
from .permissions import require
from .pipeline import pipeline
from .storage import get_backend
from .tables import TABLES

RANGES = (("15m", 15, "Last 15 minutes"), ("1h", 60, "Last hour"), ("6h", 360, "Last 6 hours"),
          ("24h", 1440, "Last 24 hours"), ("7d", 10080, "Last 7 days"), ("30d", 43200, "Last 30 days"))
PAGE_SIZE = 50
STATIC = {"app.css": "text/css", "app.js": "application/javascript"}
_STATIC_DIR = Path(__file__).parent / "static" / "django_observatory"
# cache-buster: asset URLs change whenever the files do, so upgrades are picked up at once
ASSET_VERSION = format(max(int((_STATIC_DIR / n).stat().st_mtime) for n in STATIC), "x")


# -- helpers --------------------------------------------------------------------
def _range(request, default="1h", allow_all=False):
    key = request.GET.get("range", default)
    if allow_all and key == "all":
        return "all", None, timezone.now()
    minutes = next((m for k, m, _ in RANGES if k == key), None)
    if minutes is None:
        key, minutes = default, next(m for k, m, _ in RANGES if k == default)
    end = timezone.now()
    return key, end - datetime.timedelta(minutes=minutes), end


def _chart(points, unit, **series):
    """points: [(datetime, Agg)]; series: name=function(Agg) -> number."""
    return {"labels": [t.isoformat() for t, _ in points], "unit": unit,
            "series": [{"name": name.replace("_", " "), "values": [round(fn(a), 3) for _, a in points]}
                       for name, fn in series.items()]}


def _page(request, template, context, nav):
    """Render with the shared chrome (navigation, permissions, badges)."""
    context.update(
        nav=nav, perms_obs=permissions.granted(request.user), dims=conf.dims(), version=__version__,
        asset_version=ASSET_VERSION,
        ranges=RANGES, tables=TABLES,
        badges={
            "issues": Issue.objects.filter(status__in=("OPEN", "REGRESSED")).count(),
            "incidents": Incident.objects.exclude(status="RESOLVED").count(),
            "alerts": Alert.objects.filter(status="FIRING").count(),
        },
    )
    return render(request, f"django_observatory/{template}", context)


def _delta(cur, prev):
    if not prev:
        return None
    return (cur - prev) / prev * 100


def _error_rate(by_status):
    total = sum(a.sum for a in by_status.values())
    return (by_status["5xx"].sum / total * 100 if "5xx" in by_status and total else 0.0), total


VOLUME_ROWS = 50000


def _volume(qs, table, start, end):
    """Stacked volume-over-time chart for a filtered list. Returns (chart, truncated).

    ponytail: buckets in Python over at most VOLUME_ROWS (timestamp, category) pairs, newest
    first; portable to every database. Use SQL GROUP BY on a truncated timestamp if lists
    routinely exceed that within one time range.
    """
    column, classify, series = table.chart
    tf = table.time_field
    rows = list(qs.order_by(f"-{tf}").values_list(tf, column)[: VOLUME_ROWS + 1])
    if not rows:
        return None, False
    truncated = len(rows) > VOLUME_ROWS
    rows = rows[:VOLUME_ROWS]
    if start is None or truncated:
        start = rows[-1][0]
    step = metrics.step_for(start, end, 60)
    first = int(start.timestamp()) // step * step
    labels = list(range(first, int(end.timestamp()) + 1, step))
    index = {t: i for i, t in enumerate(labels)}
    values = {name: [0] * len(labels) for name, _ in series}
    for ts, raw in rows:
        i = index.get(int(ts.timestamp()) // step * step)
        name = classify(raw)
        if i is not None and name in values:
            values[name][i] += 1
    from .context import to_dt

    return {"type": "bar", "unit": "", "labels": [to_dt(t).isoformat() for t in labels],
            "series": [{"name": name, "color": color, "values": values[name]}
                       for name, color in series if any(values[name])]}, truncated


def _stacked(name, label, start, end, step, order):
    """Stacked time bars from a counter metric split by one label. order: [(value, colour)]."""
    step = max(int(step), 60)
    got = metrics._scan(name, start, end, lambda n, ts, lb: (int(ts.timestamp()) // step * step, lb.get(label, "")))
    first = int(start.timestamp()) // step * step
    last = int((end - datetime.timedelta(microseconds=1)).timestamp()) // step * step
    buckets = list(range(first, last + 1, step))
    known = [v for v, _ in order]
    extra = sorted({v for _, v in got} - set(known))
    from .context import to_dt

    series = []
    for value, color in [*order, *[(v, "") for v in extra]]:
        vals = [got[(t, value)].sum if (t, value) in got else 0 for t in buckets]
        if any(vals):
            series.append({"name": value or "(none)", "color": color, "values": vals})
    return {"type": "bar", "unit": "", "labels": [to_dt(t).isoformat() for t in buckets], "series": series}


def _distribution(agg, name="requests", color=""):
    """Histogram buckets -> categorical bar chart, trimmed to the occupied range."""
    counts = agg.buckets or []
    used = [i for i, c in enumerate(counts) if c]
    if not used:
        return {"type": "bar", "categorical": True, "unit": "", "labels": [], "series": []}
    lo, hi = max(used[0] - 1, 0), min(used[-1] + 1, len(counts) - 1)
    bounds = metrics.BOUNDS
    labels = [f"≤ {fmt_ms(bounds[i])}" if i < len(bounds) else f"> {fmt_ms(bounds[-1])}" for i in range(lo, hi + 1)]
    return {"type": "bar", "categorical": True, "unit": "", "labels": labels,
            "series": [{"name": name, "color": color, "values": counts[lo: hi + 1]}]}


def _bars(items, kind="db", limit=10):
    """items: [(label, value, text, tip, href)] -> rows for _hbars.html, scaled to the largest."""
    items = sorted(items, key=lambda x: -x[1])[:limit]
    top = max((v for _, v, *_ in items), default=0) or 1
    return [{"label": label, "pct": max(value / top * 100, 0.8), "text": text, "tip": tip, "href": href, "kind": kind}
            for label, value, text, tip, href in items if value > 0]


@require_GET
def static_asset(request, name):
    """Serve our two assets ourselves: no collectstatic / web-server configuration needed."""
    if name not in STATIC:
        raise Http404
    resp = FileResponse((_STATIC_DIR / name).open("rb"), content_type=STATIC[name])
    resp["Cache-Control"] = "public, max-age=3600"
    return resp


# -- dashboard --------------------------------------------------------------------
@require()
def dashboard(request):
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    step = metrics.step_for(start, end)
    prev_start = start - (end - start)

    by_status = metrics.by_label("http.requests", "status", start, end)
    rate, total = _error_rate(by_status)
    prev_rate, prev_total = _error_rate(metrics.by_label("http.requests", "status", prev_start, start))
    lat = metrics.total("http.duration", start, end)
    ext = metrics._scan("ext.calls", start, end, lambda n, t, lb: lb.get("outcome"))
    ext_fail = ext["error"].sum if "error" in ext else 0

    reqs = metrics.timeseries("http.requests", start, end, step)
    errs = dict(metrics.timeseries("http.requests", start, end, step, status="5xx"))
    latency = metrics.timeseries("http.duration", start, end, step)
    perms = permissions.granted(request.user)
    tiles = [
        {"k": "Requests", "v": f"{total:,.0f}", "delta": _delta(total, prev_total), "href": reverse("django_observatory:requests")},
        {"k": "Error rate", "v": f"{rate:.2f}%", "delta": rate - prev_rate if prev_total else None, "pp": True,
         "bad_up": True, "href": reverse("django_observatory:requests") + "?q=status:>=500"},
        {"k": "Avg latency", "v": fmt_ms(lat.avg) if lat.count else "–"},
        {"k": "P95", "v": fmt_ms(lat.quantile(0.95)) if lat.count else "–"},
        {"k": "P99", "v": fmt_ms(lat.quantile(0.99)) if lat.count else "–"},
        {"k": "Slow queries", "v": f"{metrics.total('db.slow_queries', start, end).sum:,.0f}",
         "href": reverse("django_observatory:queries") + "?q=slow:true"},
        {"k": "External API failures", "v": f"{ext_fail:,.0f}", "href": reverse("django_observatory:external")},
        {"k": "Active issues", "v": Issue.objects.filter(status__in=("OPEN", "REGRESSED", "ACKNOWLEDGED")).count(),
         "href": reverse("django_observatory:issues") + "?range=all&q=status:open OR status:regressed"},
        {"k": "Open incidents", "v": Incident.objects.exclude(status="RESOLVED").count(),
         "href": reverse("django_observatory:incidents") + "?range=all"},
    ]
    try:
        system.record()  # a fresh reading, so the tiles are current
    except Exception:  # noqa: BLE001
        pass
    metrics.flush()
    res = system.worst(end - datetime.timedelta(minutes=5), end + datetime.timedelta(minutes=1))
    limits = conf.get("SYSTEM")
    server_url = reverse("django_observatory:system")
    for label, value, limit, note in (
            ("Server CPU", res["cpu"], limits["CPU_PERCENT"], res["cpu_host"]),
            ("Server memory", res["memory"], limits["MEMORY_PERCENT"], res["memory_host"]),
            ("Disk used", res["disk"], limits["DISK_PERCENT"], res["disk_mount"])):
        if value is not None and perms["view_metrics"]:
            tiles.append({"k": label, "v": f"{value:.0f}%", "href": server_url, "note": note,
                          "alarm": value >= limit})
    if perms["view_security_events"]:
        tiles.append({"k": "Security events", "v": f"{metrics.total('security.events', start, end).sum:,.0f}",
                      "href": reverse("django_observatory:security")})
    if perms["view_audit_events"]:
        tiles.append({"k": "Audit events", "v": f"{metrics.total('audit.events', start, end).sum:,.0f}",
                      "href": reverse("django_observatory:audit")})

    routes = metrics.by_label("http.duration", "route", start, end)
    req_url = reverse("django_observatory:requests")

    def route_link(route):
        return f'{req_url}?range={key}&q=route:"{route}"'

    slowest = _bars([(r, a.quantile(0.95), fmt_ms(a.quantile(0.95)),
                      f"P95 {fmt_ms(a.quantile(0.95))} · avg {fmt_ms(a.avg)} · {a.count} requests", route_link(r))
                     for r, a in routes.items() if a.count], "http", 8)
    busiest = _bars([(r, a.count, f"{a.count:,}", f"{a.count:,} requests · avg {fmt_ms(a.avg)}", route_link(r))
                     for r, a in routes.items() if a.count], "db", 8)
    exc_url = reverse("django_observatory:exceptions")
    exc_types = _bars([(t, a.sum, f"{a.sum:,.0f}", f"{a.sum:,.0f} occurrences", f"{exc_url}?range={key}&q=type:{t}")
                       for t, a in metrics.by_label("exceptions", "type", start, end).items()], "critical", 8)
    charts = [
        ("Requests by status", "c-req", _stacked("http.requests", "status", start, end, step,
                                                  [("2xx", "good"), ("3xx", "neutral"), ("4xx", "warning"), ("5xx", "critical")])),
        ("Error rate", "c-err", {"labels": [t.isoformat() for t, _ in reqs], "unit": "%", "series": [{
            "name": "5xx rate", "color": "critical",
            "values": [round(errs[t].sum / a.sum * 100, 2) if a.sum else 0 for t, a in reqs]}]}),
        ("Latency", "c-lat", _chart(latency, "ms", P50=lambda a: a.quantile(0.5), P95=lambda a: a.quantile(0.95))),
        ("Exceptions", "c-exc", {**_chart(metrics.timeseries("exceptions", start, end, step), "", exceptions=lambda a: a.sum),
                                 "type": "bar"}),
        ("Database latency", "c-db", _chart(metrics.timeseries("db.duration", start, end, step), "ms",
                                            average=lambda a: a.avg, P95=lambda a: a.quantile(0.95))),
        ("External API latency", "c-ext", _chart(metrics.timeseries("ext.duration", start, end, step), "ms",
                                                 average=lambda a: a.avg, P95=lambda a: a.quantile(0.95))),
        ("Log volume by level", "c-log", _stacked("logs", "level", start, end, step, [
            ("DEBUG", "neutral"), ("INFO", "s1"), ("WARNING", "warning"), ("ERROR", "serious"), ("CRITICAL", "critical")])),
        ("Response time distribution", "c-dist", _distribution(lat)),
    ]
    return _page(request, "dashboard.html", {
        "range": key, "tiles": tiles, "charts": charts, "slowest": slowest, "busiest": busiest, "exc_types": exc_types,
        "incidents": Incident.objects.exclude(status="RESOLVED").order_by("-detected_at")[:5],
        "issues": Issue.objects.filter(status__in=("OPEN", "REGRESSED")).order_by("-last_seen")[:6],
        "empty": not total and not ObservabilityEvent.objects.exists(),
    }, "dashboard")


# -- generic list / export / api ----------------------------------------------------
def _filtered(request, table, default_range="24h"):
    """(queryset, context) for a table with query language, time range and sorting applied."""
    key, start, end = _range(request, default_range, allow_all=True)
    qs = table.model._default_manager.all()
    if start is not None:
        qs = qs.filter(**{f"{table.time_field}__gte": start})
    q, error = request.GET.get("q", "").strip(), ""
    if q:
        try:
            qs = qs.filter(search.parse(q, table.fields, table.text))
        except search.QueryError as exc:
            error, qs = str(exc), qs.none()
    sort = request.GET.get("sort", "")
    if sort.lstrip("-") in table.sortable:
        qs = qs.order_by(sort, "-pk")
    else:
        sort = ""
        qs = qs.order_by(f"-{table.time_field}", "-pk")
    return qs, {"q": q, "error": error, "range": key, "sort": sort}


def table_view(request, key):
    table = TABLES[key]
    denied = permissions.check(request, table.perm)
    if denied:
        return denied
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    qs, ctx = _filtered(request, table)
    fmt = request.GET.get("export")
    if fmt:
        if fmt not in exporters.FORMATS:
            raise Http404
        return permissions.check(request, "export_observability_data") or exporters.response(qs, fmt, key)
    try:
        page = max(int(request.GET.get("page", 1)), 1)
    except ValueError:
        page = 1
    # ponytail: offset pagination without COUNT(*); switch to keyset if users page very deep
    rows = list(qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE + 1])
    params = request.GET.copy()
    params.pop("page", None)
    sortable = [(label, attr if isinstance(attr, str) and attr in table.sortable else "", kind)
                for label, attr, kind in table.columns]
    ctx.update(table=table, columns=sortable, rows=[table.cells(o) for o in rows[:PAGE_SIZE]], page=page,
               has_next=len(rows) > PAGE_SIZE, qs=params.urlencode(), allow_all=True,
               fields=sorted(table.fields), can_export=permissions.allowed(request.user, "export_observability_data"))
    if table.chart:
        _, start, end = _range(request, "24h", allow_all=True)
        ctx["volume"], truncated = _volume(qs, table, start, end)
        ctx["volume_note"] = f"most recent {VOLUME_ROWS:,} matching rows" if truncated else "matching the current filter"
    if key == "audit" and request.GET.get("verify"):
        ctx["verify"] = AuditEvent.verify_chain()
    return _page(request, "list.html", ctx, key)


def api_list(request, key):
    table = TABLES.get(key)
    if table is None or key == "alert_history":
        raise Http404
    if not permissions.allowed(request.user, table.perm):
        return JsonResponse({"error": "forbidden"}, status=403 if request.user.is_authenticated else 401)
    qs, ctx = _filtered(request, table)
    if ctx["error"]:
        return JsonResponse({"error": ctx["error"]}, status=400)
    try:
        limit = min(max(int(request.GET.get("limit", 100)), 1), 1000)
        offset = max(int(request.GET.get("offset", 0)), 0)
    except ValueError:
        return JsonResponse({"error": "limit and offset must be integers"}, status=400)
    rows = list(qs[offset: offset + limit + 1])
    return JsonResponse({"results": [exporters.serialize(o) for o in rows[:limit]],
                         "next_offset": offset + limit if len(rows) > limit else None})


def api_detail(request, key, pk):
    table = TABLES.get(key)
    if table is None:
        raise Http404
    if not permissions.allowed(request.user, table.perm):
        return JsonResponse({"error": "forbidden"}, status=403 if request.user.is_authenticated else 401)
    return JsonResponse(exporters.serialize(get_object_or_404(table.model, pk=pk)))


def api_metrics(request):
    if not permissions.allowed(request.user, "view_metrics"):
        return JsonResponse({"error": "forbidden"}, status=403 if request.user.is_authenticated else 401)
    name = request.GET.get("name")
    if not name:
        return JsonResponse({"metrics": list(Metric.objects.order_by("name").values("name", "kind", "unit"))})
    key, start, end = _range(request)
    points = metrics.timeseries(name, start, end, metrics.step_for(start, end))
    return JsonResponse({"name": name, "range": key, "points": [
        {"t": t.isoformat(), "count": a.count, "sum": a.sum, "min": a.min, "max": a.max, "avg": a.avg,
         "p95": a.quantile(0.95) if a.buckets else None} for t, a in points]})


def api_health(request):
    if not permissions.allowed(request.user):
        return JsonResponse({"error": "forbidden"}, status=403 if request.user.is_authenticated else 401)
    report = health.report()
    return JsonResponse(report, status=200 if report["status"] != health.UNHEALTHY else 503)


# -- detail pages --------------------------------------------------------------------
def _waterfall(spans, total_ms):
    """Rows for the trace waterfall, depth-first by parent, positioned as % of the trace."""
    total = max(total_ms, 0.001)
    children = {}
    ids = {s.span_id for s in spans}
    for s in spans:
        children.setdefault(s.parent_span_id if s.parent_span_id in ids else "", []).append(s)
    rows = []

    def walk(parent, depth):
        for s in sorted(children.get(parent, []), key=lambda x: x.offset_ms):
            rows.append({"name": s.name, "kind": s.kind, "depth": depth, "status": s.status,
                         "left": min(max(s.offset_ms / total * 100, 0), 99.5),
                         "width": max(min(s.duration_ms / total * 100, 100), 0.4),
                         "duration": fmt_ms(s.duration_ms), "attributes": s.attributes, "pad": depth * 14})
            if depth < 12:
                walk(s.span_id, depth + 1)

    walk("", 0)
    return rows


@require("view_logs")
def log_detail(request, pk):
    event = get_object_or_404(ObservabilityEvent, pk=pk)
    return _page(request, "log_detail.html", {
        "event": event,
        "exception": ExceptionRecord.objects.filter(uid=event.exception_uid).first() if event.exception_uid else None,
        "request_record": RequestRecord.objects.filter(request_id=event.request_id).first() if event.request_id else None,
    }, "logs")


@require("view_requests")
def request_detail(request, request_id):
    req = get_object_or_404(RequestRecord, request_id=request_id)
    spans = list(SpanRecord.objects.filter(trace_id=req.trace_id).order_by("offset_ms")[:600])
    queries = list(DatabaseQuery.objects.filter(request_id=request_id).order_by("timestamp")[:300])
    externals = list(ExternalCall.objects.filter(request_id=request_id).order_by("timestamp")[:100])
    exceptions = list(ExceptionRecord.objects.filter(request_id=request_id).select_related("issue")[:10])
    perms = permissions.granted(request.user)
    return _page(request, "request_detail.html", {
        "req": req, "waterfall": _waterfall(spans, req.duration_ms), "queries": queries, "externals": externals,
        "exceptions": exceptions, "why": causal.explain(req, externals, queries, exceptions),
        "breakdown": causal.breakdown(req, externals),
        "logs": ObservabilityEvent.objects.filter(request_id=request_id).order_by("timestamp")[:200]
        if perms["view_logs"] else [],
        "security": SecurityEvent.objects.filter(request_id=request_id)[:20] if perms["view_security_events"] else [],
        "audits": AuditEvent.objects.filter(request_id=request_id)[:20] if perms["view_audit_events"] else [],
        "graph": graph.request_graph(req),
    }, "requests")


@require("view_traces")
def trace_detail(request, trace_id):
    trace = TraceRecord.objects.filter(trace_id=trace_id).order_by("-duration_ms").first()
    if trace is None:
        raise Http404
    spans = list(SpanRecord.objects.filter(trace_id=trace_id).order_by("offset_ms")[:600])
    kinds = {}
    for s in spans:
        if s.kind in ("db", "http", "template"):
            kinds[s.kind] = kinds.get(s.kind, 0) + s.duration_ms
    top = max(spans, key=lambda s: s.duration_ms if s.kind in ("db", "http") else -1, default=None)
    return _page(request, "trace_detail.html", {
        "trace": trace, "waterfall": _waterfall(spans, trace.duration_ms),
        "dominant": top if top is not None and top.kind in ("db", "http")
        and top.duration_ms >= trace.duration_ms * 0.4 else None,
        "exceptions": ExceptionRecord.objects.filter(trace_id=trace_id)[:10],
        "request_record": RequestRecord.objects.filter(request_id=trace.request_id).first() if trace.request_id else None,
    }, "traces")


@require("view_exceptions")
def exception_detail(request, pk):
    exc = get_object_or_404(ExceptionRecord.objects.select_related("issue"), pk=pk)
    return _page(request, "exception_detail.html", {"exc": exc}, "exceptions")


@require("view_exceptions")
def issue_detail(request, pk):
    issue = get_object_or_404(Issue, pk=pk)
    if request.method == "POST":
        denied = permissions.check(request, "manage_alerts")
        if denied:
            return denied
        status = request.POST.get("status")
        if status in Issue.Status.values and status != Issue.Status.REGRESSED:
            issue.status = status
            issue.resolved_at = timezone.now() if status == Issue.Status.RESOLVED else None
        issue.assignee = request.POST.get("assignee", issue.assignee)[:150]
        issue.notes = request.POST.get("notes", issue.notes)[:10000]
        issue.save()
        return redirect("django_observatory:issue_detail", pk=pk)
    key, start, end = _range(request, "24h")
    occ = issue.occurrences.all()
    step = metrics.step_for(start, end)
    buckets = {}
    for ts in occ.filter(timestamp__gte=start).values_list("timestamp", flat=True)[:20000]:
        b = int(ts.timestamp()) // step * step
        buckets[b] = buckets.get(b, 0) + 1
    first = int(start.timestamp()) // step * step
    labels = list(range(first, int(end.timestamp()) + 1, step))
    from .context import to_dt

    return _page(request, "issue_detail.html", {
        "issue": issue, "range": key, "latest": occ.order_by("-timestamp").first(),
        "recent": occ.order_by("-timestamp")[:15],
        "users": occ.exclude(user_id="").values("user_id").distinct().count(),
        "endpoints": occ.exclude(endpoint="").values("endpoint").annotate(n=Count("id")).order_by("-n")[:10],
        "releases": occ.values("release").annotate(n=Count("id")).order_by("-n")[:5],
        "chart": {"labels": [to_dt(t).isoformat() for t in labels], "unit": "",
                  "series": [{"name": "occurrences", "values": [buckets.get(t, 0) for t in labels]}]},
        "statuses": [s for s in Issue.Status.values if s != "REGRESSED"],
    }, "issues")


@require()
def incident_detail(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    if request.method == "POST":
        denied = permissions.check(request, "manage_alerts")
        if denied:
            return denied
        status = request.POST.get("status")
        if status in Incident.Status.values:
            incident.status = status
            incident.resolved_at = timezone.now() if status == "RESOLVED" else None
        incident.notes = request.POST.get("notes", incident.notes)[:10000]
        incident.save()
        return redirect("django_observatory:incident_detail", pk=pk)
    end = incident.resolved_at or incident.last_seen
    routes = [e["route"] for e in incident.affected_endpoints]
    signals = list(incident.signals.all())
    reqs = RequestRecord.objects.filter(timestamp__gte=incident.started_at, timestamp__lte=end, route__in=routes[:25])
    start = incident.started_at - datetime.timedelta(minutes=30)
    chart_end = min(end + datetime.timedelta(minutes=10), timezone.now())
    step = metrics.step_for(start, chart_end)
    lat = metrics.timeseries("http.duration", start, chart_end, step)
    charts = [("Request latency around the incident", "i-lat",
               _chart(lat, "ms", P50=lambda a: a.quantile(0.5), P95=lambda a: a.quantile(0.95)))]
    if incident.cause_kind == "external":
        charts.append((f"{incident.cause_subject} latency (avg)", "i-ext", _chart(
            metrics.timeseries("ext.duration", start, chart_end, step, service=incident.cause_subject), "ms",
            average=lambda a: a.avg)))
    else:
        charts.append(("HTTP 5xx responses", "i-err", _chart(
            metrics.timeseries("http.requests", start, chart_end, step, status="5xx"), "", errors=lambda a: a.sum)))
    return _page(request, "incident_detail.html", {
        "incident": incident, "charts": charts,
        "observed": [s for s in signals if s.relationship != "evidence"],
        "evidence": [s for s in signals if s.relationship == "evidence"],
        "requests": reqs.order_by("-status_code", "-duration_ms")[:15],
        "exceptions": ExceptionRecord.objects.filter(timestamp__gte=incident.started_at, timestamp__lte=end)
        .values("issue_id", "issue__title").annotate(n=Count("id")).order_by("-n")[:8],
        "alerts": incident.alerts.select_related("rule")[:20],
        "graph": graph.incident_graph(incident), "statuses": Incident.Status.values,
    }, "incidents")


# -- performance / database / external / metrics ---------------------------------------
@require("view_metrics")
def performance(request):
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    lat = metrics.by_label("http.duration", "route", start, end)
    counts = metrics._scan("http.requests", start, end, lambda n, t, lb: (lb.get("route"), lb.get("status")))
    captured = {r["route"]: r for r in RequestRecord.objects.filter(timestamp__gte=start).values("route").annotate(
        db=Avg("db_ms"), ext=Avg("ext_ms"))}
    rows = []
    for route, a in lat.items():
        total = sum(v.sum for (r, _), v in counts.items() if r == route)
        errors = counts[(route, "5xx")].sum if (route, "5xx") in counts else 0
        cap = captured.get(route, {})
        rows.append({"route": route, "count": int(total), "errors": errors / total * 100 if total else 0,
                     "avg": a.avg, "p50": a.quantile(0.5), "p95": a.quantile(0.95), "p99": a.quantile(0.99),
                     "max": a.max or 0, "db": cap.get("db") or 0, "ext": cap.get("ext") or 0})
    sort = request.GET.get("sort", "p95")
    if sort not in ("count", "errors", "avg", "p50", "p95", "p99", "max", "db", "ext"):
        sort = "p95"
    rows.sort(key=lambda r: -r[sort])
    req_url = reverse("django_observatory:requests")

    def link(r):
        return f'{req_url}?range={key}&q=route:"{r["route"]}"'

    step = metrics.step_for(start, end)
    overall = metrics.total("http.duration", start, end)
    return _page(request, "performance.html", {
        "range": key, "rows": rows[:100], "sort": sort,
        "distribution": _distribution(overall),
        "latency": _chart(metrics.timeseries("http.duration", start, end, step), "ms",
                          P50=lambda a: a.quantile(0.5), P95=lambda a: a.quantile(0.95), P99=lambda a: a.quantile(0.99)),
        "slowest": _bars([(r["route"], r["p95"], fmt_ms(r["p95"]),
                           f"P95 {fmt_ms(r['p95'])} · P50 {fmt_ms(r['p50'])} · {r['count']} requests", link(r))
                          for r in rows], "http"),
        "busiest": _bars([(r["route"], r["count"], f"{r['count']:,}",
                           f"{r['count']:,} requests · {r['errors']:.1f}% errors", link(r)) for r in rows], "db"),
        "time_share": _bars([(r["route"], r["avg"] * r["count"], fmt_ms(r["avg"] * r["count"]),
                              f"{fmt_ms(r['avg'] * r['count'])} total server time · {r['count']} × {fmt_ms(r['avg'])}", link(r))
                             for r in rows], "view"),
        "failing": _bars([(r["route"], r["errors"], f"{r['errors']:.1f}%",
                           f"{r['errors']:.1f}% of {r['count']} requests returned 5xx", link(r) + " AND status:>=500")
                          for r in rows], "critical"),
    }, "performance")


@require("view_metrics")
def database(request):
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    agg = metrics.total("db.duration", start, end)
    qs = DatabaseQuery.objects.filter(timestamp__gte=start)
    groups = list(qs.values("fingerprint").annotate(
        calls=Count("id"), avg=Avg("duration_ms"), max=Max("duration_ms"), total=Sum("duration_ms"),
        slow=Count("id", filter=Q(is_slow=True)), failed=Count("id", filter=Q(success=False)),
    ).order_by("-total")[:40])
    for i, g in enumerate(groups):
        sample = qs.filter(fingerprint=g["fingerprint"]).values("normalized_sql", "route").first() or {}
        g["sql"], g["route"] = sample.get("normalized_sql", ""), sample.get("route", "")
        g["p95"] = None
        if i < 15:  # exact P95 from stored rows, only for the heaviest statements
            g["p95"] = (qs.filter(fingerprint=g["fingerprint"]).order_by("-duration_ms")
                        .values_list("duration_ms", flat=True)[int(g["calls"] * 0.05)])
    step = metrics.step_for(start, end)
    return _page(request, "database.html", {
        "range": key, "groups": groups,
        "tiles": [{"k": "Queries", "v": f"{agg.count:,}"}, {"k": "Avg", "v": fmt_ms(agg.avg) if agg.count else "–"},
                  {"k": "P95", "v": fmt_ms(agg.quantile(0.95)) if agg.count else "–"},
                  {"k": "Slowest", "v": fmt_ms(agg.max) if agg.count else "–"},
                  {"k": f"Slow (≥ {conf.get('DATABASE', 'SLOW_QUERY_MS')} ms)",
                   "v": f"{metrics.total('db.slow_queries', start, end).sum:,.0f}",
                   "href": reverse("django_observatory:queries") + "?q=slow:true"}],
        "chart": _chart(metrics.timeseries("db.duration", start, end, step), "ms",
                        average=lambda a: a.avg, P95=lambda a: a.quantile(0.95)),
        "distribution": _distribution(agg, "queries"),
        "volume": {**_chart(metrics.timeseries("db.queries", start, end, step), "", queries=lambda a: a.sum), "type": "bar"},
        "heaviest": _bars([(g["sql"][:70] or g["fingerprint"], g["total"] or 0, fmt_ms(g["total"] or 0),
                            f"{fmt_ms(g['total'] or 0)} total · {g['calls']} calls · avg {fmt_ms(g['avg'] or 0)}",
                            f"{reverse('django_observatory:queries')}?range={key}&q=fingerprint:{g['fingerprint']}")
                           for g in groups], "db", 8),
        "most_called": _bars([(g["sql"][:70] or g["fingerprint"], g["calls"], f"{g['calls']:,}",
                               f"{g['calls']} calls · avg {fmt_ms(g['avg'] or 0)}",
                               f"{reverse('django_observatory:queries')}?range={key}&q=fingerprint:{g['fingerprint']}")
                              for g in groups], "view", 8),
    }, "database")


@require("view_metrics")
def external(request):
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    lat = metrics.by_label("ext.duration", "service", start, end)
    calls = metrics._scan("ext.calls", start, end, lambda n, t, lb: (lb.get("service"), lb.get("outcome")))
    services = []
    for service, a in lat.items():
        err = calls[(service, "error")].sum if (service, "error") in calls else 0
        services.append({"service": service, "calls": a.count, "avg": a.avg, "p95": a.quantile(0.95),
                         "max": a.max or 0, "errors": err / a.count * 100 if a.count else 0})
    services.sort(key=lambda s: -s["calls"])
    selected = request.GET.get("service") or (services[0]["service"] if services else "")
    step = metrics.step_for(start, end)
    ok_series = metrics.timeseries("ext.calls", start, end, step, service=selected, outcome="ok")
    err_series = metrics.timeseries("ext.calls", start, end, step, service=selected, outcome="error")
    return _page(request, "external.html", {
        "range": key, "services": services, "selected": selected,
        "chart": _chart(metrics.timeseries("ext.duration", start, end, step, service=selected), "ms",
                        average=lambda a: a.avg, P95=lambda a: a.quantile(0.95)) if selected else None,
        "outcomes": {"type": "bar", "unit": "", "labels": [t.isoformat() for t, _ in ok_series], "series": [
            {"name": "OK", "color": "good", "values": [a.sum for _, a in ok_series]},
            {"name": "Error", "color": "critical", "values": [a.sum for _, a in err_series]}]} if selected else None,
        "distribution": _distribution(lat[selected], "calls", "s2") if selected in lat else None,
        "by_latency": _bars([(x["service"], x["p95"], fmt_ms(x["p95"]),
                              f"P95 {fmt_ms(x['p95'])} · avg {fmt_ms(x['avg'])} · {x['calls']} calls",
                              f"?service={x['service']}&range={key}") for x in services], "http"),
        "by_errors": _bars([(x["service"], x["errors"], f"{x['errors']:.1f}%",
                             f"{x['errors']:.1f}% of {x['calls']} calls failed",
                             f"?service={x['service']}&range={key}") for x in services], "critical"),
    }, "external")


@require("view_metrics")
def metrics_view(request):
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    names = list(Metric.objects.order_by("name"))
    selected = next((m for m in names if m.name == request.GET.get("name")), None)
    ctx = {"range": key, "metrics": names, "selected": selected}
    if selected:
        step = metrics.step_for(start, end)
        points = metrics.timeseries(selected.name, start, end, step)
        if selected.kind == "histogram":
            ctx["chart"] = _chart(points, selected.unit, average=lambda a: a.avg, P95=lambda a: a.quantile(0.95))
            ctx["distribution"] = _distribution(metrics.total(selected.name, start, end), "samples")
        elif selected.kind == "gauge":
            ctx["chart"] = _chart(points, selected.unit, value=lambda a: a.last if a.last is not None else 0)
        else:
            ctx["chart"] = _chart(points, selected.unit, total=lambda a: a.sum)
        series = metrics._scan(selected.name, start, end,
                               lambda n, t, lb: ", ".join(f"{k}={v}" for k, v in sorted(lb.items())) or "(no labels)")
        ctx["series"] = sorted(({"labels": k, "count": a.count, "sum": a.sum, "avg": a.avg, "max": a.max,
                                 "p95": a.quantile(0.95) if a.buckets else None} for k, a in series.items()),
                               key=lambda r: -r["sum"])[:50]
        ctx["total"] = metrics.total(selected.name, start, end)
    return _page(request, "metrics.html", ctx, "metrics")


def _lines(name, label, start, end, step, where=None, limit=5, unit="%", ceiling=None):
    """One line per value of ``label`` (bucket average) for a gauge metric."""
    step = max(int(step), 60)

    def key(n, ts, lb):
        if where and any(lb.get(k) != v for k, v in where.items()):
            return None
        return int(ts.timestamp()) // step * step, lb.get(label, "")

    got = metrics._scan(name, start, end, key)
    first = int(start.timestamp()) // step * step
    buckets = list(range(first, int(end.timestamp()) + 1, step))
    names = sorted({v for _, v in got})[:limit]
    from .context import to_dt

    # start at the first reading: a gauge has no value before it (0 would read as "0% memory")
    seen = [t for t in buckets if any((t, v) in got for v in names)]
    buckets = [t for t in buckets if seen and t >= seen[0]]
    series = []
    for value in names:
        first = next((got[(t, value)].avg for t in buckets if (t, value) in got), 0.0)
        vals, last = [], first
        for t in buckets:  # a gauge keeps its last value across buckets with no sample
            if (t, value) in got:
                last = got[(t, value)].avg
            vals.append(round(last, 2))
        series.append({"name": value or name, "values": vals})
    out = {"labels": [to_dt(t).isoformat() for t in buckets], "unit": unit, "series": series}
    if ceiling:
        out["max"] = ceiling
    return out


@require("view_metrics")
def system_view(request):
    live = None
    try:
        live = system.record()
    except Exception:  # noqa: BLE001
        pass
    metrics.flush()
    pipeline.flush(1.0)
    key, start, end = _range(request)
    end = end + datetime.timedelta(minutes=1)
    hosts = sorted(set(metrics.by_label("system.memory_percent", "host", start, end))
                   | set(metrics.by_label("system.disk_percent", "host", start, end)))
    host = request.GET.get("host") if request.GET.get("host") in hosts else \
        system.HOST if system.HOST in hosts or not hosts else hosts[0]
    step = metrics.step_for(start, end)
    where = {"host": host}
    limits = conf.get("SYSTEM")
    recent = end - datetime.timedelta(minutes=5)

    def latest(name, **extra):
        agg = metrics.total(name, recent, end, host=host, **extra)
        return agg.last if agg.count else None

    cpu, mem = latest("system.cpu_percent"), latest("system.memory_percent")
    used, total = latest("system.memory_used_mb"), latest("system.memory_total_mb")
    tiles = []
    if cpu is not None:
        tiles.append({"k": "CPU", "v": f"{cpu:.0f}%", "alarm": cpu >= limits["CPU_PERCENT"], "note": "last reading"})
    if mem is not None:
        tiles.append({"k": "Memory", "v": f"{mem:.0f}%", "alarm": mem >= limits["MEMORY_PERCENT"],
                      "note": f"{used / 1024:.1f} of {total / 1024:.1f} GB" if used and total else ""})
    load = latest("system.load1")
    if load is not None:
        tiles.append({"k": "Load (1 min)", "v": f"{load:.2f}", "note": "runnable processes"})
    disks = []
    for mount in sorted(metrics.by_label("system.disk_percent", "mount", recent, end, host=host)):
        pct, free, size = (latest(n, mount=mount) for n in ("system.disk_percent", "system.disk_free_gb", "system.disk_total_gb"))
        if pct is None:
            continue
        alarm = pct >= limits["DISK_PERCENT"]
        tiles.append({"k": f"Disk {mount}", "v": f"{pct:.0f}%", "alarm": alarm,
                      "note": f"{free:.1f} GB free of {size:.0f} GB" if free is not None and size else ""})
        disks.append({"label": mount, "pct": pct, "text": f"{pct:.0f}% · {free:.1f} GB free",
                      "tip": f"{pct:.1f}% used · {free:.1f} GB free of {size:.0f} GB", "href": "",
                      "kind": "critical" if alarm else "db"})
    charts = [
        ("CPU usage", "s-cpu", _lines("system.cpu_percent", "host", start, end, step, where, ceiling=100)),
        ("Memory usage", "s-mem", _lines("system.memory_percent", "host", start, end, step, where, ceiling=100)),
        ("Disk usage by volume", "s-disk", _lines("system.disk_percent", "mount", start, end, step, where, ceiling=100)),
        ("Load average (1 min)", "s-load", _lines("system.load1", "host", start, end, step, where, unit="")),
        ("Application process memory", "s-proc", _lines("process.memory_mb", "pid", start, end, step, unit="MB")),
        ("Application threads", "s-thr", _lines("process.threads", "pid", start, end, step, unit="")),
    ]
    for _, _, data in charts:
        if len(data["series"]) == 1:
            data["series"][0]["name"] = {"%": "usage", "MB": "memory"}.get(data["unit"], "value")
    return _page(request, "system.html", {
        "range": key, "hosts": hosts, "host": host, "tiles": tiles, "disks": disks, "limits": limits,
        "charts": [c for c in charts if c[2]["series"]], "source": (live or {}).get("source", ""),
        "this_host": system.HOST, "have_psutil": system.psutil is not None,
    }, "system")


# -- alerts / health / settings / search ---------------------------------------------
@require()
def alerts_view(request):
    if request.method == "POST":
        denied = permissions.check(request, "manage_alerts")
        if denied:
            return denied
        action, pk = request.POST.get("action"), request.POST.get("id")
        if action in ("ack", "resolve"):
            alert = get_object_or_404(Alert, pk=pk)
            alert.status = Alert.Status.ACKNOWLEDGED if action == "ack" else Alert.Status.RESOLVED
            alert.acknowledged_by = request.user.get_username()
            if action == "resolve":
                alert.resolved_at = timezone.now()
            alert.save()
        elif action == "toggle":
            rule = get_object_or_404(AlertRule, pk=pk)
            rule.enabled = not rule.enabled
            rule.save(update_fields=["enabled"])
        elif action == "delete":
            get_object_or_404(AlertRule, pk=pk).delete()
        elif action == "create":
            try:
                metric = request.POST["metric"]
                if metric not in dict(AlertRule.METRICS) or request.POST.get("operator") not in dict(AlertRule.OPERATORS):
                    raise ValueError
                AlertRule.objects.create(
                    name=request.POST["name"].strip()[:150] or "Unnamed rule", metric=metric,
                    custom_metric=request.POST.get("custom_metric", "")[:150], operator=request.POST["operator"],
                    threshold=float(request.POST["threshold"]),
                    window_minutes=min(max(int(request.POST.get("window_minutes") or 5), 1), 1440),
                    severity=request.POST.get("severity") if request.POST.get("severity") in ("warning", "critical") else "warning",
                    channels=[c for c in request.POST.getlist("channels") if c in alerts.CHANNELS],
                )
            except Exception:  # noqa: BLE001 - invalid form input: re-render with a message
                return _alerts_page(request, "Could not create the rule: check the name is unique and the numbers are valid.")
        return redirect("django_observatory:alerts")
    return _alerts_page(request)


def _alerts_page(request, error=""):
    alerts.ensure_default_rules()
    return _page(request, "alerts.html", {
        "error": error, "active": Alert.objects.exclude(status="RESOLVED").select_related("rule", "incident").order_by("-started_at")[:50],
        "rules": AlertRule.objects.order_by("name"), "metric_choices": AlertRule.METRICS,
        "channels": sorted(alerts.CHANNELS), "can_manage": permissions.allowed(request.user, "manage_alerts"),
    }, "alerts")


@require()
def health_view(request):
    return _page(request, "health.html", {"report": health.report()}, "health")


@require("manage_observability_settings")
def settings_view(request):
    message = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "runtime":
            values = {}
            for section, name in conf.RUNTIME_KEYS:
                raw = request.POST.get(f"{section}.{name}", "").strip()
                if raw:
                    try:
                        val = float(raw)
                    except ValueError:
                        continue
                    if section == "SAMPLING" and not 0 <= val <= 1:
                        continue
                    values[f"{section}.{name}"] = val if section == "SAMPLING" else int(val)
            Cfg.objects.update_or_create(key="runtime", defaults={
                "value": values, "updated_at": timezone.now(), "updated_by": request.user.get_username()})
            conf.set_runtime({tuple(k.split(".")): v for k, v in values.items()})
            message = "Runtime overrides saved. Other worker processes pick them up within a minute."
        elif action == "cleanup":
            denied = permissions.check(request, "delete_observability_data")
            if denied:
                return denied
            deleted = maintenance.cleanup()
            message = f"Retention cleanup removed {sum(deleted.values())} rows."
    cfg = redaction.clean(conf.all())
    row = Cfg.objects.filter(key="runtime").first()
    runtime = (row.value if row else None) or {}
    return _page(request, "settings.html", {
        "message": message, "config": cfg,
        "runtime": [{"key": f"{s}.{k}", "value": runtime.get(f"{s}.{k}", ""), "effective": conf.get(s, k)}
                    for s, k in sorted(conf.RUNTIME_KEYS)],
        "stats": get_backend().get_statistics(), "retention": conf.get("RETENTION"),
        "system": {
            "django-observatory": __version__, "Django": django.get_version(),
            "Python": sys.version.split()[0], "Platform": platform.platform(),
            "Storage database": f"{connections[conf.get('STORAGE', 'DATABASE_ALIAS') or 'default'].vendor} "
                                f"(alias '{conf.get('STORAGE', 'DATABASE_ALIAS') or 'default'}')",
            "Server mode": "ASGI" if hasattr(request, "scope") else "WSGI",
        },
    }, "settings")


@require()
def search_view(request):
    q = request.GET.get("q", "").strip()
    key, start, end = _range(request, "24h", allow_all=True)
    results, perms = [], permissions.granted(request.user)
    if q:
        for name in ("issues", "exceptions", "requests", "logs", "traces", "security", "external_calls", "incidents"):
            table = TABLES[name]
            if not perms[table.perm]:
                continue
            try:
                cond = search.parse(q, table.fields, table.text)
            except search.QueryError:
                continue  # the query uses fields this resource does not have
            qs = table.model._default_manager.filter(cond)
            if start is not None:
                qs = qs.filter(**{f"{table.time_field}__gte": start})
            rows = list(qs.order_by(f"-{table.time_field}")[:6])
            if rows:
                results.append({"table": table, "columns": table.columns, "rows": [table.cells(o) for o in rows[:5]],
                                "more": len(rows) > 5})
    return _page(request, "search.html", {"q": q, "range": key, "results": results, "allow_all": True}, "search")
