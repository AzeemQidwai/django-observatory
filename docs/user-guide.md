# User guide

A tour of every page, written for the person on call.

## Conventions used on every page

- **Time range** (top right): last 15 minutes, hour, 6 hours, 24 hours, 7 days, 30 days. List pages also
  offer *All time*.
- **Live**: re-fetches the page every five seconds while the tab is visible. The choice is remembered.
- **Status badges** always combine a shape and a label with the colour, so they read in greyscale and for
  colour-blind users: ▲ red = error/critical, ◆ amber = warning, ● green = healthy.
- **Charts**: hover (or tap) anywhere in the plot for the values at that moment. Stacked bars show each
  part and the total.
- **Light / dark**: follows the operating system; the button in the sidebar overrides it.
- **Times** are shown in your browser's time zone; hover a time for the exact UTC value.
- Counts in the sidebar: open issues, firing alerts, open incidents.

Numbers on dashboards come from metrics that count **every** request, query and call. Detailed rows
(individual requests, traces, SQL) may be sampled; see [sampling](configuration.md#sampling).

## Dashboard

*What is happening right now?*

| Element | Meaning |
|---|---|
| Incident banner | Any open incident, with probable cause and impact. Click to investigate. |
| Requests / Error rate | Totals for the range, with the change against the previous period of equal length. |
| Avg latency, P95, P99 | Response-time percentiles. P95 = 95% of requests were faster than this. |
| Slow queries | SQL statements over `DATABASE.SLOW_QUERY_MS`. |
| External API failures | Outbound calls that raised or returned 5xx. |
| Active issues / Open incidents | Things waiting for a human. |
| Server CPU / memory / Disk used | Worst host over the last five minutes; flagged HIGH above its threshold. |
| Security / Audit events | Shown only if you may view them. |

Charts: requests by status class, error rate, latency P50/P95, exceptions, database latency, external API
latency, log volume by level, response-time distribution. Below: slowest endpoints (P95), busiest endpoints
and exceptions by type as ranked bars that link to the matching requests, then the active issues.

## Explore

### Logs

Every record sent through Python `logging`, plus `observe.*` events. The chart shows volume over time by
level and follows the filter. Columns: time, level, logger, message, category, request.

Open a record for its file, line and function, the user, host and process, the metadata passed in
`extra=`, and links to the request, trace and exception it belongs to. *Events with the same fingerprint*
finds every occurrence of the same log statement regardless of the values interpolated into it.

### Requests

One row per captured HTTP request. The chart stacks 2xx / 3xx / 4xx / 5xx. Click a column header to sort
(for example by duration).

**Request detail** is the main investigation page:

| Section | What it tells you |
|---|---|
| Tiles | Status, duration, number of SQL queries and their time, external calls and their time, user and IP. |
| Why is this slow? / Why did this fail? | Shown for slow or failed requests. *Inference* (suspected cause, confidence) and *Observed evidence* are separate. |
| Latency breakdown | Time split between each external service, the database, and application code. |
| Timeline | The trace waterfall: view, templates, each SQL statement, each outbound call, your own spans. Errors are marked ✕. |
| Exceptions | Raised during this request, with a link to the issue. |
| External calls | Service, URL path, status, duration, outcome (OK / ERROR / TIMEOUT). |
| SQL | Statements as sent to the driver (placeholders, no values). *all* lists every execution of that statement. |
| Logs | Everything logged during the request, in order. |
| Security & audit | Events recorded during the request, if you may see them. |
| Relationships | The observability graph for this request. |
| Overview / headers | Route, view, trace id, response size, user agent; request headers with secrets redacted. |

### Traces

Requests, background tasks (`@task`) and manual root spans. The detail page shows the waterfall, names the
dominant latency contributor when one span accounts for 40% or more, and lists span attributes.

### Search

One box across issues, exceptions, requests, logs, traces, security events, external calls and incidents.
Free text or field filters; resources that do not have a field you used are skipped. Paste a request id or
a trace id to find everything related to it.

## Errors

### Exceptions

Individual occurrences. The detail page shows the message, location, endpoint, whether it was handled
(logged by your code) or unhandled, the **breadcrumbs** (what happened just before: the request, SQL, calls,
log lines), and the stack trace with application frames emphasised over library frames. **Copy traceback**
copies the full raw traceback, including chained exceptions.

### Issues

Exceptions grouped by a stable fingerprint: exception type, message with volatile values removed, the
application frame that raised it, and the endpoint. Line numbers and ids do not split a group.

Issue detail: status, occurrences, affected users and endpoints, first and last seen with the release, an
occurrences chart, the latest stack, and recent occurrences.

**Triage** (needs `manage_alerts`): set the status, an assignee and notes.

| Status | Meaning |
|---|---|
| OPEN | New or unhandled. |
| ACKNOWLEDGED | Someone is looking at it. |
| RESOLVED | Fixed. If it occurs again it automatically becomes REGRESSED. |
| IGNORED | Known and accepted; it stays out of the active counts. |
| REGRESSED | Came back after being resolved. |

## Performance

### Endpoints

Latency percentiles over time, the response-time distribution, and four rankings: slowest (P95), where
server time goes (requests × average duration — the best place to optimise), busiest, and highest error
rate. The table lists every route with requests, error rate, average, P50, P95, P99, max, and the average
SQL and external time of captured requests. Sort by any column.

### Database

Query latency and volume over time, the duration distribution, the heaviest statements by total time and
the most executed ones. The table groups by normalised statement (`WHERE id = ?`) with calls, average,
P95, max, total, and slow or failed counts. Click a statement to list its executions; each links to the
request that issued it. *Many calls with a small average* usually means an N+1 pattern.

Totals count every query. The statement table is built from captured queries.

### External APIs

Per service: calls, average, P95, max and failure rate, with latency and failure rankings. Select a service
for its latency over time, calls by outcome and duration distribution. *All calls* lists individual calls
with the request that made them. Map hostnames to names with `EXTERNAL_HTTP.SERVICES`.

## Security

### Security events

`LOGIN_SUCCESS`, `LOGIN_FAILURE`, `LOGOUT`, `SUPERUSER_LOGIN`, `PASSWORD_CHANGE`, `PERMISSION_DENIED`,
`CSRF_FAILURE`, plus anything recorded with `security.record(...)`. Each has the user, IP, resource and
reason. Passwords and tokens are never recorded.

### Audit

Who changed what: user, action, object, before and after, result, IP, request. Requires the
`view_audit_events` permission even for staff. Records cannot be edited or deleted from the UI or the
admin. **Verify integrity** recomputes the hash chain and reports any record that was modified or removed.

## Monitoring

### Metrics

Every built-in and application metric. Select one for its chart over time, its distribution (histograms),
and a breakdown by label values.

### Server

CPU, memory, load and each disk volume, with history. See [server monitoring](server-monitoring.md).

### Alerts

**Active** alerts with acknowledge and resolve buttons; **Rules** with enable/disable and delete; a form to
create a rule; **History**. See [alerts](alerts.md).

### Incidents

A correlated problem with one probable cause. The detail page is arranged as an investigation:

1. **Impact** — requests, users, 5xx responses, endpoints.
2. **Probable cause** — the inference, its confidence and score, the probable start, a recommended
   investigation, and the evidence supporting it. Other hypotheses that were considered are listed.
3. **Correlated signals** — observed facts with baseline → current values, each marked *cause* or *effect*.
4. **Charts** — request latency and the leading signal around the incident.
5. **Relationships** — incident → signals → example requests → traces → SQL / external calls.
6. **Affected endpoints, exceptions, requests** — worst first; open one to see its trace.
7. **Alerts** grouped under the incident, and a **Response** form for status and notes.

Incidents resolve themselves after `INCIDENTS.RESOLVE_AFTER_MINUTES` without symptoms.
See [incident correlation](incident-correlation.md).

### Health

Whether observability itself is working: database, migrations, storage, event queue, worker, event
ingestion, configuration, redaction, instrumentation, overhead (measured per-request cost) and server
resources. The pipeline counters (enqueued, written, dropped, failed, retries) are those of the process that
served the page.

## Administration

### Settings & System

Requires `manage_observability_settings`.

- **Runtime overrides** — change request and log sampling and the slow thresholds without a deployment.
- **System** — versions, platform, storage database, WSGI or ASGI.
- **Storage & retention** — rows and oldest record per kind; *Run retention cleanup now* (needs
  `delete_observability_data`).
- **Effective configuration** — the merged settings, with secrets redacted.

## Investigation recipes

**"The site is slow."** Dashboard → latency chart → *Slowest endpoints* → open a slow request → latency
breakdown. Database dominant: check the SQL list for one slow statement or many repeats. An external
service dominant: External APIs → that service's latency over time.

**"Users report errors."** Dashboard → *Active issues* or the incident banner → issue → latest occurrence →
breadcrumbs and stack. *Affected endpoints* and *releases* show scope and whether a deployment introduced it.

**"What did this user do?"** Requests → `user:ahmed` → open requests. Audit → `user:ahmed` for changes.

**"A customer gave me a request id."** Search → paste it. The id is in the `X-Request-ID` response header
of every response.

**"Is it us or the dependency?"** Incident page, or External APIs: failure rate and latency per service
alongside your own error rate.

**"Did the release break it?"** Issue detail → releases; Requests → `release:2026.10.05.1 AND status:>=500`.

**"Is someone attacking the login?"** Security events → `event:LOGIN_FAILURE`, sort by IP; a spike opens a
security incident naming the source address.

**"Is the server out of resources?"** Server page; the dashboard tiles turn HIGH; a full disk opens an
incident on its own.
