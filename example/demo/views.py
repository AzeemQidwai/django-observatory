"""Demo application: a materials API that depends on a (simulated) SAP service."""
import logging
import time
import urllib.request

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse, JsonResponse

from django_observatory import audit, metrics, observe, span

from .models import Material

logger = logging.getLogger(__name__)


def _seed():
    """Demo data on first use, so the project works straight after ``migrate``."""
    if not Material.objects.exists():
        Material.objects.bulk_create(
            [Material(code=f"A{1000 + i}", name=f"Material {i}", quantity=100 + i) for i in range(40)])


def home(request):
    _seed()
    return HttpResponse(
        "<h1>Demo project</h1><p><a href='/observability/'>Open observability</a> (log in as admin / admin)</p>"
        "<ul><li><a href='/api/materials/'>/api/materials/</a> (calls SAP)</li>"
        "<li><a href='/api/projects/'>/api/projects/</a></li><li><a href='/api/report/'>/api/report/</a> (N+1)</li>"
        "<li><a href='/api/slow-sql/'>/api/slow-sql/</a></li><li><a href='/api/boom/'>/api/boom/</a></li>"
        "<li><a href='/api/secret/'>/api/secret/</a> (403)</li>"
        "<li><a href='/sap/degrade/?on=1'>degrade SAP</a> / <a href='/sap/degrade/?on=0'>restore SAP</a></li></ul>")


def sap_stock(request):
    """Stands in for the external SAP system. /sap/degrade/ makes it slow."""
    time.sleep(4.5 if cache.get("sap_degraded") else 0.03)
    return JsonResponse({"stock": 42})


def sap_degrade(request):
    cache.set("sap_degraded", request.GET.get("on") == "1", None)
    return JsonResponse({"degraded": bool(cache.get("sap_degraded"))})


def _sap(request, timeout=2.0):
    with urllib.request.urlopen(request.build_absolute_uri("/sap/stock/"), timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def materials(request):
    items = list(Material.objects.values("code", "name", "quantity")[:20])
    _sap(request)  # raises TimeoutError when SAP is degraded -> HTTP 500
    metrics.increment("materials.listed")
    logger.info("materials listed", extra={"count": len(items)})
    return JsonResponse({"materials": items})


def projects(request):
    with span("load_projects", department="projects"):
        count = Material.objects.count()
    observe.info("projects_viewed", count=count)
    return JsonResponse({"projects": count})


def report(request):
    with span("generate_monthly_report", report_type="monthly"):
        rows = [Material.objects.get(pk=m.pk).quantity for m in Material.objects.all()[:30]]  # deliberate N+1
    return JsonResponse({"total": sum(rows)})


def slow_sql(request):
    with connection.cursor() as cur:
        cur.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 2500000) SELECT max(x) FROM c")
        value = cur.fetchone()[0]
    return JsonResponse({"value": value})


def update_material(request):
    _seed()
    m = Material.objects.first()
    before = m.quantity
    m.quantity = max(before - 40, 0)
    m.save()
    audit.record("UPDATE", "Material", m.code, changes={"quantity": {"before": before, "after": m.quantity}})
    logger.warning("Stock level is low for %s", m.code)
    return JsonResponse({"code": m.code, "quantity": m.quantity})


def boom(request):
    logger.error("Material import failed token=abc123secret")
    raise ValueError("Material import failed for batch 4711")


def secret(request):
    raise PermissionDenied
