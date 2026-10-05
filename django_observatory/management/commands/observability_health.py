import sys

from django.core.management.base import BaseCommand

from django_observatory import health


class Command(BaseCommand):
    help = "Check the health of the observability pipeline. Exit code 1 when unhealthy."

    def handle(self, *args, **opts):
        report = health.report()
        self.stdout.write("Observability Health\n")
        for c in report["checks"]:
            style = {"HEALTHY": self.style.SUCCESS, "DEGRADED": self.style.WARNING}.get(c["status"], self.style.ERROR)
            self.stdout.write(f"{c['name']:<16} " + style(f"{c['status']:<10}") + f" {c['detail']}")
        self.stdout.write(f"\nOverall: {report['status']}")
        if report["status"] == health.UNHEALTHY:
            sys.exit(1)
