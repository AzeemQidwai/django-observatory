# Query language

Every list page, the search page, the JSON API and `observability_export` accept the same filter language.

```text
level:error
status:>=500 AND duration:>1000
(status:500 OR status:502) NOT path:/health*
type:*Timeout* "connection refused"
```

## Syntax

| Form | Meaning |
|---|---|
| `field:value` or `field=value` | equals (text is case-insensitive) |
| `field!=value` | not equal |
| `field:>10`, `field>=10`, `field:<10`, `field<=10` | comparisons, for numbers, dates and levels |
| `field:"two words"` | quote values containing spaces |
| `path:/api/*`, `type:*Timeout*`, `logger:*.views` | `*` wildcard at the start, end or middle of exact-text fields |
| `word` or `"a phrase"` | free text: contained in any of the resource's text columns |
| `A AND B`, `A B` | both (AND is implied between terms) |
| `A OR B` | either |
| `NOT A` | negation |
| `( ... )` | grouping |

`AND`, `OR` and `NOT` are upper-case. `AND` binds tighter than `OR`.

**Levels** are ordered, so `level:>=warning` matches WARNING, ERROR and CRITICAL. Names: `debug`, `info`,
`warning`, `error`, `critical`.

**Dates** accept `2026-10-05` or `2026-10-05T10:30`, for example `timestamp:>=2026-10-05T10:00`. The time
range selector applies as well.

**Booleans** accept `true` / `false`.

A term such as `http://example.com/x` that looks like `field:value` but names no field is treated as free
text. An unknown field is an error, shown with the list of available fields.

## Safety

The query is tokenised and parsed into a tree over the whitelist of fields below, then expressed through
Django ORM lookups with bound parameters. It is never turned into SQL text, cannot reach other tables or
relations (`user__password:x` is rejected), and is limited to 40 terms.

Free text is a case-insensitive substring match, which works on every database without extra
infrastructure. On very large log tables narrow the time range or add a field filter first.

## Fields by resource

### Logs (`logs`)

Free text searches: `message`, `logger_name`.

| Field | Type |
|---|---|
| `category` | text (exact, `*` wildcards) |
| `environment` | text (exact, `*` wildcards) |
| `fingerprint` | text (exact, `*` wildcards) |
| `function` | text (exact, `*` wildcards) |
| `host` | text (exact, `*` wildcards) |
| `ip` | text (exact, `*` wildcards) |
| `level` | log level |
| `logger` | text (exact, `*` wildcards) |
| `message` | text (contains) |
| `module` | text (exact, `*` wildcards) |
| `release` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `type` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |

Examples: `level:error` · `level:>=warning` · `category:security` · `NOT logger:django.*`

### Requests (`requests`)

Free text searches: `path`, `route`, `view_name`.

| Field | Type |
|---|---|
| `db_count` | integer |
| `db_ms` | number |
| `duration` | number |
| `environment` | text (exact, `*` wildcards) |
| `ext_ms` | number |
| `ip` | text (exact, `*` wildcards) |
| `method` | text (exact, `*` wildcards) |
| `path` | text (exact, `*` wildcards) |
| `release` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `route` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `status` | integer |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `user` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |
| `view` | text (exact, `*` wildcards) |

Examples: `status:>=500` · `duration:>1000` · `status:>=400 AND status:<500` · `method:POST`

### Traces (`traces`)

Free text searches: `name`.

| Field | Type |
|---|---|
| `duration` | number |
| `environment` | text (exact, `*` wildcards) |
| `error` | true / false |
| `kind` | text (exact, `*` wildcards) |
| `name` | text (exact, `*` wildcards) |
| `release` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `spans` | integer |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |

Examples: `error:true` · `duration:>1000` · `kind:task`

### Exceptions (`exceptions`)

Free text searches: `message`, `exc_type`, `endpoint`.

| Field | Type |
|---|---|
| `endpoint` | text (exact, `*` wildcards) |
| `environment` | text (exact, `*` wildcards) |
| `fingerprint` | text (exact, `*` wildcards) |
| `function` | text (exact, `*` wildcards) |
| `handled` | true / false |
| `issue` | integer |
| `message` | text (contains) |
| `release` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `type` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |

Examples: `handled:false` · `type:*Timeout*`

### Issues (`issues`)

Free text searches: `title`, `culprit`.

