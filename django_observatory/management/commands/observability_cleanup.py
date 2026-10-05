from django.core.management.base import BaseCommand

from django_observatory import conf, maintenance


class Command(BaseCommand):
    help = "Delete telemetry older than its retention period (bounded batches) and roll up old metrics."

    def add_arguments(self, parser):
        parser.add_argument("--kind", action="append", choices=sorted(conf.DEFAULTS["RETENTION"]),
                            help="Limit to one or more data kinds (default: all).")
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--no-rollup", action="store_true")

    def handle(self, *args, **opts):
        deleted = maintenance.cleanup(opts["kind"], opts["batch_size"])
        for kind, n in deleted.items():
            self.stdout.write(f"{kind:<12} {n:>8} rows deleted (retention {conf.get('RETENTION', kind)} days)")
        if not opts["no_rollup"]:
            self.stdout.write(f"{'metrics':<12} {maintenance.rollup():>8} rows merged by rollup")
        self.stdout.write(self.style.SUCCESS("Cleanup complete."))
