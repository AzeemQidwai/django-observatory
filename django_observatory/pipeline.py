"""Asynchronous event pipeline: bounded in-memory queue + one daemon worker thread.

Application threads only ever do ``queue.put_nowait``; all database work happens in
the worker. Under pressure low-priority events are shed first, and nothing here can
raise into, or block, the application.
"""
import atexit
import os
import queue
import threading
import time

from django.db import connections

from . import conf, context, internal
from .storage import get_backend

_BACKOFF = (0.2, 0.5, 1.0, 2.0)


class Pipeline:
    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._q = queue.Queue(maxsize=conf.get("PIPELINE", "QUEUE_SIZE"))
        self._thread = None
        self._pid = None
        self._stop = threading.Event()
        self.stats = {"enqueued": 0, "written": 0, "dropped": 0, "failed": 0, "retries": 0,
                      "last_write": None, "started": None}

    # -- producer side --------------------------------------------------------
    def enqueue(self, kind, row, high=False):
        """Never raises, never blocks. ``high`` marks errors/security/audit: shed last."""
        try:
            if not conf.get("ENABLED"):
                return
            if conf.get("PIPELINE", "SYNC"):
                self._write([(kind, row)], retries=0)
                return
            if self._pid != os.getpid() or self._thread is None or not self._thread.is_alive():
                self._start()
            q = self._q
            if not high and q.qsize() >= q.maxsize * 0.8:  # reserve headroom for high priority
                self.stats["dropped"] += 1
                return
            q.put_nowait((kind, row))
            self.stats["enqueued"] += 1
        except queue.Full:
            self.stats["dropped"] += 1
        except Exception as exc:  # noqa: BLE001
            internal.warn("pipeline.enqueue", "could not enqueue", exc)

    def _start(self):
        with self._lock:
            if self._pid != os.getpid():  # forked worker process: threads do not survive fork
                self._reset()
                self._pid = os.getpid()
                atexit.register(self.shutdown)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="django-observatory", daemon=True)
                self._thread.start()
                self.stats["started"] = time.time()

    # -- worker side ----------------------------------------------------------
    def _run(self):
        context.suppress_thread()
        from . import scheduler

        interval = conf.get("PIPELINE", "FLUSH_INTERVAL")
        size = conf.get("PIPELINE", "BATCH_SIZE")
        while True:
            batch = []
            try:
                batch.append(self._q.get(timeout=interval))
                while len(batch) < size:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            if batch:
                self._write(batch, retries=conf.get("PIPELINE", "MAX_RETRIES"))
                for _ in batch:
                    self._q.task_done()
            try:
                scheduler.maybe_tick()
            except Exception as exc:  # noqa: BLE001
                internal.warn("scheduler", "tick failed", exc)
            if self._stop.is_set() and self._q.empty():
                break
        self._close_connections()

    def _write(self, batch, retries):
        grouped = {}
        for kind, row in batch:
            if kind == "deferred":
                # serialisation work moved off the request thread; the rows it produces
                # come back through enqueue()
                try:
                    with context.suppressed():
                        row()
                except Exception as exc:  # noqa: BLE001
                    internal.warn("pipeline.deferred", "serialisation failed", exc)
                continue
            grouped.setdefault(kind, []).append(row)
        if not grouped:
            return
        backend = get_backend()
        total = sum(len(rows) for rows in grouped.values())
        with context.suppressed():
            for attempt in range(retries + 1):
                try:
                    backend.write_batch(grouped)
                    self.stats["written"] += total
                    self.stats["last_write"] = time.time()
                    return
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    if attempt < retries:
                        self.stats["retries"] += 1
                        self._close_connections()  # a dead connection must not poison the retry
                        time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
            # The batch keeps failing: write each kind on its own so one bad row cannot
            # take unrelated telemetry down with it.
            for kind, rows in grouped.items():
                try:
                    backend.write_batch({kind: rows})
                    self.stats["written"] += len(rows)
                    self.stats["last_write"] = time.time()
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    self.stats["failed"] += len(rows)
                    internal.warn("pipeline.write", f"dropped {len(rows)} '{kind}' rows", error)

    @staticmethod
    def _close_connections():
        for conn in connections.all(initialized_only=True):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- control --------------------------------------------------------------
    def flush(self, timeout=5.0):
        """Block until everything enqueued so far is written (tests, commands, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            if self._thread is None or not self._thread.is_alive():
                return False
            time.sleep(0.01)
        return not self._q.unfinished_tasks

    def shutdown(self, timeout=5.0):
        try:
            if self._pid == os.getpid() and self._thread is not None and self._thread.is_alive():
                self._stop.set()
                self._thread.join(timeout)
        except Exception:  # noqa: BLE001
            pass

    def health(self):
        alive = self._thread is not None and self._thread.is_alive()
        return {**self.stats, "queue_depth": self._q.qsize(), "queue_size": self._q.maxsize,
                "worker_alive": alive, "sync": bool(conf.get("PIPELINE", "SYNC"))}


pipeline = Pipeline()
enqueue = pipeline.enqueue
flush = pipeline.flush


def reset(**_):
    pipeline.shutdown(2.0)
    pipeline._reset()
