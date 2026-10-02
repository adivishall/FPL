# CI/CD

GitHub requires workflow files under `.github/workflows/`; this directory documents them.

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | every push / PR | `lint` (ruff, format, mypy, import-linter layering), `test` (PostgreSQL + Redis service containers; migration validation; unit, property, integration, data-quality tests with coverage), `optimizer-suite`, `backtest-smoke`, `security` (pip-audit, gitleaks), `containers` (image builds) |
| `deploy.yml` | push to `main` after `ci` succeeds | builds and publishes images to GHCR; deployment only from a green `main` (§78) |

Local equivalents are in the `Makefile` (`make lint`, `make test`, `make ci`).
