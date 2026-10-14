"""forecast record and season evaluations

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14 02:50:43.709843+00:00

M1.5 (``docs/architecture/season-evaluation.md``): ``forecast_promotions`` (the commitment the
forecast job writes before a forecast can be served), ``forecast_checkpoints`` (the forecast
served at each decision cutoff, frozen) and ``season_evaluations`` (insert-only evaluations of
the live record). ``predictions`` becomes the rows of a checkpoint: keyed by checkpoint instead
of by prediction run (a run id can be recomputed by other code under the same id), and no
longer cascaded away with its run. Nothing wrote ``predictions`` before M1.5, so the new
non-null column is added to an empty table; the upgrade refuses to run otherwise rather than
guess a checkpoint. Downgrading deletes the forecast record (the 0005 schema cannot hold rows
without a run, nor checkpoints).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM predictions")).scalar()
    if rows:
        raise RuntimeError(
            f"predictions holds {rows} rows that belong to no checkpoint; "
            "0006 expects the table empty (nothing wrote it before M1.5)"
        )
    op.create_table('forecast_promotions',
    sa.Column('snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('season', sa.String(length=9), nullable=False),
    sa.Column('gameweek', sa.SmallInteger(), nullable=False),
    sa.Column('forecast_key', sa.String(length=80), nullable=False),
    sa.Column('prediction_run_id', sa.String(length=80), nullable=False),
    sa.Column('snapshot_created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('artifact_sha256', sa.String(length=64), nullable=False),
    sa.Column('code_digest', sa.String(length=32), nullable=True),
    sa.Column('promoted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('snapshot_id', name=op.f('pk_forecast_promotions'))
    )
    op.create_index('ix_forecast_promotions_season_gameweek', 'forecast_promotions', ['season', 'gameweek'], unique=False)
    op.create_table('season_evaluations',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('season', sa.String(length=9), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('previous_id', sa.BigInteger(), nullable=True),
    sa.Column('trigger', sa.String(length=32), nullable=False),
    sa.Column('input_digest', sa.String(length=64), nullable=False),
    sa.Column('evaluator_version', sa.String(length=32), nullable=False),
    sa.Column('metric_config_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('data_snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('snapshot_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('code_digest', sa.String(length=32), nullable=False),
    sa.Column('checkpoint_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('windows_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('exclusions_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('metrics_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('changes_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['previous_id'], ['season_evaluations.id'], name=op.f('fk_season_evaluations_previous_id_season_evaluations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_season_evaluations')),
    sa.UniqueConstraint('season', 'scope', 'seq', name=op.f('uq_season_evaluations_season_scope_seq'))
    )
    op.create_table('forecast_checkpoints',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('season', sa.String(length=9), nullable=False),
    sa.Column('gameweek', sa.SmallInteger(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('checks', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('prediction_run_id', sa.String(length=80), nullable=True),
    sa.Column('forecast_key', sa.String(length=80), nullable=True),
    sa.Column('data_snapshot_id', sa.String(length=80), nullable=True),
    sa.Column('feature_snapshot_id', sa.String(length=80), nullable=True),
    sa.Column('cutoff_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_kickoff_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('snapshot_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('promoted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('artifact_sha256', sa.String(length=64), nullable=True),
    sa.Column('code_digest', sa.String(length=32), nullable=True),
    sa.Column('capture_code_digest', sa.String(length=32), nullable=False),
    sa.Column('build_revision', sa.String(length=64), nullable=False),
    sa.Column('provenance_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('populations_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('n_rows', sa.Integer(), nullable=False),
    sa.Column('rows_sha256', sa.String(length=64), nullable=True),
    sa.Column('context_blob', sa.String(length=64), nullable=True),
    sa.Column('distributions_blob', sa.String(length=64), nullable=True),
    sa.CheckConstraint("kind in ('live')", name=op.f('ck_forecast_checkpoints_kind')),
    sa.CheckConstraint("state in ('prospective','live_unverified','missed')", name=op.f('ck_forecast_checkpoints_state')),
    sa.ForeignKeyConstraint(['context_blob'], ['artifact_blobs.sha256'], name=op.f('fk_forecast_checkpoints_context_blob_artifact_blobs')),
    sa.ForeignKeyConstraint(['distributions_blob'], ['artifact_blobs.sha256'], name=op.f('fk_forecast_checkpoints_distributions_blob_artifact_blobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_forecast_checkpoints')),
    sa.UniqueConstraint('season', 'gameweek', 'kind', name=op.f('uq_forecast_checkpoints_season_gameweek_kind'))
    )
    op.add_column('predictions', sa.Column('checkpoint_id', sa.String(length=80), nullable=False))
    op.alter_column('predictions', 'run_id',
               existing_type=sa.VARCHAR(length=80),
               nullable=True)
    op.drop_constraint(op.f('uq_predictions_run_id_player_code_gw'), 'predictions', type_='unique')
    op.create_index(op.f('ix_predictions_checkpoint_id'), 'predictions', ['checkpoint_id'], unique=False)
    op.create_unique_constraint(op.f('uq_predictions_checkpoint_id_player_code_gw'), 'predictions', ['checkpoint_id', 'player_code', 'gw'])
    op.drop_constraint(op.f('fk_predictions_run_id_prediction_runs'), 'predictions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_predictions_checkpoint_id_forecast_checkpoints'), 'predictions', 'forecast_checkpoints', ['checkpoint_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_predictions_run_id_prediction_runs'), 'predictions', 'prediction_runs', ['run_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    op.execute("DELETE FROM predictions")  # every row belongs to a checkpoint
    op.drop_constraint(op.f('fk_predictions_run_id_prediction_runs'), 'predictions', type_='foreignkey')
    op.drop_constraint(op.f('fk_predictions_checkpoint_id_forecast_checkpoints'), 'predictions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_predictions_run_id_prediction_runs'), 'predictions', 'prediction_runs', ['run_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint(op.f('uq_predictions_checkpoint_id_player_code_gw'), 'predictions', type_='unique')
    op.drop_index(op.f('ix_predictions_checkpoint_id'), table_name='predictions')
    op.create_unique_constraint(op.f('uq_predictions_run_id_player_code_gw'), 'predictions', ['run_id', 'player_code', 'gw'])
    op.alter_column('predictions', 'run_id',
               existing_type=sa.VARCHAR(length=80),
               nullable=False)
    op.drop_column('predictions', 'checkpoint_id')
    op.drop_table('forecast_checkpoints')
    op.drop_table('season_evaluations')
    op.drop_index('ix_forecast_promotions_season_gameweek', table_name='forecast_promotions')
    op.drop_table('forecast_promotions')
