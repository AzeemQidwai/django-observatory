"""Streaming exports: JSON, CSV, NDJSON. Rows are redacted again on the way out, so
redaction rules added after ingestion also apply to historical data."""
import csv
import datetime
import json

from django.http import StreamingHttpResponse

from . import redaction

FORMATS = {"json": "application/json", "csv": "text/csv", "ndjson": "application/x-ndjson"}
MAX_ROWS = 50000


def serialize(obj, exclude=()):
    """Model instance -> JSON-safe, redacted dict of its concrete fields."""
    out = {}
    for f in obj._meta.concrete_fields:
        if f.attname in exclude:
            continue
        v = getattr(obj, f.attname)
        if isinstance(v, (datetime.datetime, datetime.date)):
            v = v.isoformat()
        out[f.attname] = redaction.REDACTED if redaction.is_sensitive_key(f.attname) else redaction.clean(v)
    return out


class _Echo:
    def write(self, value):
        return value


def _csv_safe(v):
    """Neutralise spreadsheet formula injection and flatten structures."""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, default=str)
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        v = "'" + v
    return v


def _rows(queryset, limit):
    for obj in queryset[:limit].iterator(chunk_size=500):
        yield serialize(obj)


def stream(queryset, fmt, limit=MAX_ROWS):
    """Generator of text chunks."""
    rows = _rows(queryset, min(limit, MAX_ROWS))
    if fmt == "ndjson":
        for r in rows:
            yield json.dumps(r, default=str) + "\n"
    elif fmt == "json":
        yield "["
        for i, r in enumerate(rows):
            yield ("," if i else "") + json.dumps(r, default=str)
        yield "]"
    elif fmt == "csv":
        writer = csv.writer(_Echo())
        names = [f.attname for f in queryset.model._meta.concrete_fields]
        yield writer.writerow(names)
        for r in rows:
            yield writer.writerow([_csv_safe(r.get(n)) for n in names])
    else:
        raise ValueError(f"Unknown export format: {fmt}")


def response(queryset, fmt, name):
    resp = StreamingHttpResponse(stream(queryset, fmt), content_type=FORMATS[fmt] + "; charset=utf-8")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    resp["Content-Disposition"] = f'attachment; filename="observability-{name}-{stamp}.{fmt}"'
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
