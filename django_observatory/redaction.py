"""The single redaction engine. Everything that is stored, exported or returned by the
API passes through ``clean`` (structures) or ``text`` (free text)."""
import re

from . import conf

REDACTED = "[REDACTED]"

DEFAULT_KEYS = (
    "password", "passwd", "secret", "token", "access_token", "refresh_token", "api_key",
    "apikey", "authorization", "cookie", "sessionid", "csrf", "client_secret",
    "private_key", "credential", "credentials", "signature", "otp", "pin_code",
    "cvv", "cvc", "ssn",
)

_KV_KEYS = (
    r"(?:[\w.-]*?(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|sessionid|csrf\w*))"
)
DEFAULT_PATTERNS = (
    # Authorization / Cookie header lines
    (re.compile(r"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie|x-api-key)(\s*[:=]\s*)[^\r\n]+"), r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}"), r"\1 " + REDACTED),
    # key=value / "key": "value"
    (re.compile(r"(?i)([\"']?\b(?:%s)\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}\]]+)" % _KV_KEYS), r"\1" + REDACTED),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), REDACTED),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
    # credentials in URLs
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:)[^\s/@]+@"), r"\1" + REDACTED + "@"),
)

MAX_DEPTH = 6
MAX_ITEMS = 60
MAX_STR = 4000

_compiled = None


def _engine():
    global _compiled
    if _compiled is None:
        cfg = conf.get("REDACTION") or {}
        keys = tuple(dict.fromkeys(k.lower().replace("-", "_") for k in (*DEFAULT_KEYS, *cfg.get("keys", ()))))
        patterns = list(DEFAULT_PATTERNS)
        for p in cfg.get("patterns", ()):
            try:
                patterns.append((re.compile(p), REDACTED))
            except re.error:
                pass  # reported by the system check
        _compiled = (keys, patterns)
    return _compiled


def reset(**_):
    global _compiled
    _compiled = None


def is_sensitive_key(key):
    k = str(key).lower().replace("-", "_")
    return any(s in k for s in _engine()[0])


def text(value, limit=MAX_STR):
    """Redact secrets embedded in free text and bound its size."""
    if not value:
        return "" if value is None else value
    s = value if isinstance(value, str) else str(value)
    if len(s) > limit * 4:  # don't regex megabytes
        s = s[: limit * 4]
    for rx, repl in _engine()[1]:
        s = rx.sub(repl, s)
    return s if len(s) <= limit else s[:limit] + "…"


def clean(value, _depth=0):
    """Redact + bound + make JSON-serialisable. Sensitive keys lose their value entirely."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return text(value)
    if _depth >= MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["…"] = f"{len(value) - MAX_ITEMS} more"
                break
            k = str(k)[:200]
            out[k] = REDACTED if is_sensitive_key(k) else clean(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [clean(v, _depth + 1) for v in list(value)[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"… {len(value) - MAX_ITEMS} more")
        return items
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    try:
        return text(str(value))
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def headers(meta):
    """HTTP headers from request.META / a mapping, sensitive ones redacted."""
    out = {}
    for k, v in meta.items():
        if k.startswith("HTTP_"):
            name = k[5:].replace("_", "-").title()
        elif k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            name = k.replace("_", "-").title()
        else:
            continue
        out[name] = REDACTED if is_sensitive_key(name) else text(v, 500)
    return out
