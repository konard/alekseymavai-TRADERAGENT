"""OHLCV candles hypertable for TimescaleDB (HistoryManager).

Revision ID: ohlcv_hypertable
Revises: v2_multi_strategy
Create Date: 2026-03-02

Creates a ``candles`` table as a TimescaleDB hypertable, partitioned by time
in 7-day chunks.  The TimescaleDB extension is loaded lazily if not already
present (idempotent via ``CREATE EXTENSION IF NOT EXISTS``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "ohlcv_hypertable"
down_revision: Union[str, None] = "v2_multi_strategy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure TimescaleDB extension is available (no-op if already installed)
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            time        TIMESTAMPTZ  NOT NULL,
            exchange    VARCHAR(20)  NOT NULL,
            symbol      VARCHAR(20)  NOT NULL,
            interval    VARCHAR(5)   NOT NULL,
            open        NUMERIC      NOT NULL,
            high        NUMERIC      NOT NULL,
            low         NUMERIC      NOT NULL,
            close       NUMERIC      NOT NULL,
            volume      NUMERIC      NOT NULL,
            PRIMARY KEY (time, exchange, symbol, interval)
        )
        """
    )

    # Convert to hypertable — chunk_time_interval = 7 days
    # if_not_exists=TRUE makes this idempotent (safe for re-runs)
    op.execute(
        """
        SELECT create_hypertable(
            'candles',
            'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days'
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candles_sym_int_time
            ON candles (symbol, interval, time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candles")
