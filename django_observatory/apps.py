from django.apps import AppConfig
from django.core.signals import setting_changed


class ObservabilityConfig(AppConfig):
    name = "django_observatory"
    verbose_name = "Observability"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks, conf, instrumentation, internal, metrics, pipeline, redaction, signals, storage  # noqa: F401 (checks: registers system checks)
        from . import logging as obs_logging

        def _reset(setting=None, **_):
            if setting is None or setting.startswith("OBSERVABILITY"):
                conf.reset()
                redaction.reset()
                storage.reset()
                pipeline.reset()
                metrics.reset()

        setting_changed.connect(_reset, dispatch_uid="django_observatory.settings", weak=False)

        if not conf.get("ENABLED"):
            return
        try:
            instrumentation.install()
            signals.connect()
            if conf.get("LOGGING", "AUTO_ATTACH"):
                obs_logging.auto_attach()
        except Exception as exc:  # noqa: BLE001 - never prevent the project from starting
            internal.warn("startup", "observability could not initialise", exc)
