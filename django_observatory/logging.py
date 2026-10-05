"""Standard-library logging integration.

    "handlers": {"observability": {"class": "django_observatory.logging.ObservabilityHandler"}}

With LOGGING.AUTO_ATTACH (default) the handler is added to the root logger at startup,
so existing ``logger.info(...)`` calls show up with no configuration at all.
"""
import logging
import os
import random
import socket
import time

from . import conf, context, fingerprints, internal, metrics, pipeline, redaction

_HOST = socket.gethostname()
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
_LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}


def level_name(no):
    return _LEVELS.get(no) or ("CRITICAL" if no >= 50 else "ERROR" if no >= 40 else "WARNING" if no >= 30
                               else "INFO" if no >= 20 else "DEBUG")


def emit_event(level_no, message, *, logger_name="", event_type="log", category="application",
               metadata=None, template=None, module="", function="", file_name="", line_number=None,
               thread_id=None, exception_uid=""):
    """Sample -> redact -> enrich -> enqueue. The single entry point for log-like events."""
    is_error = level_no >= logging.WARNING
    metrics.increment("logs", labels={"level": level_name(level_no)})
    rate = conf.get("SAMPLING", "errors" if is_error else "successful_logs", default=1.0)
    if rate < 1.0 and random.random() >= rate:  # decided before any redaction/serialisation work
        return
    ctx = context.current()
    rid, tid, sid = context.ids()
    message = redaction.text(message)
    meta = redaction.clean(metadata) if metadata else {}
    if ctx is not None:
        if ctx.extra:
            meta = {**redaction.clean(ctx.extra), **meta}
        ctx.crumb("log", f"{level_name(level_no)} {message}", level_name(level_no).lower())
    pipeline.enqueue("event", {
        "timestamp": context.to_dt(time.time()),
        "event_type": event_type, "level": level_name(level_no), "level_no": level_no,
        "logger_name": logger_name, "message": message, "category": category,
        "application": conf.get("APPLICATION") or "",
        "user_id": ctx.user_id if ctx else "", "session_id": ctx.session if ctx else "",
        "ip_address": ctx.ip if ctx else "", "hostname": _HOST, "process_id": os.getpid(),
        "thread_id": thread_id, "module": module, "function": function, "file_name": file_name,
        "line_number": line_number, "exception_uid": exception_uid, "metadata": meta,
        "fingerprint": fingerprints.log_fingerprint(logger_name, template or message),
        "request_id": rid, "trace_id": tid, "span_id": sid, **conf.dims(),
    }, high=is_error)


def _category(name):
    if name.startswith("django.security"):
        return "security"
    if name.startswith("django.db"):
        return "database"
    if name.startswith(("django.request", "django.server")):
        return "http"
    return "application"


class ObservabilityHandler(logging.Handler):
    """Never raises and never blocks: failures are reported on the internal logger only."""

    def emit(self, record):
        try:
            name = record.name
            if context.is_suppressed() or name.startswith("django_observatory"):
                return  # our own diagnostics must not re-enter the pipeline
            if name.startswith(tuple(conf.get("LOGGING", "IGNORE_LOGGERS"))) or not conf.get("ENABLED"):
                return
            exception_uid = ""
            if record.exc_info and record.exc_info[1] is not None:
                from .instrumentation.exceptions import capture_exception

                exception_uid = capture_exception(record.exc_info[1], handled=True) or ""
            if name == "django.security.csrf":
                from . import security

                security.record("CSRF_FAILURE", reason=record.getMessage(), severity="warning")
            extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD and not k.startswith("_")}
            extras.pop("request", None)  # django.request attaches the whole HttpRequest
            emit_event(
                record.levelno, record.getMessage(), logger_name=name, category=_category(name),
                metadata=extras, template=str(record.msg), module=record.module, function=record.funcName,
                file_name=record.pathname, line_number=record.lineno, thread_id=record.thread,
                exception_uid=exception_uid,
            )
        except Exception as exc:  # noqa: BLE001
            internal.warn("logging.handler", "emit failed", exc)


class _LastResort(logging.Handler):
    """Reproduces logging.lastResort once we sit on the root logger: print WARNING+ to
    stderr only for records no other handler would have handled."""

    def emit(self, record):
        logger = logging.getLogger(record.name)
        while logger is not None:
            if any(not isinstance(h, (ObservabilityHandler, _LastResort)) for h in logger.handlers):
                return
            logger = logger.parent if logger.propagate else None
        if logging.lastResort is not None:
            logging.lastResort.handle(record)


def auto_attach():
    """Attach to the root logger unless the project already wired the handler itself."""
    root = logging.getLogger()
    loggers = [root] + [lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
    if any(isinstance(h, ObservabilityHandler) for lg in loggers for h in lg.handlers):
        return
    level = logging.getLevelName(str(conf.get("LOGGING", "LEVEL")).upper())
    level = level if isinstance(level, int) else logging.INFO
    handler = ObservabilityHandler(level=level)
    if not root.handlers:
        # Root was unconfigured: keep the stock "warnings to stderr" behaviour that
        # logging.lastResort provided, and let INFO reach us.
        root.addHandler(_LastResort(level=logging.WARNING))
        if root.level > level or root.level == logging.NOTSET:
            root.setLevel(level)
    root.addHandler(handler)
