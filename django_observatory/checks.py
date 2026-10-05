"""``manage.py check`` validation of the observability configuration."""
import re

from django.conf import settings
from django.core.checks import Error, Warning, register
from django.utils.module_loading import import_string

from . import conf
from .storage import BUILTIN

MW = "django_observatory.middleware.ObservabilityMiddleware"


@register()
def check_configuration(app_configs, **kwargs):
    out = []
    try:
        conf.reset()
        cfg = conf.all()
    except Exception as exc:  # noqa: BLE001
        return [Error(f"OBSERVABILITY settings could not be loaded: {exc}", id="observability.E001")]

    for key, rate in cfg["SAMPLING"].items():
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 1:
            out.append(Error(f"SAMPLING['{key}'] must be a number between 0 and 1 (got {rate!r}).",
                             id="observability.E002"))
    for key in ("security", "audit"):
        if isinstance(cfg["SAMPLING"].get(key), (int, float)) and cfg["SAMPLING"][key] < 1:
            out.append(Warning(f"SAMPLING['{key}'] is below 1.0: {key} events will be lost.",
                               id="observability.W001"))
    for key, days in cfg["RETENTION"].items():
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            out.append(Error(f"RETENTION['{key}'] must be a positive number of days (got {days!r}).",
                             id="observability.E003"))

    backend = cfg["STORAGE"]["BACKEND"]
    try:
        import_string(BUILTIN.get(backend, backend))
    except Exception:  # noqa: BLE001
        out.append(Error(f"STORAGE['BACKEND'] '{backend}' is not a known backend or importable class.",
                         id="observability.E004"))
    alias = cfg["STORAGE"]["DATABASE_ALIAS"] or "default"
    if alias not in settings.DATABASES:
        out.append(Error(f"STORAGE['DATABASE_ALIAS'] '{alias}' is not defined in DATABASES.", id="observability.E005"))
    elif alias != "default" and not any("ObservabilityRouter" in r for r in map(str, settings.DATABASE_ROUTERS)):
        out.append(Warning(
            "STORAGE['DATABASE_ALIAS'] is set but django_observatory.routers.ObservabilityRouter is not in "
            "DATABASE_ROUTERS; telemetry will still be written to 'default'.", id="observability.W002"))

    count = list(settings.MIDDLEWARE).count(MW)
    if count == 0 and cfg["ENABLED"]:
        out.append(Warning(f"{MW} is not in MIDDLEWARE: requests, traces and SQL correlation are disabled.",
                           id="observability.W003"))
    elif count > 1:
        out.append(Error(f"{MW} is listed {count} times in MIDDLEWARE.", id="observability.E006"))

    if cfg["PERMISSION_MODE"] not in ("staff", "permissions"):
        out.append(Error("PERMISSION_MODE must be 'staff' or 'permissions'.", id="observability.E007"))
    for pattern in cfg["REDACTION"].get("patterns", ()):
        try:
            re.compile(pattern)
        except re.error as exc:
            out.append(Error(f"REDACTION pattern {pattern!r} is not a valid regular expression: {exc}",
                             id="observability.E008"))

    insecure = [name for name, on in (
        ("REQUESTS.CAPTURE_BODY", cfg["REQUESTS"]["CAPTURE_BODY"]),
        ("DATABASE.CAPTURE_PARAMS", cfg["DATABASE"]["CAPTURE_PARAMS"]),
        ("REQUESTS.CAPTURE_QUERY_PARAMS", cfg["REQUESTS"]["CAPTURE_QUERY_PARAMS"]),
    ) if on]
    if insecure and not settings.DEBUG:
        out.append(Warning(
            f"{', '.join(insecure)} enabled with DEBUG=False: personal or secret data may be stored "
            "(values are redacted by key/pattern, but review REDACTION for your data).", id="observability.W004"))
    if cfg["PIPELINE"]["SYNC"] and not settings.DEBUG:
        out.append(Warning("PIPELINE['SYNC'] writes telemetry inside the request; use only for tests.",
                           id="observability.W005"))
    if cfg["OTEL"].get("enabled") and not cfg["OTEL"].get("endpoint"):
        out.append(Error("OTEL is enabled but OTEL['endpoint'] is empty.", id="observability.E009"))
    if cfg["PIPELINE"]["QUEUE_SIZE"] < 100:
        out.append(Error("PIPELINE['QUEUE_SIZE'] must be at least 100.", id="observability.E010"))
    return out