| Field | Type |
|---|---|
| `assignee` | text (exact, `*` wildcards) |
| `events` | integer |
| `first_seen` | date / time |
| `last_seen` | date / time |
| `release` | text (exact, `*` wildcards) |
| `severity` | text (exact, `*` wildcards) |
| `status` | text (exact, `*` wildcards) |
| `title` | text (contains) |
| `type` | text (exact, `*` wildcards) |

Examples: `status:open OR status:regressed` · `status:resolved` · `events:>100`

### Security Events (`security`)

Free text searches: `resource`, `reason`, `username`.

| Field | Type |
|---|---|
| `environment` | text (exact, `*` wildcards) |
| `event` | text (exact, `*` wildcards) |
| `ip` | text (exact, `*` wildcards) |
| `reason` | text (contains) |
| `release` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `resource` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `severity` | text (exact, `*` wildcards) |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `user` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |

Examples: `event:LOGIN_FAILURE` · `event:PERMISSION_DENIED` · `event:SUPERUSER_LOGIN`

### Audit Trail (`audit`)

Free text searches: `object_type`, `object_id`, `username`, `reason`.

| Field | Type |
|---|---|
| `action` | text (exact, `*` wildcards) |
| `ip` | text (exact, `*` wildcards) |
| `object` | text (exact, `*` wildcards) |
| `object_id` | text (exact, `*` wildcards) |
| `reason` | text (contains) |
| `request_id` | text (exact, `*` wildcards) |
| `result` | text (exact, `*` wildcards) |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |
| `user` | text (exact, `*` wildcards) |
| `user_id` | text (exact, `*` wildcards) |

Examples: `action:DELETE` · `result:FAILURE`

### Captured SQL (`queries`)

Free text searches: `normalized_sql`.

| Field | Type |
|---|---|
| `alias` | text (exact, `*` wildcards) |
| `duration` | number |
| `fingerprint` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `route` | text (exact, `*` wildcards) |
| `slow` | true / false |
| `sql` | text (contains) |
| `success` | true / false |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |

Examples: `slow:true` · `success:false` · `duration:>100`

### External Calls (`external_calls`)

Free text searches: `service`, `host`, `path`.

| Field | Type |
|---|---|
| `duration` | number |
| `error` | true / false |
| `host` | text (exact, `*` wildcards) |
| `method` | text (exact, `*` wildcards) |
| `path` | text (exact, `*` wildcards) |
| `request_id` | text (exact, `*` wildcards) |
| `route` | text (exact, `*` wildcards) |
| `service` | text (exact, `*` wildcards) |
| `status` | integer |
| `timeout` | true / false |
| `timestamp` | date / time |
| `trace_id` | text (exact, `*` wildcards) |

Examples: `error:true` · `timeout:true` · `duration:>1000`

### Incidents (`incidents`)

Free text searches: `title`, `probable_cause`.

| Field | Type |
|---|---|
| `cause` | text (exact, `*` wildcards) |
| `confidence` | text (exact, `*` wildcards) |
| `requests` | integer |
| `severity` | text (exact, `*` wildcards) |
| `status` | text (exact, `*` wildcards) |
| `title` | text (contains) |

Examples: `status:open` · `severity:critical OR severity:high`

### Alert History (`alert_history`)

Free text searches: `title`, `message`.

| Field | Type |
|---|---|
| `severity` | text (exact, `*` wildcards) |
| `status` | text (exact, `*` wildcards) |
| `title` | text (contains) |

Examples: `status:firing`

## Recipes

| Goal | Where | Query |
|---|---|---|
| Server errors on one endpoint | Requests | `route:"/api/materials/" AND status:>=500` |
| Slow but successful requests | Requests | `duration:>2000 AND status:<400` |
| Everything one user did | Requests | `user:ahmed` |
| Requests doing too much SQL | Requests | `db_count:>50` |
| Errors that are not Django noise | Logs | `level:>=error NOT logger:django.*` |
| All records of one request | Search | `request_id:req_…` |
| Unhandled timeouts | Exceptions | `handled:false AND type:*Timeout*` |
| Issues from the latest release | Issues | `release:2026.10.05.1 AND status:open` |
| Failed logins from one address | Security Events | `event:LOGIN_FAILURE AND ip:10.0.0.9` |
| Deletions by a user | Audit | `action:DELETE AND user:ahmed` |
| Slow statements on one table | Captured SQL | `slow:true AND sql:inventory` |
| Timeouts calling a service | External Calls | `service:SAP AND timeout:true` |
