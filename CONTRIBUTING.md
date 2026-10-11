# Contributing to FPL Copilot

Thanks for your interest. FPL Copilot is developed milestone by milestone; the current state and
the next milestone are in [`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) and
[`docs/PRODUCT_GAP_ANALYSIS.md`](docs/PRODUCT_GAP_ANALYSIS.md).

## Development setup

```bash
uv sync --frozen                     # Python 3.12 workspace and dev tools
make services-up                     # PostgreSQL + Redis (docker compose)
make ci                              # lint, types, layering contracts, fast tests
cd apps/web && npm ci && npm run build
```

The README's *Run it locally* section starts the whole stack.

## Before opening a pull request

* `make ci` passes (ruff, ruff format, mypy, import-linter, fast tests).
* Web changes: `npx tsc --noEmit` and `npm run build` in `apps/web`; browser flows in
  `apps/web/e2e` (`npx playwright test`) when a user flow changes.
* Engine changes (forecasting, simulation, optimiser, decision rules) must keep golden parity
  (`tests/integration/test_golden_parity.py`) or regenerate the golden deliberately with
  `FPL_UPDATE_GOLDEN=1` *and* explain in the pull request why the output changed. Never add a
  numerical tolerance to make a comparison pass.
* Schema changes come with an Alembic migration that upgrades and downgrades
  (`tests/integration/test_migrations.py`) and a note on rollback in `docs/DEPLOYMENT.md`.
* Never present a plan as optimal unless the solver proved it; never present a historical
  result as current.
* Update the documentation the change affects (API contract, limitations, changelog).

## Conventions

* Small, focused commits with descriptive messages; no rewriting of published history.
* Library packages never import applications (enforced by import-linter).
* No secrets, real manager data or personal identifiers in code, fixtures, logs or screenshots.

Security issues: see [`SECURITY.md`](SECURITY.md).
