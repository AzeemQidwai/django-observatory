import logging
import urllib.request

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.urls import include, path

from django_observatory import span

log = logging.getLogger("tests.app")


def ok(request):
    log.info("ok view called", extra={"password": "SuperSecret123", "normal_field": "hello"})
    return HttpResponse("ok")


def sql(request):
    with span("load_users", department="projects"):
        list(User.objects.filter(username="x", id=123))
        list(User.objects.filter(username="y", id=456))
    return HttpResponse("sql")


def boom(request, pk=0):
    raise ValueError(f"exploded for order {pk} token=abc123secret")


def denied(request):
    raise PermissionDenied


def external(request):
    urllib.request.urlopen("http://sap.internal/stock?apikey=SECRETKEY", timeout=1)  # patched in tests
    return HttpResponse("ext")


async def aview(request):
    return HttpResponse("async")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("observability/", include("django_observatory.urls")),
    path("ok/", ok), path("sql/", sql), path("boom/<int:pk>/", boom), path("denied/", denied),
    path("external/", external), path("async/", aview),
]
