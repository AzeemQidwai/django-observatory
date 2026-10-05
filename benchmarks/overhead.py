"""Measure the synchronous overhead observability adds to a request.

    python benchmarks/overhead.py            (from the repository root)

Compares the same views with observability disabled and enabled (asynchronous pipeline,
the production configuration), plus logging and SQL-monitoring cost in isolation.
"""
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"

import django  # noqa: E402
from django.conf import settings  # noqa: E402

django.setup()
settings.DATABASES["default"]["NAME"] = os.path.join(tempfile.mkdtemp(), "bench.sqlite3")

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from django_observatory import pipeline  # noqa: E402

N = 400
ASYNC = {"SCHEDULER": {"ENABLED": False}, "LOGGING": {"AUTO_ATTACH": False}}


def timed(fn, n=N):
    for _ in range(20):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return statistics.mean(samples), samples[int(n * 0.95)]


def run(label, fn, variants):
    results = {}
    for name, obs in variants.items():
        with override_settings(OBSERVABILITY=obs):
            results[name] = timed(fn)
            pipeline.flush(30)
    base = results["off"][0]
    print(f"\n{label}")
    for name, (mean, p95) in results.items():
        delta = f"  (+{mean - base:.3f} ms)" if name != "off" else ""
        print(f"  {name:<28} mean {mean:7.3f} ms   p95 {p95:7.3f} ms{delta}")


def main():
    connection.close()
    call_command("migrate", verbosity=0)
    client = Client()
    log = logging.getLogger("bench")
    log.setLevel(logging.INFO)
    log.propagate = False
    from django_observatory.logging import ObservabilityHandler

    log.addHandler(ObservabilityHandler())
    off = {**ASYNC, "ENABLED": False}
    print(f"{N} iterations each, asynchronous pipeline, SQLite file database")
    run("Request with 2 SQL queries (/sql/)", lambda: client.get("/sql/"), {
        "off": off, "on": ASYNC, "on, SQL monitoring disabled": {**ASYNC, "DATABASE": {"ENABLED": False}},
        "on, 10% request sampling": {**ASYNC, "SAMPLING": {"requests": 0.1}}})
    run("Logging: logger.info() with extra", lambda: log.info("bench %s", 1, extra={"k": "v"}), {"off": off, "on": ASYNC})


if __name__ == "__main__":
    main()
