from django.utils.module_loading import import_string

from .. import conf
from .base import ObservabilityBackend

BUILTIN = {"django": "django_observatory.storage.django.DjangoORMBackend"}

_backend = None


def get_backend():
    global _backend
    if _backend is None:
        name = conf.get("STORAGE", "BACKEND")
        _backend = import_string(BUILTIN.get(name, name))()
    return _backend


def reset(**_):
    global _backend
    _backend = None


__all__ = ["ObservabilityBackend", "get_backend", "reset"]
