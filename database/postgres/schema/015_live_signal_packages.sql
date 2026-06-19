-- =====================================================
-- PAMS live signal package ingestion
-- =====================================================

CREATE TABLE IF NOT EXISTS pams_live_signal_packages (
    package_id              VARCHAR(80) PRIMARY KEY,
    schema_version          VARCHAR(80) NOT NULL,
    idempotency_key         VARCHAR(160) NOT NULL UNIQUE,
    generated_at            TIMESTAMPTZ NOT NULL,

    strategy_id             VARCHAR(120) NOT NULL,
    strategy_run_id         VARCHAR(160) NOT NULL,
    baseline_id             VARCHAR(160) NOT NULL,
    config_hash             VARCHAR(160) NOT NULL,
    execution_contract_hash VARCHAR(160) NOT NULL,
    publish_mode            VARCHAR(30) NOT NULL,

    signal_date             DATE NOT NULL,
    execution_date          DATE NOT NULL,
    signal_clock            VARCHAR(60) NOT NULL,
    execution_clock         VARCHAR(60) NOT NULL,
    reference_price_basis   VARCHAR(80) NOT NULL,

    market                  VARCHAR(20) NOT NULL,
    asset_scope             VARCHAR(80) NOT NULL,
    asset_class             VARCHAR(40) NOT NULL,
    account_scope           VARCHAR(120) NOT NULL,
    currency                VARCHAR(10) NOT NULL,
    symbol_namespace        VARCHAR(80) NOT NULL,

    market_snapshot_id      VARCHAR(160) NOT NULL,
    holdings_snapshot_id    VARCHAR(160) NOT NULL,
    cash_snapshot_id        VARCHAR(160) NOT NULL,
    reconcile_id            VARCHAR(160) NOT NULL,

    package_status          VARCHAR(30) NOT NULL,
    validation_status       VARCHAR(30) NOT NULL,
    failure_reason_codes    JSONB NOT NULL DEFAULT '[]'::jsonb,
    stale                   BOOLEAN NOT NULL DEFAULT FALSE,
    drifted                 BOOLEAN NOT NULL DEFAULT FALSE,
    target_positions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    risk_flags              JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_payload             JSONB NOT NULL,

    received_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_live_signal_package_schema
        CHECK (schema_version = 'daily_position_adjustment_package@1'),
    CONSTRAINT chk_live_signal_package_status
        CHECK (package_status IN ('ready', 'no_op', 'blocked', 'invalid')),
    CONSTRAINT chk_live_signal_validation_status
        CHECK (validation_status IN ('passed', 'blocked', 'invalid')),
    CONSTRAINT chk_live_signal_publish_mode
        CHECK (publish_mode IN ('live', 'dry_run', 'audit_copy'))
);

CREATE INDEX IF NOT EXISTS idx_live_signal_packages_latest
    ON pams_live_signal_packages(strategy_id, account_scope, signal_date DESC, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_signal_packages_status
    ON pams_live_signal_packages(package_status, validation_status);


CREATE TABLE IF NOT EXISTS pams_live_signal_actions (
    id                    BIGSERIAL PRIMARY KEY,
    package_id            VARCHAR(80) NOT NULL REFERENCES pams_live_signal_packages(package_id) ON DELETE CASCADE,
    action_id             VARCHAR(120) NOT NULL,
    idempotency_key       VARCHAR(220) NOT NULL UNIQUE,

    symbol                VARCHAR(40) NOT NULL,
    market                VARCHAR(20) NOT NULL,
    asset_class           VARCHAR(40) NOT NULL,
    action_type           VARCHAR(30) NOT NULL,
    action_status         VARCHAR(30) NOT NULL,

    current_qty           BIGINT NOT NULL,
    target_qty            BIGINT NOT NULL,
    delta_qty             BIGINT NOT NULL,
    suggested_qty         BIGINT NOT NULL,
    target_weight         NUMERIC(10,8),
    reference_price       NUMERIC(20,8) NOT NULL,
    reference_price_basis VARCHAR(80) NOT NULL,
    estimated_amount      NUMERIC(20,4),

    reason_code           VARCHAR(120) NOT NULL,
    reason_tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason_codes          JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked               BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_reason        VARCHAR(160) NOT NULL DEFAULT '',
    expires_at            TIMESTAMPTZ NOT NULL,
    raw_payload           JSONB NOT NULL,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_live_signal_action_per_package
        UNIQUE (package_id, action_id),
    CONSTRAINT chk_live_signal_action_type
        CHECK (action_type IN ('buy', 'sell', 'hold', 'increase', 'reduce', 'rebalance')),
    CONSTRAINT chk_live_signal_action_status
        CHECK (action_status IN ('proposed', 'no_op', 'blocked'))
);

CREATE INDEX IF NOT EXISTS idx_live_signal_actions_package
    ON pams_live_signal_actions(package_id);

CREATE INDEX IF NOT EXISTS idx_live_signal_actions_symbol
    ON pams_live_signal_actions(symbol, market);


CREATE TABLE IF NOT EXISTS pams_live_signal_transaction_lifecycle (
    id                    BIGSERIAL PRIMARY KEY,
    source_package_id     VARCHAR(80) NOT NULL REFERENCES pams_live_signal_packages(package_id) ON DELETE CASCADE,
    source_action_id      VARCHAR(120) NOT NULL,

    strategy_id           VARCHAR(120) NOT NULL,
    strategy_run_id       VARCHAR(160) NOT NULL,
    signal_date           DATE NOT NULL,
    execution_date        DATE NOT NULL,
    config_hash           VARCHAR(160) NOT NULL,
    account_scope         VARCHAR(120) NOT NULL,

    symbol                VARCHAR(40) NOT NULL,
    market                VARCHAR(20) NOT NULL,
    action_type           VARCHAR(30) NOT NULL,
    transaction_status    VARCHAR(30) NOT NULL,
    requested_qty         BIGINT NOT NULL,
    final_qty             BIGINT,
    fill_qty              BIGINT,
    fill_price            NUMERIC(20,8),

    operator              VARCHAR(120),
    decision_reason       TEXT,
    raw_action_payload    JSONB NOT NULL,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_live_signal_transaction_source
        UNIQUE (source_package_id, source_action_id),
    CONSTRAINT chk_live_signal_transaction_status
        CHECK (transaction_status IN (
            'received', 'validated', 'notified', 'needs_review', 'approved',
            'rejected', 'submitted', 'partially_filled', 'filled',
            'cancelled', 'expired', 'blocked'
        ))
);

CREATE INDEX IF NOT EXISTS idx_live_signal_transaction_latest
    ON pams_live_signal_transaction_lifecycle(strategy_id, account_scope, signal_date DESC, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_signal_transaction_status
    ON pams_live_signal_transaction_lifecycle(transaction_status);
