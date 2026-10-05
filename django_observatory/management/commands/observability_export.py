import sys

from django.core.management.base import BaseCommand, CommandError

from django_observatory import exporters, search
from django_observatory.tables import TABLES


class Command(BaseCommand):
    help = "Export observability data (redacted) as JSON, CSV or NDJSON."

    def add_arguments(self, parser):
        parser.add_argument("resource", choices=sorted(TABLES))
        parser.add_argument("--format", choices=sorted(exporters.FORMATS), default="ndjson")
        parser.add_argument("--query", default="", help="Query language filter, e.g. 'level:error'")
        parser.add_argument("--limit", type=int, default=exporters.MAX_ROWS)
        parser.add_argument("--output", help="File path (default: stdout)")

    def handle(self, *args, **opts):
        table = TABLES[opts["resource"]]
        try:
            qs = table.model._default_manager.filter(search.parse(opts["query"], table.fields, table.text))
        except search.QueryError as exc:
            raise CommandError(str(exc)) from None
        qs = qs.order_by(f"-{table.time_field}")
        out = open(opts["output"], "w", encoding="utf-8", newline="") if opts["output"] else sys.stdout  # noqa: SIM115
        try:
            for chunk in exporters.stream(qs, opts["format"], opts["limit"]):
                out.write(chunk)
        finally:
            if opts["output"]:
                out.close()
                self.stderr.write(self.style.SUCCESS(f"Exported to {opts['output']}"))
