-- Backfill Shenzhen Housing Provident Fund account details from the user
-- supplied official mini-program screenshots. Positive rows are provident
-- fund contributions/interest income. Negative rows are withdrawals from the
-- provident fund account; only rows with a matching bank-side receipt are
-- closed as account transfers.

BEGIN;

WITH screenshot_rows AS (
    SELECT *
    FROM (
        VALUES
            (DATE '2025-01-22',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-02-25',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-03-26',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-04-25',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-05-26',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-06-25',  1900.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-07-01',   454.43::numeric, '年终结息', '公积金年终结息','operating_income',  '福利资产', '公积金结息',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-07-28',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-08-26',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-09-28',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-10-27',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-11-25',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2025-12-26',  2150.00::numeric, '汇缴',     '公积金汇缴',   'operating_income',  '福利资产', '公积金汇缴',   NULL::varchar, TRUE,  TRUE,  FALSE, FALSE),
            (DATE '2026-06-18', -2924.00::numeric, '租房提取', '公积金租房提取','internal_transfer', '内部划转', '提取待匹配', NULL::varchar, FALSE, FALSE, FALSE, TRUE)
    ) AS t(event_date, amount_cny, summary, description, cashflow_type, category, subcategory, counter_account_code, affects_spending, affects_nav, affects_investment_capital, is_internal_transfer)
),
prepared AS (
    SELECT
        event_date,
        'hpf'::varchar AS account_code,
        counter_account_code,
        amount_cny,
        'CNY'::varchar AS currency,
        1::numeric AS fx_rate_to_cny,
        amount_cny AS amount,
        cashflow_type,
        category,
        subcategory,
        '深圳住房公积金管理中心'::text AS counterparty,
        summary,
        description,
        affects_spending,
        affects_nav,
        affects_investment_capital,
        is_internal_transfer,
        CASE
            WHEN amount_cny < 0 THEN 'hpf_withdrawal_unmatched:' || to_char(event_date, 'YYYYMMDD') || ':' || ABS(amount_cny)::text
            ELSE NULL
        END AS transfer_group_id,
        'user_screenshot_hpf_account_detail'::varchar AS source_table,
        'hpf_screenshot_' || to_char(event_date, 'YYYYMMDD') || '_' ||
            CASE
                WHEN summary = '年终结息' THEN 'annual_interest'
                WHEN amount_cny < 0 THEN 'rent_withdrawal'
                ELSE 'contribution'
            END AS source_id,
        'hpf_screenshot_' || to_char(event_date, 'YYYYMMDD') || '_' || amount_cny::text AS source_uid,
        md5('migration_062:hpf:' || to_char(event_date, 'YYYY-MM-DD') || ':' || summary || ':' || amount_cny::text) AS source_hash,
        jsonb_build_object(
            'migration', '062_hpf_account_detail_backfill',
            'source', 'official_hpf_mini_program_screenshot',
            'matching_note', CASE
                WHEN amount_cny < 0 THEN 'No CCB/WeChat receipt with matching date and amount was found at backfill time; keep as withdrawal unmatched.'
                ELSE 'Positive provident fund account income from official account details.'
            END
        ) AS raw_json
    FROM screenshot_rows
)
INSERT INTO pams_cashflow_fact (
    event_date,
    posting_date,
    account_code,
    counter_account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    transfer_group_id,
    source_table,
    source_id,
    source_uid,
    source_hash,
    raw_json,
    classification_source,
    confidence,
    review_status,
    source_channel,
    auto_classified
)
SELECT
    event_date,
    event_date,
    account_code,
    counter_account_code,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    cashflow_type,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    affects_spending,
    affects_nav,
    affects_investment_capital,
    is_internal_transfer,
    transfer_group_id,
    source_table,
    source_id,
    source_uid,
    source_hash,
    raw_json,
    'user_confirmed',
    1,
    CASE WHEN amount_cny < 0 THEN 'needs_review' ELSE 'confirmed' END,
    'official_hpf_screenshot',
    FALSE
FROM prepared
ON CONFLICT (source_table, source_id) DO UPDATE
SET event_date = EXCLUDED.event_date,
    posting_date = EXCLUDED.posting_date,
    account_code = EXCLUDED.account_code,
    counter_account_code = EXCLUDED.counter_account_code,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    amount_cny = EXCLUDED.amount_cny,
    cashflow_type = EXCLUDED.cashflow_type,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    counterparty = EXCLUDED.counterparty,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    affects_spending = EXCLUDED.affects_spending,
    affects_nav = EXCLUDED.affects_nav,
    affects_investment_capital = EXCLUDED.affects_investment_capital,
    is_internal_transfer = EXCLUDED.is_internal_transfer,
    transfer_group_id = EXCLUDED.transfer_group_id,
    source_uid = EXCLUDED.source_uid,
    source_hash = EXCLUDED.source_hash,
    raw_json = pams_cashflow_fact.raw_json || EXCLUDED.raw_json,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    source_channel = EXCLUDED.source_channel,
    auto_classified = EXCLUDED.auto_classified,
    updated_at = NOW();

