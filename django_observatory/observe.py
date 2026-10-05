"""Structured logging API.

    from django_observatory import observe
    observe.info("material_created", material_code="ABC123", quantity=100)
"""
import sys

from . import internal
from .logging import emit_event


def _emit(level_no, event, fields):
    try:
        f = sys._getframe(2)
        category = fields.pop("category", "application")
        emit_event(
            level_no, str(event), logger_name=f.f_globals.get("__name__", ""), event_type="structured",
            category=category, metadata=fields, module=f.f_globals.get("__name__", ""),
            function=f.f_code.co_name, file_name=f.f_code.co_filename, line_number=f.f_lineno,
        )
    except Exception as exc:  # noqa: BLE001
        internal.warn("observe", "emit failed", exc)


def debug(event, **fields):
    _emit(10, event, fields)


def info(event, **fields):
    _emit(20, event, fields)


def warning(event, **fields):
    _emit(30, event, fields)


def error(event, **fields):
    _emit(40, event, fields)


def critical(event, **fields):
    _emit(50, event, fields)
