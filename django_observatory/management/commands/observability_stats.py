from django.core.management.base import BaseCommand

from django_observatory.pipeline import pipeline
from django_observatory.storage import get_backend


class Command(BaseCommand):
    help = "Show how much observability data is stored."

    def handle(self, *args, **opts):
        self.stdout.write(f"{'Data':<12} {'Rows':>10}  Oldest")
        for kind, s in get_backend().get_statistics().items():
            oldest = s["oldest"].strftime("%Y-%m-%d %H:%M") if s["oldest"] else "-"
            self.stdout.write(f"{kind:<12} {s['rows']:>10}  {oldest}")
        self.stdout.write(f"\nPipeline (this process): {pipeline.health()}")
