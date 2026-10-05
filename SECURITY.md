# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**:
<https://github.com/AzeemQidwai/django-observatory/security/advisories/new>

Do not open a public issue. Include the affected version, a description and steps to reproduce. You will
receive an acknowledgement within five working days, and a fix or mitigation plan as soon as the report is
confirmed. You will be credited in the release notes unless you prefer otherwise.

Of particular interest:

- telemetry storing an unredacted secret under the default configuration
- access to observability data without the required permission
- modification of audit records that `AuditEvent.verify_chain()` does not detect
- any input that makes observability fail, block or noticeably slow the host application
- cross-site scripting through stored telemetry, or injection through the query language

## Supported versions

Security fixes are released for the latest minor version.

## Security model

See [docs/security.md](docs/security.md) and [docs/privacy.md](docs/privacy.md).
