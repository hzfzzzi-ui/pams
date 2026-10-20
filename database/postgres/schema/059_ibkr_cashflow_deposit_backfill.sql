-- Backfill IBKR deposit trade facts from confirmed Santander -> IBKR cashflow facts.
-- The statement side is pams_cashflow_fact; this mirrors it into
-- pams_investment_trade_fact with cashflow_id for a repeatable funding closure.

WITH src AS (
    SELECT
        cf.cashflow_id,
        cf.event_date,
        cf.amount,
        cf.currency,
        cf.fx_rate_to_cny,
        cf.amount_cny,
        cf.source_hash,
        cf.summary,
        cf.counterparty
    FROM pams_cashflow_fact cf
    WHERE cf.event_date >= DATE '2026-01-01'
      AND cf.counter_account_code = 'ibkr'
      AND cf.amount_cny < 0
      AND cf.cashflow_type IN ('investment_funding', 'internal_transfer')
      AND COALESCE(LOWER(cf.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
)
INSERT INTO pams_investment_trade_fact (
    trade_date,
    settle_date,
    account_code,
    broker,
    currency,
    trade_type,
    side,
    gross_amount,
    fee_amount,
    tax_amount,
    net_cash,
    fx_rate_to_cny,
    net_cash_cny,
    affects_position,
    affects_cost_basis,
    affects_realized_pnl,
    affects_cash,
    cashflow_id,
    source_table,
    source_id,
    source_key,
    source_hash,
    raw_json,
    review_status,
    notes
)
SELECT
    event_date,
    event_date,
    'ibkr',
    'IBKR',
    currency,
    'deposit',
    'deposit',
    ABS(amount),
    0,
    0,
    ABS(amount),
    fx_rate_to_cny,
    ABS(amount_cny),
    FALSE,
    FALSE,
    FALSE,
    TRUE,
    cashflow_id,
    'pams_cashflow_fact',
    cashflow_id::text,
    'ibkr_deposit:' || cashflow_id::text,
    md5('ibkr_deposit:' || cashflow_id::text || ':' || COALESCE(source_hash, '')),
    jsonb_build_object(
        'derived_from', 'pams_cashflow_fact',
        'cashflow_id', cashflow_id,
        'summary', summary,
        'counterparty', counterparty,
        'accounting_note', 'Santander to IBKR funding mirrored as IBKR deposit fact'
    ),
    'reviewed',
    '由现金流 fact 派生补齐 IBKR 入金闭环'
FROM src
ON CONFLICT (source_table, source_id)
DO UPDATE SET
    trade_date = EXCLUDED.trade_date,
    settle_date = EXCLUDED.settle_date,
    account_code = EXCLUDED.account_code,
    broker = EXCLUDED.broker,
    currency = EXCLUDED.currency,
    trade_type = EXCLUDED.trade_type,
    side = EXCLUDED.side,
    gross_amount = EXCLUDED.gross_amount,
    net_cash = EXCLUDED.net_cash,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    net_cash_cny = EXCLUDED.net_cash_cny,
    affects_cash = EXCLUDED.affects_cash,
    cashflow_id = EXCLUDED.cashflow_id,
    source_key = EXCLUDED.source_key,
    source_hash = EXCLUDED.source_hash,
    raw_json = EXCLUDED.raw_json,
    review_status = EXCLUDED.review_status,
    notes = EXCLUDED.notes,
    updated_at = now();
