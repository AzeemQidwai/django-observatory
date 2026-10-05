# Contributing

Thank you for helping. Bug reports, documentation fixes and code are all welcome.

## Ground rules

These are what make the project what it is. Changes that break them are not merged.

1. **Zero infrastructure.** Nothing may require Redis, Celery, Docker, a message broker, a search engine,
   a SaaS or a CDN. Django is the only mandatory dependency; prefer the standard library.
2. **Offline.** No runtime network access except what the operator configures (webhook, OTLP endpoint).
   The UI ships all of its assets.
3. **Failure isolation.** A bug or outage in observability must never fail, block or slow the host
   application's request. New capture code is wrapped and gets a test that breaks it on purpose.
4. **Privacy by default.** Everything stored, exported or displayed goes through `redaction`. New capture of
   potentially sensitive data is opt-in.
5. **Portable.** Windows Server and Linux; SQLite, PostgreSQL and SQL Server. No database-specific SQL, no
   Unix-only APIs without a fallback.
6. **Explainable.** Incident rules state the evidence behind every inference. No opaque scoring, no LLM in
   the core.

## Development setup

```bash
git clone https://github.com/AzeemQidwai/django-observatory.git
cd django-observatory
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[all]"
python -m django test tests --settings=tests.settings
```

Run the demo project to see your change in the UI:

```bash
cd example
python manage.py migrate && python manage.py createsuperuser
python manage.py runserver
```

Open `/observability/`. `/sap/degrade/?on=1` makes the simulated dependency slow so that an incident is
detected.

## Making a change

- Open an issue first for anything larger than a small fix, so the approach can be agreed.
- Keep pull requests focused: one change, with its tests and documentation.
- Tests live in `tests/` and use Django's test runner. They run with the synchronous pipeline
  (`PIPELINE.SYNC`), so assertions are deterministic; `tests/test_capture.py::PipelineTests` covers the real
  worker thread.
- Model changes need a migration (`python -m django makemigrations django_observatory --settings=tests.settings`).
- UI changes: no build step, no framework, no external assets. Check light and dark mode and a phone-width
  viewport. Charts follow the conventions already in `static/django_observatory/app.js`.
- Add a line to the *Unreleased* section of `CHANGELOG.md`.

See [docs/development.md](docs/development.md) for the code layout, how to add an incident rule, a storage
backend or a notification channel, and the release process.

## Reporting bugs

Use the bug report template. Include the output of `python manage.py observability_test` and
`observability_health`, and remove secrets and production data from anything you paste.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contributions are licensed under the MIT licence of this project.
