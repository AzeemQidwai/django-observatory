"""Centralised settings: ``OBSERVABILITY = {...}`` deep-merged over DEFAULTS.

Flat legacy names from the spec (``OBSERVABILITY_SAMPLING`` etc.) are also honoured.
Runtime overrides stored in ``ObservabilityConfiguration`` are layered on top by the
scheduler (never read from the DB on the request path).
"""
import copy

from django.conf import settings as dj_settings

DEFAULTS = {
    "ENABLED": True,
    "ENVIRONMENT": "development",
    "SERVICE_NAME": "django",
    "APPLICATION": "",
    "RELEASE": "",
    # "staff": any is_staff user (audit still needs an explicit permission).
    # "permissions": every section requires its Django permission.
    "PERMISSION_MODE": "staff",
    "STORAGE": {"BACKEND": "django", "DATABASE_ALIAS": "default", "SQLITE_WAL": True},
    "PIPELINE": {
        "SYNC": False,  # write in the calling thread (tests / debugging only)
        "QUEUE_SIZE": 10000,
        "BATCH_SIZE": 500,
        "FLUSH_INTERVAL": 1.0,
        "MAX_RETRIES": 3,
    },
    "LOGGING": {
        "AUTO_ATTACH": True,
        "LEVEL": "INFO",
        # django.server is runserver's access log: requests are already captured.
        "IGNORE_LOGGERS": ["django.db.backends", "django.utils.autoreload", "django.template", "django.server"],
    },
    "REQUESTS": {
        "ENABLED": True,
        "CAPTURE_QUERY_PARAMS": False,
        "CAPTURE_HEADERS": True,  # always redacted
        "CAPTURE_BODY": False,
        "MAX_BODY_BYTES": 4096,
        "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico"],
        "RESPONSE_HEADERS": True,  # X-Request-ID / X-Trace-ID
        "TRUST_REQUEST_ID_HEADER": False,
        "TRUST_PROXY_HEADERS": False,  # use X-Forwarded-For for client IP
        "SLOW_MS": 1000,
    },
    "DATABASE": {
        "ENABLED": True,
        "SLOW_QUERY_MS": 500,
        "MAX_QUERIES_PER_REQUEST": 200,
        "CAPTURE_PARAMS": False,
    },
    "EXTERNAL_HTTP": {"ENABLED": True, "SERVICES": {}},  # {"sap.corp.local": "SAP"}
    "TRACING": {"ENABLED": True, "MAX_SPANS_PER_TRACE": 500, "TEMPLATES": True},
    "METRICS": {"ENABLED": True, "FLUSH_INTERVAL": 60, "PROCESS": True},
    # Host CPU / memory / disk. DISKS: paths to watch (None = system volume + volumes the project uses).
    "SYSTEM": {"ENABLED": True, "DISKS": None, "CPU_PERCENT": 90, "MEMORY_PERCENT": 90, "DISK_PERCENT": 90},
    "SECURITY": {"ENABLED": True},
    "AUDIT": {"ENABLED": True},
    "ALERTS": {"ENABLED": True, "EMAIL_TO": [], "WEBHOOK_URL": "", "DEFAULT_RULES": True},
    "INCIDENTS": {
        "ENABLED": True,
        "WINDOW_MINUTES": 5,
        "BASELINE_MINUTES": 60,
        "MIN_REQUESTS": 20,
        "RESOLVE_AFTER_MINUTES": 15,
    },
    "SCHEDULER": {"ENABLED": True, "INTERVAL": 60},
    "SAMPLING": {
        "requests": 1.0,
        "successful_logs": 1.0,
        "errors": 1.0,
        "exceptions": 1.0,
        "security": 1.0,
        "audit": 1.0,
    },
    "REDACTION": {"keys": [], "patterns": []},
    "RETENTION": {
        "logs": 30,
        "requests": 30,
        "traces": 14,
        "metrics": 90,
        "exceptions": 180,
        "audit": 365,
        "security": 365,
        "alerts": 180,
    },
    # Control-flow exceptions that are not defects (matched against class names in the MRO).
    "EXCEPTIONS": {"IGNORE": ["Http404", "PermissionDenied", "SuspiciousOperation"]},
    "BREADCRUMBS": 30,
    "OTEL": {"enabled": False, "endpoint": "", "headers": {}, "timeout": 5},
}

# flat setting name -> path inside OBSERVABILITY
_FLAT = {
    "OBSERVABILITY_ENVIRONMENT": ("ENVIRONMENT",),
    "OBSERVABILITY_RELEASE": ("RELEASE",),
    "OBSERVABILITY_SERVICE_NAME": ("SERVICE_NAME",),
    "OBSERVABILITY_SLOW_QUERY_THRESHOLD_MS": ("DATABASE", "SLOW_QUERY_MS"),
    "OBSERVABILITY_SAMPLING": ("SAMPLING",),
    "OBSERVABILITY_REDACTION": ("REDACTION",),
    "OBSERVABILITY_RETENTION": ("RETENTION",),
    "OBSERVABILITY_OTEL": ("OTEL",),
}

# Keys that may be changed at runtime from the Settings page.
RUNTIME_KEYS = {
    ("SAMPLING", "requests"), ("SAMPLING", "successful_logs"),
    ("DATABASE", "SLOW_QUERY_MS"), ("REQUESTS", "SLOW_MS"),
}

_cache = None
_runtime = {}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _norm_retention(r):
    """Accept both ``{"logs": 30}`` and ``{"LOGS_DAYS": 30}``."""
    return {k.lower().removesuffix("_days"): v for k, v in r.items()}


def _load():
    cfg = copy.deepcopy(DEFAULTS)
    user = copy.deepcopy(getattr(dj_settings, "OBSERVABILITY", None) or {})
    if isinstance(user.get("RETENTION"), dict):
        user["RETENTION"] = _norm_retention(user["RETENTION"])
    _merge(cfg, user)
    for name, path in _FLAT.items():
        if hasattr(dj_settings, name):
            val = getattr(dj_settings, name)
            if path == ("RETENTION",):
                val = _norm_retention(val)
            node = cfg
            for p in path[:-1]:
                node = node[p]
            if isinstance(val, dict) and isinstance(node.get(path[-1]), dict):
                _merge(node[path[-1]], val)
            else:
                node[path[-1]] = val
    for path, val in _runtime.items():
        cfg[path[0]][path[1]] = val
    return cfg


def all():  # noqa: A001 - deliberate, read as conf.all()
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def get(*path, default=None):
    node = all()
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node


def enabled(section=None):
    cfg = all()
    if not cfg.get("ENABLED", True):
        return False
    if section is None:
        return True
    sec = cfg.get(section)
    if isinstance(sec, dict):
        return bool(sec.get("ENABLED", True))
    return bool(sec) if sec is not None else False


def set_runtime(overrides):
    """Replace runtime overrides ({(section, key): value}); non-whitelisted keys are ignored."""
    global _runtime
    new = {k: v for k, v in overrides.items() if k in RUNTIME_KEYS}
    if new != _runtime:
        _runtime = new
        reset()


def reset(**_):
    global _cache
    _cache = None


def dims():
    """Deployment dimensions stamped on every record."""
    c = all()
    return {"service": c["SERVICE_NAME"], "environment": c["ENVIRONMENT"], "release": c["RELEASE"]}
