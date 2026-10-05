# Server monitoring

CPU, memory and disk of the machine running the application, so that a hardware or capacity problem is
recognised as such instead of looking like an application fault.

## What is collected

| Reading | Metric | Notes |
|---|---|---|
| CPU utilisation | `system.cpu_percent` | all cores, averaged since the previous sample |
| Memory used | `system.memory_percent`, `system.memory_used_mb`, `system.memory_total_mb` | physical memory; "used" = total − available |
| Load average | `system.load1` | Unix only |
| Disk usage per volume | `system.disk_percent`, `system.disk_free_gb`, `system.disk_total_gb` | one series per watched volume |
| Application process | `process.threads`, `process.memory_mb`, `process.cpu_percent` | memory and CPU need psutil |

Readings are taken once per `METRICS.FLUSH_INTERVAL` (60 s) and labelled with the host name, so several
servers writing to one telemetry database are shown separately (a selector appears on the Server page).

## How readings are obtained

| Reading | With psutil | Without psutil |
|---|---|---|
| Disk | `shutil.disk_usage` | `shutil.disk_usage` (always available) |
| CPU, memory on Linux | psutil | `/proc/stat`, `/proc/meminfo` |
| CPU, memory on Windows | psutil | Win32 API (`GetSystemTimes`, `GlobalMemoryStatusEx`) |
| CPU, memory on macOS and others | psutil | not available |
| Per-process memory and CPU | psutil | not available |

`pip install "django-observatory[process]"` installs psutil. It is recommended on Windows servers. Anything
that cannot be read is simply not reported; nothing fails.

## Which disks are watched

By default: the system volume, plus every volume the project writes to — `BASE_DIR`, `MEDIA_ROOT` and the
directories of SQLite databases — de-duplicated by mount point (drive letter on Windows).

To choose explicitly:

```python
OBSERVABILITY = {"SYSTEM": {"DISKS": ["C:\\", "D:\\", "E:\\uploads"]}}       # Windows
OBSERVABILITY = {"SYSTEM": {"DISKS": ["/", "/var/lib/app", "/mnt/media"]}}   # Linux
```

## Where it shows up

- **Monitoring → Server**: current values, disk-space bars, and history for CPU, memory, disk per volume,
  load and the application process.
- **Dashboard**: tiles for the worst CPU, memory and disk in the last five minutes, flagged HIGH above the
  threshold.
- **Health** page and `python manage.py observability_health`: a *Server Resources* line. Disk at or above
  97% makes health UNHEALTHY (exit code 1 for the command).
- **Alerts**: *Disk almost full*, *Server memory pressure*, *Server CPU saturated* (see [alerts](alerts.md)).
- **Incidents**: see below.

## Thresholds

```python
OBSERVABILITY = {"SYSTEM": {"CPU_PERCENT": 85, "MEMORY_PERCENT": 90, "DISK_PERCENT": 80}}
```

These levels drive the HIGH flags, the health status and incident detection. The alert rules have their own
thresholds, editable on the Alerts page.

## Incidents caused by the server

The incident engine treats sustained saturation as a symptom and a possible cause:

- CPU or memory whose **average over the window** is at or above its threshold
- any watched volume at or above `DISK_PERCENT`

A nearly full disk opens an incident by itself, before anything fails. When requests are slow or failing at
the same time, the *server resources* hypothesis gains evidence from: the rise above the preceding hour,
the simultaneous request impact, resource-related exceptions (`OSError`, `MemoryError`, database errors),
and the absence of a degraded external service. The incident then reads, for example, *"CPU saturation on
server app-01"* with the recommendation to look for runaway processes or add capacity.

If an external dependency is degraded at the same time and explains more of the failing requests, that
remains the probable cause and the resource reading is listed as a correlated signal.

## Limits

- Sampling happens in the application's worker thread, which runs in a process that has handled at least
  one request. A completely idle server shows a gap, and a server that is down reports nothing: this is
  monitoring from inside the application, not an external uptime check.
- CPU is an average between samples; short spikes within a minute are smoothed out.
- It measures the host the application runs on. A separate database server is not measured.
- Inside a container the values are those the container can see.

## Disabling

```python
OBSERVABILITY = {"SYSTEM": {"ENABLED": False}}
```
