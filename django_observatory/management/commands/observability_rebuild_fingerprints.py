from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min

from django_observatory import fingerprints
from django_observatory.models import DatabaseQuery, ExceptionRecord, Issue


class Command(BaseCommand):
    help = "Recompute SQL and exception fingerprints after the fingerprinting rules changed, and regroup issues."

    def handle(self, *args, **opts):
        changed = 0
        for q in DatabaseQuery.objects.only("id", "sql", "fingerprint").iterator(chunk_size=1000):
            norm = fingerprints.normalize_sql(q.sql)
            fp = fingerprints.sql_fingerprint(norm)
            if fp != q.fingerprint:
                DatabaseQuery.objects.filter(pk=q.pk).update(normalized_sql=norm, fingerprint=fp)
                changed += 1
        self.stdout.write(f"SQL fingerprints updated: {changed}")

        moved = 0
        for exc in ExceptionRecord.objects.select_related("issue").iterator(chunk_size=500):
            fp = fingerprints.exception_fingerprint(exc.exc_type, exc.message, exc.frames or [], exc.endpoint)
            if fp == exc.fingerprint:
                continue
            issue, _ = Issue.objects.get_or_create(fingerprint=fp, defaults={
                "exc_type": exc.exc_type, "title": exc.issue.title, "culprit": exc.issue.culprit,
                "first_seen": exc.timestamp, "last_seen": exc.timestamp})
            ExceptionRecord.objects.filter(pk=exc.pk).update(fingerprint=fp, issue=issue)
            moved += 1
        if moved:
            for row in ExceptionRecord.objects.values("issue_id").annotate(
                    n=Count("id"), first=Min("timestamp"), last=Max("timestamp")):
                Issue.objects.filter(pk=row["issue_id"]).update(
                    occurrence_count=row["n"], first_seen=row["first"], last_seen=row["last"])
            Issue.objects.filter(occurrences__isnull=True).delete()
        self.stdout.write(self.style.SUCCESS(f"Exceptions regrouped: {moved}"))
