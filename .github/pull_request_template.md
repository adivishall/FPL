## Why

<!-- The problem this change solves, and the milestone or issue it belongs to. -->

## What changed

<!-- The design in a few lines: components touched, behaviour before and after. -->

## Checks run

- [ ] `make ci` (ruff, format, mypy, import-linter, fast tests)
- [ ] web: `npx tsc --noEmit`, `npm run build`
- [ ] browser flows (`apps/web/e2e`) — if a user flow changed
- [ ] golden parity unchanged, or regenerated deliberately (explain below)
- [ ] other:

## Migrations and deployment

<!-- New Alembic revision? Upgrade / downgrade tested? Rollback notes? Configuration changes? -->

## Security and privacy impact

<!-- Authentication, manager ownership, keys, webhooks, logs, exported data. "None" is an answer. -->

## Documentation

<!-- README, docs/API.md, docs/KNOWN_LIMITATIONS.md, CHANGELOG.md updated where needed. -->
