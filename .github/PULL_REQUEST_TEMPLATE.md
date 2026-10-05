## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `python -m django test tests --settings=tests.settings` passes
- [ ] No new mandatory dependency, no network access at runtime, no CDN assets
- [ ] Anything stored, exported or shown passes through `redaction`
- [ ] A failure in the new code cannot fail the host application's request
- [ ] Model changes include a migration; `CHANGELOG.md` and docs updated
