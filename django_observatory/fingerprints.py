"""Stable fingerprints: SQL normalisation and exception grouping.

Normalised SQL is display/grouping text only. It is never executed.
"""
import hashlib
import re
import sys
import sysconfig

_SQL_STR = re.compile(r"'(?:[^']|'')*'")
_SQL_NUM = re.compile(r"(?<![\w.\"`\[])-?\b\d+(?:\.\d+)?\b")
_SQL_PARAM = re.compile(r"%s|%\(\w+\)s|\$\d+|\?")
_SQL_IN = re.compile(r"\b(IN|VALUES)\s*\(\s*\?(?:\s*,\s*\?)*\s*\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))*", re.I)
_WS = re.compile(r"\s+")

_MSG_SUBS = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
)

_STDLIB = sysconfig.get_paths().get("stdlib", "") or "\0"


def _hash(*parts):
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:20]


def normalize_sql(sql, limit=2000):
    s = _SQL_STR.sub("?", sql[: limit * 4])
    s = _SQL_PARAM.sub("?", s)
    s = _SQL_NUM.sub("?", s)
    s = _SQL_IN.sub(lambda m: f"{m.group(1).upper()} (?)", s)
    return _WS.sub(" ", s).strip()[:limit]


def sql_fingerprint(normalized):
    return _hash(normalized)


def normalize_message(message):
    s = str(message)[:500]
    for rx, repl in _MSG_SUBS:
        s = rx.sub(repl, s)
    return _WS.sub(" ", s).strip()


def is_app_frame(filename):
    f = filename.replace("\\", "/")
    return not (
        "site-packages" in f or "dist-packages" in f or f.startswith("<")
        or f.startswith(_STDLIB.replace("\\", "/")) or f.startswith(sys.base_prefix.replace("\\", "/") + "/lib")
        or "/django_observatory/" in f
    )


def culprit_frame(frames):
    """Innermost application frame, else innermost frame. frames: [{file, function, module, line}]"""
    for fr in reversed(frames):
        if fr.get("in_app"):
            return fr
    return frames[-1] if frames else {}


def exception_fingerprint(exc_type, message, frames, route=""):
    fr = culprit_frame(frames)
    # line numbers and raw values are volatile: deliberately excluded
    return _hash(exc_type, normalize_message(message), fr.get("module", ""), fr.get("function", ""), route or "")


def log_fingerprint(logger_name, msg_template):
    return _hash(logger_name, normalize_message(msg_template))
