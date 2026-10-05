"""Server (host) resource monitoring: CPU, memory, disk.

Uses psutil when it is installed. Without it, falls back to the standard library:
``shutil.disk_usage`` everywhere, ``/proc`` on Linux and the Win32 API (ctypes) on
Windows. Anything that cannot be read on a platform is simply not reported.

Sampled once per metrics flush by the pipeline worker and stored as gauges labelled
with the host name, so several servers writing to one database stay distinguishable.
"""
import os
import shutil
import socket
import sys

from django.conf import settings

from . import conf, internal, metrics

HOST = socket.gethostname()

try:
    import psutil
except ImportError:  # optional
    psutil = None

_prev_cpu = None  # (idle, total) from the previous sample: CPU % is a delta between two readings


def _cpu_times():
    """(idle, total) cumulative CPU time in arbitrary units, or None if unavailable."""
    if sys.platform.startswith("linux"):
        with open("/proc/stat", encoding="ascii") as f:
            parts = [float(x) for x in f.readline().split()[1:9]]
        return parts[3] + parts[4], sum(parts)  # idle + iowait
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def value(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return float(value(idle)), float(value(kernel) + value(user))  # kernel time includes idle
    return None


def cpu_percent():
    """Average CPU utilisation since the previous call; None on the first call."""
    global _prev_cpu
    if psutil is not None:
        first = _prev_cpu is None
        _prev_cpu = True
        value = psutil.cpu_percent(interval=None)  # psutil's first reading is meaningless too
        return None if first else value
    times = _cpu_times()
    if times is None:
        return None
    prev, _prev_cpu = _prev_cpu, times
    if prev is None or times[1] <= prev[1]:
        return None
    busy = 1 - (times[0] - prev[0]) / (times[1] - prev[1])
    return round(min(max(busy, 0.0), 1.0) * 100, 1)


def memory():
    """{"percent", "used_mb", "total_mb"} or None."""
    if psutil is not None:
        m = psutil.virtual_memory()
        return {"percent": m.percent, "used_mb": (m.total - m.available) / 1048576, "total_mb": m.total / 1048576}
    if sys.platform.startswith("linux"):
        info = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = float(rest.split()[0]) / 1024  # kB -> MB
        total, avail = info["MemTotal"], info["MemAvailable"]
        return {"percent": round((total - avail) / total * 100, 1), "used_mb": total - avail, "total_mb": total}
    if sys.platform == "win32":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        total, avail = stat.ullTotalPhys / 1048576, stat.ullAvailPhys / 1048576
        return {"percent": round((total - avail) / total * 100, 1), "used_mb": total - avail, "total_mb": total}
    return None


def _mount_of(path):
    """The mount point (drive on Windows) that holds ``path``."""
    path = os.path.realpath(path)
    if sys.platform == "win32":
        return os.path.splitdrive(path)[0] + os.sep
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def disk_paths():
    """Volumes to watch: SYSTEM.DISKS if set, else the system volume plus every volume the
    project writes to (project directory, media, SQLite databases), de-duplicated."""
    configured = conf.get("SYSTEM", "DISKS")
    if configured:
        return list(configured)
    candidates = [os.path.abspath(os.sep)]
    for value in (getattr(settings, "BASE_DIR", None), getattr(settings, "MEDIA_ROOT", None)):
        if value:
            candidates.append(str(value))
    for db in settings.DATABASES.values():
        name = str(db.get("NAME") or "")
        if "sqlite" in db.get("ENGINE", "") and name and name != ":memory:" and not name.startswith("file:"):
            candidates.append(os.path.dirname(os.path.abspath(name)))
    mounts = []
    for path in candidates:
        try:
            if os.path.exists(path):
                mount = _mount_of(path)
                if mount not in mounts:
                    mounts.append(mount)
        except OSError:
            continue
    return mounts


def disks():
    """[{"mount", "percent", "free_gb", "total_gb"}]"""
    out = []
    for path in disk_paths():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if usage.total:
            out.append({"mount": path, "percent": round((usage.total - usage.free) / usage.total * 100, 1),
                        "free_gb": usage.free / 1073741824, "total_gb": usage.total / 1073741824})
    return out


def sample():
    """One reading of everything available on this platform. Never raises."""
    out = {"host": HOST, "cpu": None, "memory": None, "disks": [], "load": None, "source": "psutil" if psutil else "stdlib"}
    for key, fn in (("cpu", cpu_percent), ("memory", memory), ("disks", disks)):
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001
            internal.warn(f"system.{key}", "could not be read on this platform", exc, every=3600)
    try:
        out["load"] = os.getloadavg()[0]
    except (AttributeError, OSError):  # not available on Windows
        pass
    return out


def record():
    """Sample and store as gauges. Called by the scheduler in every process."""
    if not conf.enabled("SYSTEM"):
        return None
    s = sample()
    host = {"host": HOST}
    g = metrics.internal_gauge
    if s["cpu"] is not None:
        g("system.cpu_percent", s["cpu"], host)
    if s["memory"]:
        g("system.memory_percent", s["memory"]["percent"], host)
        g("system.memory_used_mb", round(s["memory"]["used_mb"], 1), host)
        g("system.memory_total_mb", round(s["memory"]["total_mb"], 1), host)
    if s["load"] is not None:
        g("system.load1", round(s["load"], 2), host)
    for d in s["disks"] or []:
        labels = {"host": HOST, "mount": d["mount"]}
        g("system.disk_percent", d["percent"], labels)
        g("system.disk_free_gb", round(d["free_gb"], 2), labels)
        g("system.disk_total_gb", round(d["total_gb"], 2), labels)
    return s


def worst(start, end):
    """Peak readings in a window across hosts: {"cpu", "memory", "disk", "disk_mount", ...} (None = no data)."""
    cpu = metrics.by_label("system.cpu_percent", "host", start, end)
    mem = metrics.by_label("system.memory_percent", "host", start, end)
    disk = metrics._scan("system.disk_percent", start, end, lambda n, t, lb: (lb.get("host", ""), lb.get("mount", "")))
    out = {"cpu": None, "cpu_host": "", "memory": None, "memory_host": "", "disk": None, "disk_mount": "", "disk_host": ""}
    for host, a in cpu.items():
        if a.count and (out["cpu"] is None or a.avg > out["cpu"]):
            out["cpu"], out["cpu_host"] = a.avg, host
    for host, a in mem.items():
        if a.count and (out["memory"] is None or a.avg > out["memory"]):
            out["memory"], out["memory_host"] = a.avg, host
    for (host, mount), a in disk.items():
        if a.max is not None and (out["disk"] is None or a.max > out["disk"]):
            out["disk"], out["disk_mount"], out["disk_host"] = a.max, mount, host
    return out
