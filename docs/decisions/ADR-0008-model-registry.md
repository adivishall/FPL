# ADR-0008: Model registry and experiment tracking without MLflow

- Status: Accepted
- Date: 2026-10-02

## Decision

"MLflow or equivalent" (§34) is satisfied by a minimal, auditable registry:

- `model_registry` table: `model_name, version, artifact_uri, artifact_sha256, feature_version,
  ruleset_version, training_window, data_snapshot_id, params_json, metrics_json, status
  (candidate|approved|production|rejected|retired), owner, created_at, promoted_at, parent_version`.
- Artifacts are stored content-addressed (`ml/models/<name>/<sha256>.joblib`), so a version can
  never be silently overwritten.
- **Promotion gates** (`config/models/*.yaml`): a candidate becomes `approved` only if it beats
  its baseline on the configured out-of-sample metric and passes calibration thresholds (§71).
- **Rollback**: exactly one `production` version per model; promotion demotes the previous
  version to `approved`, so rollback is a single registry operation (§86.2).
- Experiment runs (training, evaluation, backtests) store seed, config, data snapshot id and code
  version (git SHA) in JSON reports under `ml/reports/` and in the database.

## Consequences

No extra server to operate; lineage is queryable with SQL. Trade-off: no MLflow UI — the Backtest
Lab / Model pages in the product UI render registry contents instead.
