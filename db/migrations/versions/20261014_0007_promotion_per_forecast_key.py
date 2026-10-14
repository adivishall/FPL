"""promotion per forecast key

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14 05:30:00+00:00

M1.5 review: a snapshot can be promoted again with another forecast key (a model, feature or
simulation configuration change re-keys the serving forecast of the same snapshot). Keyed by
snapshot alone, the second promotion was dropped, so the forecast record named a forecast the
API no longer served. The primary key becomes (snapshot, forecast key). Downgrading keeps only
each snapshot's first promotion.
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f('pk_forecast_promotions'), 'forecast_promotions', type_='primary')
    op.create_primary_key(
        op.f('pk_forecast_promotions'), 'forecast_promotions', ['snapshot_id', 'forecast_key']
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM forecast_promotions p USING forecast_promotions q
        WHERE p.snapshot_id = q.snapshot_id
          AND (p.promoted_at, p.forecast_key) > (q.promoted_at, q.forecast_key)
        """
    )
    op.drop_constraint(op.f('pk_forecast_promotions'), 'forecast_promotions', type_='primary')
    op.create_primary_key(op.f('pk_forecast_promotions'), 'forecast_promotions', ['snapshot_id'])
