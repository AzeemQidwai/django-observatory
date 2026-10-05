# Security model

## Access

The UI and API require an authenticated, active user.

| Mode | Who can view | Elevated (always explicit) |
|---|---|---|
| `staff` (default) | any `is_staff` user | audit trail, settings, data deletion |
| `permissions` | `view_observability` plus the section permission | same |

Superusers always pass. Permissions (app label `django_observatory`): `view_observability`, `view_logs`,
`view_requests`, `view_traces`, `view_exceptions`, `view_metrics`, `view_security_events`,
`view_audit_events`, `manage_alerts`, `manage_observability_settings`, `export_observability_data`,
`delete_observability_data`.

Anonymous users are redirected to `LOGIN_URL` (the API answers 401); authenticated users without access get
403. Detail pages and API detail endpoints enforce the same permission as their list, so changing an id in
a URL never crosses a permission boundary. Related security/audit records on a request page are shown only
to users who hold those permissions.

## Audit trail integrity

`AuditEvent` rows are append-only: the model refuses `save()` on existing rows, `delete()`, and queryset
`update()` / `delete()`; the admin registration is read-only; the UI offers no edit or delete. Each row
stores `hash = sha256(prev_hash + canonical payload)`. **Audit → Verify integrity** (or
`AuditEvent.verify_chain()`) reports modified rows and broken links. Only retention cleanup removes audit
rows, oldest first. For stronger guarantees grant the database user no `UPDATE`/`DELETE` on the audit table
and export it regularly.

## Web hardening

- **SQL / filter injection** – user queries are tokenised and parsed into an AST over a per-resource field
  whitelist, then expressed as ORM lookups. Sort keys are whitelisted. No SQL is assembled from input, and
  normalised SQL shown in the UI is never executed.
- **XSS** – all values pass through Django auto-escaping; chart and graph data use `json_script`; the
  JavaScript writes text with `textContent`.
- **CSRF** – every state change is a POST behind Django's CSRF middleware; list pages are GET-only.
- **Path traversal** – the asset view serves two whitelisted file names.
- **Exports** – redacted again on output; CSV cells that start with a formula character are neutralised.
- **Recursion** – internal failures go to the non-propagating `django_observatory.internal` logger,
  which the handler ignores.

## Reporting

See [SECURITY.md](../SECURITY.md).
