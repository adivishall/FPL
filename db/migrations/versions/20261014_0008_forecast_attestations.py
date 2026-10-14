"""forecast attestations

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 08:00:00+00:00

M1.5 time-attestation review: promotion and export times come from the host's clocks (the
Colima VM and the database), which can lag after the laptop sleeps, and FPL news stamps only
detect leakage — neither shows that a forecast existed before a deadline. ``forecast_attestations``
records FPL's own server time (the HTTP ``Date`` of a request to the official API) around each
forecast's computation and, later, after re-hashing the stored file. A checkpoint is
``prospective`` only when such a time precedes the cutoff; otherwise, with every other check
holding, it is the new state ``prospective_unverified``. ``attested_at`` keeps that server time
on the checkpoint. Downgrading drops the attestations and records ``prospective_unverified``
checkpoints as ``live_unverified`` (a check did not hold), the only state the older schema has.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = "'prospective','prospective_unverified','live_unverified','missed'"
OLD_STATES = "'prospective','live_unverified','missed'"


def upgrade() -> None:
    op.create_table('forecast_attestations',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('forecast_key', sa.String(length=80), nullable=False),
    sa.Column('artifact_sha256', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
    sa.Column('not_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind in ('generation','existence')", name=op.f('ck_forecast_attestations_kind')),
    sa.CheckConstraint('not_before is null or not_before <= not_after', name=op.f('ck_forecast_attestations_bounds')),
    sa.ForeignKeyConstraint(['snapshot_id', 'forecast_key'], ['forecast_promotions.snapshot_id', 'forecast_promotions.forecast_key'], name=op.f('fk_forecast_attestations_snapshot_id_forecast_promotions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_forecast_attestations'))
    )
    op.create_index('ix_forecast_attestations_promotion', 'forecast_attestations', ['snapshot_id', 'forecast_key'], unique=False)
    op.add_column('forecast_checkpoints', sa.Column('attested_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f('ck_forecast_checkpoints_state'), 'forecast_checkpoints', type_='check')
    op.create_check_constraint(op.f('ck_forecast_checkpoints_state'), 'forecast_checkpoints', f"state in ({STATES})")


def downgrade() -> None:
    op.drop_constraint(op.f('ck_forecast_checkpoints_state'), 'forecast_checkpoints', type_='check')
    op.execute(
        "UPDATE forecast_checkpoints SET state = 'live_unverified' "
        "WHERE state = 'prospective_unverified'"
    )
    op.create_check_constraint(op.f('ck_forecast_checkpoints_state'), 'forecast_checkpoints', f"state in ({OLD_STATES})")
    op.drop_column('forecast_checkpoints', 'attested_at')
    op.drop_index('ix_forecast_attestations_promotion', table_name='forecast_attestations')
    op.drop_table('forecast_attestations')
