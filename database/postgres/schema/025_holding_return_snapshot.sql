-- Precomputed holding-return snapshots for investment return charts.
--
-- This table stores the return state that the holdings page already exposes,
-- so chart endpoints can read a compact daily series instead of rebuilding the
-- full holdings/trade/FX/IBKR history on every request.

CREATE TABLE IF NOT EXISTS pams_holding_return_snapshot (
    snapshot_date DATE NOT NULL,
    section VARCHAR(40) NOT NULL DEFAULT 'total',
    base_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',

    market_value NUMERIC(28, 10) NOT NULL DEFAULT 0,
    cost NUMERIC(28, 10) NOT NULL DEFAULT 0,
    position_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    realized_income NUMERIC(28, 10) NOT NULL DEFAULT 0,
    total_pnl NUMERIC(28, 10) NOT NULL DEFAULT 0,
    net_flow NUMERIC(28, 10) NOT NULL DEFAULT 0,
    cumulative_capital NUMERIC(28, 10) NOT NULL DEFAULT 0,

    return_rate NUMERIC(28, 12) NOT NULL DEFAULT 0,
    daily_return_rate NUMERIC(28, 12) NOT NULL DEFAULT 0,
    time_weighted_return_rate NUMERIC(28, 12) NOT NULL DEFAULT 0,
    capital_return_rate NUMERIC(28, 12) NOT NULL DEFAULT 0,
    display_return_rate NUMERIC(28, 12) NOT NULL DEFAULT 0,

    rows_count INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(80) NOT NULL DEFAULT 'holdings_return_snapshot',
    source_hash VARCHAR(64),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (snapshot_date, section, base_currency),
    CONSTRAINT ck_pams_holding_return_snapshot_section CHECK (
        section IN ('total', 'a_stock', 'a_fund', 'us_stock', 'precious_metal')
    )
);

CREATE INDEX IF NOT EXISTS idx_holding_return_snapshot_series
    ON pams_holding_return_snapshot (section, base_currency, snapshot_date);
