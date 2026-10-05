# Privacy

## Never stored by default

Raw request bodies, raw response bodies, cookies, `Authorization` headers, uploaded files, password fields,
authentication tokens, query-string parameters, SQL parameter values, exception local variables, session
keys (only a one-way hash is kept for grouping).

## Opt-in settings

| Setting | Effect | Still applied |
|---|---|---|
| `REQUESTS.CAPTURE_QUERY_PARAMS` | store query parameters | key-based redaction |
| `REQUESTS.CAPTURE_BODY` | store form fields / first `MAX_BODY_BYTES` of other bodies; multipart never | key + pattern redaction |
| `DATABASE.CAPTURE_PARAMS` | store SQL parameter values | pattern redaction |

`manage.py check` warns when these are on with `DEBUG=False`.

## Redaction engine

One engine (`django_observatory.redaction`) handles log messages and metadata, exception messages and
stack text, request metadata and headers, external-call paths, audit and security metadata, breadcrumbs,
exports and API responses.

- **By key** (case-insensitive substring; `-` and `_` equivalent): password, passwd, secret, token,
  access_token, refresh_token, api_key, apikey, authorization, cookie, sessionid, csrf, client_secret,
  private_key, credential(s), signature, otp, pin_code.
- **By pattern** in free text: `Authorization:` / `Cookie:` lines, `Bearer` / `Basic` credentials,
  `key=value` and `"key": "value"` pairs for sensitive keys, JWTs, PEM private keys, passwords in URLs.

Add your own:

```python
OBSERVABILITY_REDACTION = {"keys": ["national_id", "iban"], "patterns": [r"\b\d{5}-\d{7}-\d\b"]}
```

Values are redacted **before** they are queued, so secrets never reach the database. Exports redact again,
so rules added later also cover historical data on the way out.

## Limits

Pattern redaction is best effort for free text: a secret with no recognisable key or shape (for example a
bare token in a sentence) cannot be detected. Prefer structured logging (`extra={...}` / `observe.*`) where
keys make the intent explicit, and add patterns for identifiers specific to your domain.

Usernames, user ids and client IP addresses are stored because investigation requires them. Use retention
settings to bound how long they are kept.
