"""Application Observability Graph: entities and relationships assembled from ordinary
relational rows (no graph database). Output: {"nodes": [...], "edges": [[from, to]]}.
"""
import urllib.parse

from django.urls import reverse

from ..models import ExceptionRecord, ExternalCall, Issue, RequestRecord, SpanRecord


class Graph:
    def __init__(self):
        self.nodes, self.edges = {}, []

    def node(self, node_id, label, kind, href="", detail=""):
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, "label": str(label)[:48], "kind": kind, "href": href,
                                   "detail": str(detail)[:120]}
        return node_id

    def edge(self, a, b):
        if a != b and [a, b] not in self.edges:
            self.edges.append([a, b])

    def data(self):
        return {"nodes": list(self.nodes.values()), "edges": self.edges}


def _url(name, *args):
    return reverse(f"django_observatory:{name}", args=args)


def add_request(g, req, parent=None, max_spans=14):
    """Request -> Trace -> View / SQL / External API, and Request -> User / Exception -> Issue."""
    rid = g.node(f"req:{req.request_id}", f"{req.method} {req.route or req.path}", "request",
                 _url("request_detail", req.request_id), f"{req.status_code} · {req.duration_ms:.0f} ms")
    if parent:
        g.edge(parent, rid)
    if req.username or req.user_id:
        g.edge(rid, g.node(f"user:{req.user_id}", req.username or f"user {req.user_id}", "user"))
    tid = g.node(f"trace:{req.trace_id}", f"Trace {req.trace_id[:8]}", "trace", _url("trace_detail", req.trace_id))
    g.edge(rid, tid)
    spans = (SpanRecord.objects.filter(trace_id=req.trace_id, kind__in=("view", "db", "http", "template"))
             .order_by("-duration_ms")[:max_spans])
    for s in spans:
        kind = {"db": "query", "http": "external"}.get(s.kind, s.kind)
        key = f"ext:{s.attributes.get('service')}" if s.kind == "http" else f"span:{s.span_id}"
        g.edge(tid, g.node(key, s.name, kind, "", f"{s.duration_ms:.0f} ms" + (" · error" if s.status == "error" else "")))
    for exc in ExceptionRecord.objects.filter(request_id=req.request_id).select_related("issue")[:3]:
        eid = g.node(f"exc:{exc.uid}", exc.exc_type, "exception", _url("exception_detail", exc.pk), exc.message)
        g.edge(rid, eid)
        g.edge(eid, g.node(f"issue:{exc.issue_id}", f"Issue #{exc.issue_id}", "issue",
                           _url("issue_detail", exc.issue_id), exc.issue.title))
    return rid


def request_graph(req):
    g = Graph()
    add_request(g, req)
    return g.data()


def incident_graph(incident):
    """Incident -> signals (metrics / external services / issues) -> example failing requests."""
    g = Graph()
    root = g.node(f"inc:{incident.pk}", f"Incident #{incident.pk}", "incident", "", incident.title)
    for sig in incident.signals.exclude(relationship="evidence"):
        if sig.ref_kind == "external":
            nid = g.node(f"ext:{sig.source}", sig.source, "external",
                         _url("external") + f"?service={urllib.parse.quote_plus(sig.source)}",
                         sig.description)
        elif sig.ref_kind == "exception":
            issue = Issue.objects.filter(exc_type=sig.source).order_by("-last_seen").first()
            nid = g.node(f"issue:{issue.pk}" if issue else f"exc:{sig.source}",
                         f"Issue #{issue.pk}" if issue else sig.source, "issue",
                         _url("issue_detail", issue.pk) if issue else "", issue.title if issue else sig.description)
        else:
            nid = g.node(f"metric:{sig.kind}", sig.kind.replace("_", " ").title(), "metric", "", sig.description)
        g.edge(root, nid)
    if incident.cause_kind == "exception" and incident.cause_subject:
        for sig in incident.signals.filter(ref_kind="issue")[:1]:
            issue = Issue.objects.filter(pk=sig.ref_id).first() if sig.ref_id.isdigit() else None
            if issue:
                g.edge(root, g.node(f"issue:{issue.pk}", f"Issue #{issue.pk}", "issue",
                                    _url("issue_detail", issue.pk), issue.title))
    if incident.cause_kind == "deployment":
        g.edge(root, g.node(f"release:{incident.cause_subject}", f"Release {incident.cause_subject}", "release"))
    routes = [e["route"] for e in incident.affected_endpoints][:20]
    end = incident.resolved_at or incident.last_seen
    examples = RequestRecord.objects.filter(timestamp__gte=incident.started_at, timestamp__lte=end, route__in=routes)
    if incident.cause_kind == "external":
        ids = list(ExternalCall.objects.filter(timestamp__gte=incident.started_at, service=incident.cause_subject)
                   .order_by("-duration_ms").values_list("request_id", flat=True)[:50])
        examples = examples.filter(request_id__in=ids)
    for req in examples.order_by("-status_code", "-duration_ms")[:2]:
        add_request(g, req, parent=root, max_spans=5)
    return g.data()
