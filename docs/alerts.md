# Alerts

An alert rule compares a measurement over a time window with a threshold. Rules are evaluated once per
`SCHEDULER.INTERVAL` (60 s) by one process in the cluster.

## Default rules

Created once at first start (`ALERTS.DEFAULT_RULES`). Edit, disable or delete them freely; they are not
recreated.

| Rule | Condition | Severity |
|---|---|---|
| High error rate | HTTP 5xx rate > 5% over 5 min | critical |
| Slow responses (P95) | request P95 > 2000 ms over 5 min | warning |
| Issue spike | busiest issue > 100 occurrences over 60 min | warning |
| External API failures | worst service failure rate > 10% over 5 min | warning |
| Very slow database query | slowest query > 5000 ms over 5 min | warning |
| Disk almost full | fullest volume > 90% | critical |
| Server memory pressure | average memory > 90% over 10 min | warning |
| Server CPU saturated | average CPU > 90% over 10 min | warning |

## Creating a rule

**Alerts → New rule** (requires `manage_alerts`), or the Django admin, or code:

```python
from django_observatory.models import AlertRule

AlertRule.objects.create(
    name="Payment failures",
    metric="custom", custom_metric="payments.failed",
    operator=">", threshold=10, window_minutes=15,
    severity="critical", channels=["email", "webhook"], cooldown_minutes=30,
)
```

| Field | Meaning |
|---|---|
| `name` | Unique; used as the alert title. |
| `metric` | What to measure (table below). |
| `custom_metric` | Metric name, for `metric="custom"`. |
| `operator` | `>`, `>=`, `<`, `<=` |
| `threshold` | The value to compare with. |
| `window_minutes` | How far back to measure (1–1440). |
| `severity` | `warning` or `critical`. |
| `channels` | Extra channels: `email`, `webhook`. In-app is always on. Empty = every configured channel. |
| `cooldown_minutes` | Minimum time between reminders while it keeps firing (default 30). |
| `enabled` | Evaluate or not. |

### Metrics

| `metric` | Measures | Needs |
|---|---|---|
| `error_rate` | HTTP 5xx as % of requests | at least 10 requests in the window |
| `p95_latency`, `p99_latency` | request latency percentile, ms | at least 10 requests |
| `request_rate` | requests per minute | |
| `exception_count` | exceptions recorded | |
| `issue_occurrences` | occurrences of the busiest issue | |
| `external_failure_rate` | failure % of the worst external service | at least 5 calls to it |
| `external_p95` | P95 latency of the slowest external service, ms | at least 5 calls |
| `slow_query_count` | SQL statements over the slow threshold | |
| `db_max_ms` | slowest SQL statement, ms | |
| `login_failures` | failed logins | |
| `cpu_percent`, `memory_percent` | average server usage, worst host | readings available |
| `disk_percent` | fullest watched volume | |
| `custom` | sum of your counter metric in the window | `custom_metric` |

When there is too little data to judge, a rule neither fires nor resolves.

## Lifecycle

```text
condition true  ─► FIRING ──(acknowledge)──► ACKNOWLEDGED
                     │                            │
                     └──── condition false ───────┴──► RESOLVED
```

- **One alert per rule.** While the condition holds, the same alert is updated (value, last seen,
  evaluation count); no duplicates are created.
- **Notifications** go out when the alert opens, and again only after `cooldown_minutes` if it is still
  firing and nobody acknowledged it.
- **Acknowledging** stops reminders. **Resolving** by hand closes it; it reopens if the condition is still
  true at the next evaluation.
- **Incident grouping.** If an incident is open when an alert fires, the alert is attached to the incident
  and not notified separately: one message about the incident instead of one per symptom.

## Channels

### In-app

Always on: the Alerts page, the sidebar counter.

### Email

```python
OBSERVABILITY = {"ALERTS": {"EMAIL_TO": ["ops@example.com", "oncall@example.com"]}}
```

Sent with Django's `send_mail`, so `EMAIL_BACKEND`, `EMAIL_HOST` and `DEFAULT_FROM_EMAIL` apply.

### Webhook

```python
OBSERVABILITY = {"ALERTS": {"WEBHOOK_URL": "https://hooks.example.com/ops"}}
```

A JSON `POST`:

```json
{"text": "[project-api] ALERT: High error rate\nHTTP 5xx rate (%) is 18.6% (> 5%) over 5 min: 26 of 140 requests failed",
 "type": "alert", "id": 12, "severity": "critical"}
```

For incidents `type` is `"incident"` and `status` is included. The `text` field makes the payload accepted
as-is by Slack-compatible and Microsoft Teams incoming webhooks.

### Adding a channel

```python
# apps.py of one of your apps, in ready()
from django_observatory import alerts

def send_sms(subject, body, payload):
    sms_gateway.send("+92...", f"{subject}: {body}"[:160])

alerts.CHANNELS["sms"] = send_sms
```

Then list `"sms"` in a rule's `channels`. A channel that raises is reported on the internal logger and does
not affect other channels.

## Notes

- Alerts and incident detection run inside the application's worker thread. A process that receives no
  requests at all does not evaluate; any process of the application that is serving traffic does.
- With several processes or servers, evaluation is elected through a database lease, so each rule is
  evaluated once per interval.
- Resolved alerts are removed after `RETENTION.alerts` days.
