"""Safe observability query language -> Django Q objects.

    level:error OR level:critical
    category:database AND duration:>500
    status:500 NOT path:/health*  "connection refused"

User input is tokenised and parsed into an AST of whitelisted fields; it is only ever
expressed through ORM lookups. No SQL is built from it.
"""
import re

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
_FIELD = re.compile(r"^([A-Za-z_][\w.]*)(:|!=|>=|<=|=|>|<)(.*)$", re.S)
_OP = re.compile(r"^(!=|>=|<=|=|>|<)(.*)$", re.S)
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50, "fatal": 50}
_LOOKUP = {">": "__gt", ">=": "__gte", "<": "__lt", "<=": "__lte"}
MAX_TERMS = 40


class QueryError(ValueError):
    """Shown to the user as-is: keep messages helpful and free of internals."""


class Field:
    """kind: str (exact, * wildcards) | text (contains) | int | float | bool | level | datetime"""

    def __init__(self, column, kind="str"):
        self.column, self.kind = column, kind


def _tokenize(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise QueryError(f"Could not read the query near: {text[pos:pos + 20]!r}")
        pos = m.end()
        if m.group(1):
            out.append(("(", None))
        elif m.group(2):
            out.append((")", None))
        elif m.group(3) is not None:
            out.append(("str", m.group(3).replace('\\"', '"')))
        else:
            out.append(("word", m.group(4)))
    if len(out) > MAX_TERMS * 3:
        raise QueryError("Query is too long.")
    return out


class _Parser:
    def __init__(self, tokens, fields, text_fields):
        self.t, self.i = tokens, 0
        self.fields, self.text_fields = fields, text_fields
        self.terms = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        if not self.t:
            return Q()
        q = self.or_()
        if self.peek()[0] is not None:
            raise QueryError("Unbalanced ')' in query.")
        return q

    def or_(self):
        q = self.and_()
        while self.peek() == ("word", "OR"):
            self.next()
            q |= self.and_()
        return q

    def and_(self):
        q = self.not_()
        while True:
            kind, val = self.peek()
            if kind is None or kind == ")" or (kind, val) == ("word", "OR"):
                return q
            if (kind, val) == ("word", "AND"):
                self.next()
            q &= self.not_()

    def not_(self):
        if self.peek() == ("word", "NOT"):
            self.next()
            return ~self.not_()
        return self.atom()

    def atom(self):
        kind, val = self.next()
        self.terms += 1
        if self.terms > MAX_TERMS:
            raise QueryError(f"Too many terms (maximum {MAX_TERMS}).")
        if kind is None:
            raise QueryError("The query ends unexpectedly.")
        if kind == "(":
            q = self.or_()
            if self.next()[0] != ")":
                raise QueryError("Missing ')' in query.")
            return q
        if kind == ")":
            raise QueryError("Unexpected ')' in query.")
        if kind == "str":
            return self.text(val)
        m = _FIELD.match(val)
        if not m:
            return self.text(val)
        name, sep, rest = m.groups()
        field = self.fields.get(name.lower())
        if field is None:
            if sep == ":" and ("/" in rest or not rest):  # e.g. a URL, not a field filter
                return self.text(val)
            raise QueryError(f"Unknown field '{name}'. Available: {', '.join(sorted(self.fields))}.")
        op = "=" if sep == ":" else sep
        if sep == ":":
            mo = _OP.match(rest)
            if mo:
                op, rest = mo.groups()
        if rest == "" and self.peek()[0] == "str":  # field:"quoted value"
            rest = self.next()[1]
        return self.compare(name, field, op, rest)

    def text(self, term):
        if not self.text_fields:
            raise QueryError("Free-text search is not available here; use field:value filters.")
        q = Q()
        for col in self.text_fields:
            q |= Q(**{f"{col}__icontains": term})
        return q

    def compare(self, name, field, op, raw):
        col, kind = field.column, field.kind
        negate = op == "!="
        if kind == "level":
            no = _LEVELS.get(raw.lower())
            if no is None:
                raise QueryError(f"Unknown level '{raw}'. Use debug, info, warning, error or critical.")
            col, value = "level_no", no
        elif kind in ("int", "float"):
            try:
                value = int(raw) if kind == "int" else float(raw)
            except ValueError:
                raise QueryError(f"'{name}' needs a number (got '{raw}').") from None
        elif kind == "bool":
            if raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
                raise QueryError(f"'{name}' needs true or false (got '{raw}').")
            value = raw.lower() in ("true", "yes", "1")
        elif kind == "datetime":
            value = parse_datetime(raw) or parse_datetime(raw + "T00:00:00")
            if value is None:
                raise QueryError(f"'{name}' needs a date like 2026-10-04 or 2026-10-04T10:30 (got '{raw}').")
            if getattr(settings, "USE_TZ", False) and timezone.is_naive(value):
                value = timezone.make_aware(value)
        else:
            value = raw
        if op in _LOOKUP:
            if kind in ("str", "text", "bool"):
                raise QueryError(f"'{name}' does not support '{op}'.")
            return Q(**{col + _LOOKUP[op]: value})
        if kind == "text":
            q = Q(**{f"{col}__icontains": value})
        elif kind == "str" and "*" in value:
            core = value.strip("*")
            if value.startswith("*") and value.endswith("*"):
                q = Q(**{f"{col}__icontains": core})
            elif value.endswith("*"):
                q = Q(**{f"{col}__istartswith": core})
            elif value.startswith("*"):
                q = Q(**{f"{col}__iendswith": core})
            else:
                head, _, tail = value.partition("*")
                q = Q(**{f"{col}__istartswith": head, f"{col}__iendswith": tail})
        elif kind == "str":
            q = Q(**{f"{col}__iexact": value})
        else:
            q = Q(**{col: value})
        return ~q if negate else q


def parse(text, fields, text_fields=()):
    """Query string -> Q. Raises QueryError with a user-presentable message.

    ponytail: free text is a case-insensitive substring match (LIKE). Portable to SQLite,
    PostgreSQL and SQL Server with no extra infrastructure; swap in FTS5 / tsvector /
    CONTAINS behind this function if log volume makes substring scans too slow.
    """
    return _Parser(_tokenize(text or ""), fields, tuple(text_fields)).parse()