UPDATE pams_cashflow_fact
SET raw_json = raw_json || jsonb_build_object(
        'balance', '15016.63',
        'balance_source', 'official_hpf_mini_program_screenshot',
        'balance_note', 'Official provident fund balance after the 2026-06-18 rent withdrawal.'
    ),
    updated_at = NOW()
WHERE source_table = 'user_screenshot_hpf_account_detail'
  AND source_id = 'hpf_screenshot_20260618_rent_withdrawal';

UPDATE pams_cashflow_fact
SET classification_source = 'user_confirmed',
    confidence = 1,
    review_status = 'confirmed',
    raw_json = raw_json || jsonb_build_object(
        'migration', '062_hpf_account_detail_backfill',
        'source', 'official_hpf_mini_program_screenshot',
        'screenshot_matched', TRUE
    ),
    updated_at = NOW()
WHERE account_code = 'hpf'
  AND (
      (event_date, amount_cny) IN (
          (DATE '2026-01-26', 2150.00::numeric),
          (DATE '2026-02-26', 2150.00::numeric),
          (DATE '2026-03-26', 2150.00::numeric),
          (DATE '2026-04-22', -46655.00::numeric),
          (DATE '2026-04-27', 2150.00::numeric),
          (DATE '2026-05-23', -2924.00::numeric),
          (DATE '2026-05-26', 3655.00::numeric)
      )
  );

WITH balances AS (
    SELECT *
    FROM (
        VALUES
            (DATE '2025-01-01', 30510.20::numeric),
            (DATE '2025-01-22', 32410.20::numeric),
            (DATE '2025-02-25', 34310.20::numeric),
            (DATE '2025-03-26', 36210.20::numeric),
            (DATE '2025-04-25', 38110.20::numeric),
            (DATE '2025-05-26', 40010.20::numeric),
            (DATE '2025-06-25', 41910.20::numeric),
            (DATE '2025-07-01', 42364.63::numeric),
            (DATE '2025-07-28', 44514.63::numeric),
            (DATE '2025-08-26', 46664.63::numeric),
            (DATE '2025-09-28', 48814.63::numeric),
            (DATE '2025-10-27', 50964.63::numeric),
            (DATE '2025-11-25', 53114.63::numeric),
            (DATE '2025-12-26', 55264.63::numeric),
            (DATE '2026-01-01', 55264.63::numeric),
            (DATE '2026-01-26', 57414.63::numeric),
            (DATE '2026-02-26', 59564.63::numeric),
            (DATE '2026-03-26', 61714.63::numeric),
            (DATE '2026-04-22', 15059.63::numeric),
            (DATE '2026-04-27', 17209.63::numeric),
            (DATE '2026-05-23', 14285.63::numeric),
            (DATE '2026-05-26', 17940.63::numeric),
            (DATE '2026-06-18', 15016.63::numeric)
    ) AS t(as_of, balance)
)
INSERT INTO pams_holdings_snapshot (
    symbol,
    name,
    asset_liability,
    currency,
    account_code,
    major_category,
    minor_category,
    market_value,
    cost,
    as_of,
    quantity,
    buy_price,
    current_price,
    asset_id,
    created_at,
    updated_at
)
SELECT
    'HPF_SHENZHEN',
    '深圳公积金账户',
    '资产',
    'CNY',
    'hpf',
    '公积金',
    '住房公积金',
    balance,
    balance,
    as_of,
    balance,
    1,
    1,
    'ASSET_NAME_7BC494786287AD4A',
    NOW(),
    NOW()
FROM balances
ON CONFLICT (account_code, name, as_of) DO UPDATE
SET symbol = EXCLUDED.symbol,
    asset_liability = EXCLUDED.asset_liability,
    currency = EXCLUDED.currency,
    major_category = EXCLUDED.major_category,
    minor_category = EXCLUDED.minor_category,
    market_value = EXCLUDED.market_value,
    cost = EXCLUDED.cost,
    quantity = EXCLUDED.quantity,
    buy_price = EXCLUDED.buy_price,
    current_price = EXCLUDED.current_price,
    asset_id = EXCLUDED.asset_id,
    updated_at = NOW();

COMMIT;
