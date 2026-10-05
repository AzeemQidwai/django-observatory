from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("", views.home),
    path("admin/", admin.site.urls),
    path("observability/", include("django_observatory.urls")),
    path("api/materials/", views.materials),
    path("api/projects/", views.projects),
    path("api/report/", views.report),
    path("api/slow-sql/", views.slow_sql),
    path("api/materials/update/", views.update_material),
    path("api/boom/", views.boom),
    path("api/secret/", views.secret),
    path("sap/stock/", views.sap_stock),
    path("sap/degrade/", views.sap_degrade),
]
