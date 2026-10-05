"""django-observatory: Django-native, offline-first observability.

Public API (imported lazily so that importing this package never touches Django
before the app registry is ready):

    from django_observatory import observe, span, task, metrics, audit, security
"""
import importlib

__version__ = "0.1.0"

_LAZY = {
    "observe": ("django_observatory.observe", None),
    "metrics": ("django_observatory.metrics", None),
    "audit": ("django_observatory.audit", None),
    "security": ("django_observatory.security", None),
    "span": ("django_observatory.tracing", "span"),
    "task": ("django_observatory.tracing", "task"),
    "bind": ("django_observatory.context", "bind"),
    "capture_exception": ("django_observatory.instrumentation.exceptions", "capture_exception"),
}

__all__ = [*_LAZY, "__version__"]


def __getattr__(name):
    try:
        module, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'django_observatory' has no attribute {name!r}") from None
    value = importlib.import_module(module)
    if attr:
        value = getattr(value, attr)
        globals()[name] = value
    return value
