"""Safe fallback reporting for failures inside observability itself.

Uses a dedicated non-propagating logger that the ObservabilityHandler ignores, so an
observability failure can never be fed back into the pipeline (no recursion).
"""
import functools
import logging
import sys
import time

INTERNAL_LOGGER = "django_observatory.internal"

log = logging.getLogger(INTERNAL_LOGGER)
log.propagate = False
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[django-observatory] %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.WARNING)

_last = {}
errors = {"count": 0, "last": "", "last_at": None}


def warn(key, message, exc=None, every=30.0):
    """Report at most once per ``every`` seconds per key. Never raises."""
    try:
        errors["count"] += 1
        errors["last"] = f"{key}: {message}" + (f" ({type(exc).__name__}: {exc})" if exc else "")
        errors["last_at"] = time.time()
        now = time.monotonic()
        if now - _last.get(key, -1e9) >= every:
            _last[key] = now
            log.warning("%s", errors["last"])
    except Exception:
        pass


def safe(fn):
    """Decorator: the wrapped function can never raise into application code."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            warn(fn.__qualname__, "failed", exc)
            return None

    return wrapper
