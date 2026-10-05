"""Optional: keep telemetry out of the application database.

    DATABASES = {"default": {...}, "observability": {"ENGINE": "django.db.backends.sqlite3", "NAME": ...}}
    DATABASE_ROUTERS = ["django_observatory.routers.ObservabilityRouter"]
    OBSERVABILITY = {"STORAGE": {"DATABASE_ALIAS": "observability"}}

then ``python manage.py migrate --database observability``.
"""
from . import conf

APP = "django_observatory"


class ObservabilityRouter:
    @staticmethod
    def _alias():
        return conf.get("STORAGE", "DATABASE_ALIAS") or "default"

    def db_for_read(self, model, **hints):
        return self._alias() if model._meta.app_label == APP else None

    db_for_write = db_for_read

    def allow_relation(self, obj1, obj2, **hints):
        if obj1._meta.app_label == APP and obj2._meta.app_label == APP:
            return True
        return None

    def allow_migrate(self, db, app_label, **hints):
        alias = self._alias()
        if app_label == APP:
            return db == alias
        if alias != "default" and db == alias:
            return False  # a dedicated telemetry database holds nothing but telemetry
        return None
