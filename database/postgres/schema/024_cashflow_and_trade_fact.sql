-- P0 fact tables for cash movements and investment trade actions.
-- Raw import tables remain the audit source; these tables provide stable
-- business facts for later state, analytics, and review layers.

CREATE TABLE IF NOT EXISTS pams_cashflow_fact (
    cashflow_id BIGSERIAL PRIMARY KEY,
    event_date DATE NOT NULL,
    posting_date DATE,
    account_code VARCHAR(80) NOT NULL,
    counter_account_code VARCHAR(80),
    amount NUMERIC(20, 4) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    fx_rate_to_cny NUMERIC(20, 10) NOT NULL DEFAULT 1,
    amount_cny NUMERIC(20, 4) NOT NULL,

    cashflow_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    category VARCHAR(80),
    subcategory VARCHAR(80),
    counterparty TEXT,
    summary TEXT,
    description TEXT,

    affects_spending BOOLEAN NOT NULL DEFAULT FALSE,
    affects_nav BOOLEAN NOT NULL DEFAULT TRUE,
    affects_investment_capital BOOLEAN NOT NULL DEFAULT FALSE,
    is_internal_transfer BOOLEAN NOT NULL DEFAULT FALSE,
    transfer_group_id VARCHAR(180),

    source_table VARCHAR(120) NOT NULL,
    source_id VARCHAR(120) NOT NULL,
    source_uid VARCHAR(180),
    source_hash VARCHAR(64) NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    classification_source VARCHAR(80) NOT NULL DEFAULT 'raw_import',
    confidence NUMERIC(6, 4) NOT NULL DEFAULT 0,
    review_status VARCHAR(40) NOT NULL DEFAULT 'auto',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_pams_cashflow_fact_type CHECK (
        cashflow_type IN (
            'operating_income',
            'operating_expense',
            'internal_transfer',
            'investment_funding',
            'investment_withdrawal',
            'investment_return_cash',
            'debt_repayment',
            'debt_drawdown',
            'fee',
            'tax',
            'balance_adjustment',
            'fx_conversion',
            'unknown'
        )
    ),
    CONSTRAINT uq_pams_cashflow_fact_source UNIQUE (source_table, source_id),
    CONSTRAINT uq_pams_cashflow_fact_hash UNIQUE (source_hash)
);

CREATE INDEX IF NOT EXISTS idx_cashflow_fact_event
    ON pams_cashflow_fact (event_date DESC);

CREATE INDEX IF NOT EXISTS idx_cashflow_fact_account_date
    ON pams_cashflow_fact (account_code, event_date DESC);

CREATE INDEX IF NOT EXISTS idx_cashflow_fact_type
    ON pams_cashflow_fact (cashflow_type);

CREATE INDEX IF NOT EXISTS idx_cashflow_fact_source
    ON pams_cashflow_fact (source_table, source_id);

CREATE TABLE IF NOT EXISTS pams_investment_trade_fact (
    trade_id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    trade_time TIMESTAMP WITHOUT TIME ZONE,
    settle_date DATE,
    account_code VARCHAR(80) NOT NULL,
    broker VARCHAR(80),
    portfolio_code VARCHAR(120),

    asset_id VARCHAR(120) REFERENCES pams_asset_master(asset_id) ON DELETE SET NULL,
    symbol VARCHAR(120),
    name VARCHAR(240),
    market VARCHAR(80),
    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',

    trade_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    side VARCHAR(40),
    quantity NUMERIC(28, 10),
    price NUMERIC(28, 10),
    gross_amount NUMERIC(28, 10),
    fee_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    tax_amount NUMERIC(28, 10) NOT NULL DEFAULT 0,
    net_cash NUMERIC(28, 10),
    fx_rate_to_cny NUMERIC(20, 10) NOT NULL DEFAULT 1,
    net_cash_cny NUMERIC(28, 10),

    affects_position BOOLEAN NOT NULL DEFAULT FALSE,
    affects_cost_basis BOOLEAN NOT NULL DEFAULT FALSE,
    affects_realized_pnl BOOLEAN NOT NULL DEFAULT FALSE,
    affects_cash BOOLEAN NOT NULL DEFAULT TRUE,
    realized_pnl NUMERIC(28, 10),
    realized_pnl_cny NUMERIC(28, 10),

    cashflow_id BIGINT REFERENCES pams_cashflow_fact(cashflow_id) ON DELETE SET NULL,
    source_table VARCHAR(120) NOT NULL,
    source_id VARCHAR(120) NOT NULL,
    source_key VARCHAR(180),
    source_hash VARCHAR(64) NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    review_status VARCHAR(40) NOT NULL DEFAULT 'auto',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_pams_investment_trade_fact_type CHECK (
        trade_type IN (
            'buy',
            'sell',
            'deposit',
            'withdraw',
            'fund_subscription',
            'fund_redemption',
            'dividend',
            'interest',
            'coupon',
            'fee',
            'tax',
            'fx_conversion',
            'corporate_action',
            'split',
            'transfer_in_kind',
            'transfer_out_kind',
            'position_adjustment',
            'unknown'
        )
    ),
    CONSTRAINT uq_pams_investment_trade_fact_source UNIQUE (source_table, source_id),
    CONSTRAINT uq_pams_investment_trade_fact_hash UNIQUE (source_hash)
);

CREATE INDEX IF NOT EXISTS idx_trade_fact_trade_date
    ON pams_investment_trade_fact (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_trade_fact_account_date
    ON pams_investment_trade_fact (account_code, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_trade_fact_asset
    ON pams_investment_trade_fact (asset_id);

CREATE INDEX IF NOT EXISTS idx_trade_fact_symbol
    ON pams_investment_trade_fact (symbol);

CREATE INDEX IF NOT EXISTS idx_trade_fact_source
    ON pams_investment_trade_fact (source_table, source_id);
