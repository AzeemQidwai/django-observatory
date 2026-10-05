from django.urls import path

from . import views
from .tables import TABLES

app_name = "django_observatory"

_PATHS = {"external_calls": "external/calls", "queries": "database/queries", "alert_history": "alerts/history"}

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("assets/<str:name>", views.static_asset, name="asset"),
    path("search/", views.search_view, name="search"),
    path("logs/<int:pk>/", views.log_detail, name="log_detail"),
    path("requests/<str:request_id>/", views.request_detail, name="request_detail"),
    path("traces/<str:trace_id>/", views.trace_detail, name="trace_detail"),
    path("exceptions/<int:pk>/", views.exception_detail, name="exception_detail"),
    path("issues/<int:pk>/", views.issue_detail, name="issue_detail"),
    path("incidents/<int:pk>/", views.incident_detail, name="incident_detail"),
    path("performance/", views.performance, name="performance"),
    path("database/", views.database, name="database"),
    path("external/", views.external, name="external"),
    path("metrics/", views.metrics_view, name="metrics"),
    path("server/", views.system_view, name="system"),
    path("alerts/", views.alerts_view, name="alerts"),
    path("health/", views.health_view, name="health"),
    path("settings/", views.settings_view, name="settings"),
    path("api/health/", views.api_health, name="api_health"),
    path("api/metrics/", views.api_metrics, name="api_metrics"),
    path("api/<slug:key>/", views.api_list, name="api_list"),
    path("api/<slug:key>/<int:pk>/", views.api_detail, name="api_detail"),
    *[path(f"{_PATHS.get(key, key)}/", views.table_view, {"key": key}, name=key) for key in TABLES],
]
