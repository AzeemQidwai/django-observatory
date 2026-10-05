import json

from django import template

from ..correlation.engine import fmt_ms
from ..tables import tone as _tone

register = template.Library()


@register.filter
def ms(value):
    try:
        return fmt_ms(float(value))
    except (TypeError, ValueError):
        return "–"


@register.filter
def tone(value, kind="badge"):
    return _tone(kind, value)


@register.filter
def shortid(value):
    value = str(value or "")
    return value[:14] + "…" if len(value) > 15 else value


@register.filter
def pretty(value):
    """Indented JSON for display (the template still HTML-escapes it)."""
    try:
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


@register.filter
def basename(value):
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
