-- Backfill Santander January 2026 statement rows from the formal PDF statement.
-- The 2026-01-08 Huang Xingyu MXN receipt replaces the temporary manual
-- correction inserted before the official Santander January record was available.

WITH statement_import AS (
    INSERT INTO pams_bank_statement_imports (
        profile_code,
        bank,
        account_code,
        account_kind,
        source_file,
        statement_start,
        statement_end,
        statement_balance,
        row_count
    )
    SELECT
        'santander_4209',
        'Santander Mexico',
        'santander',
        'debit',
        '/host/account-ledger/Santander/Santander_4209_20260101-20260131.csv',
        DATE '2026-01-01',
        DATE '2026-01-31',
        119924.5200,
        10
    WHERE NOT EXISTS (
        SELECT 1
        FROM pams_bank_statement_imports
        WHERE profile_code = 'santander_4209'
          AND source_file = '/host/account-ledger/Santander/Santander_4209_20260101-20260131.csv'
    )
    RETURNING id
),
selected_import AS (
    SELECT id FROM statement_import
    UNION ALL
    SELECT id
    FROM pams_bank_statement_imports
    WHERE profile_code = 'santander_4209'
      AND source_file = '/host/account-ledger/Santander/Santander_4209_20260101-20260131.csv'
    ORDER BY id DESC
    LIMIT 1
),
statement_rows (
    source_uid,
    transaction_date,
    transaction_time,
    folio,
    amount,
    balance,
    category,
    subcategory,
    summary,
    reference,
    source_hash
) AS (
    VALUES
    ('03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7', DATE '2026-01-08', '15:46:27', '0666381', 40000.0000, 40000.0000, '内部划转', '换汇转账', 'ABONO TRANSFERENCIA SPEI RECIBIDO DE BBVA MEXICO XINGYU HUANG CONCEPTO 40000transfer', '0080126', '1b2fffbd2971024ef9fbe545d6eb3f808d0e46731f4434e4046630e16a91ab4b'),
    ('ae6ee1177c3cac3f5d5fe401d1358299aab1c25b2cb9015891ae53912c586ebe', DATE '2026-01-09', '18:23:43', '4962993', 20000.0000, 60000.0000, '未分类', NULL, 'ABONO TRANSFERENCIA SPEI RECIBIDO DE BBVA MEXICO BO QIU CONCEPTO hzf', '0912250', '16289a4fde0bd3224b6b94186167d80cd897d0d757052af5500337be17f6d697'),
    ('e0e59256e41583a2ee55fd329a03e64ecb51f3e3884a122771f27c7118e61ca1', DATE '2026-01-12', '10:00:00', '0519934', -1000.0000, 59000.0000, '居住', '房租', 'PAGO TRANSFERENCIA SPEI TRANSFERENCIA A RENTA', '3009566', '2dbf5aafd80a53bcfd751f07b34b90d5aff907697c7749aec3e808cd35a19442'),
    ('d28bc8defc0108dd1a984a3f41365ca16159f3d5b09f28a8caa040ec3ac4b3a4', DATE '2026-01-12', '11:00:00', '0535470', -7000.0000, 52000.0000, '未分类', NULL, 'PAGO TRANSF RAPIDA SPEI TRANSFERENCIA A ANA SILVIA', '1438585', '41610b7e67ceeb0eb5993b266fff091816eadca71dcfde8b8cf13af6859c2896'),
    ('7410cb7f4114f3403eaf88badf3661047459938c5da319da2be615660f9567d5', DATE '2026-01-12', '12:19:58', '0539025', -49000.0000, 3000.0000, '居住', '房租', 'PAGO TRANSFERENCIA SPEI ENVIADO A BANORTE JUAN ANTONIO CALDERA TRUJILLO CONCEPTO TRANSFERENCIA A RENTA', '2444542', '1e5604e418ed773a5fc54f6473c901eec237c039025edc67fdf8caeb17c2f7af'),
    ('4d3f2974342dcd9a5e0a5eed795faf010f549f1cda08b5f61a5abc004e534778', DATE '2026-01-16', '00:00:00', '0074838', -1000.0000, 2000.0000, '未分类', NULL, 'DISP ATM PROPIO TARJ DEB X9330 TERMINACION 3268 16ENE26', '0074838', '6cfb5141cbc0f4cd45c0a1289c8dc34a42b698da81f48be1798016a897324e9d'),
    ('5a6bf40a34d2d508efa8fbb528fc09e45eeb37f1c393b4d52fcb259fc228497b', DATE '2026-01-27', '10:00:00', '0021057', -399.0000, 1601.0000, '日常消费', '小额消费/红包', 'CONSUMO INTERNACIONAL MC TERMINACION 3268 26ENE26 APPLE.COM/BILL CUPERTINO', '0021057', 'a6f1e81bc7197c8b881d0100a4e908276d90559cb3a38b8d90d28fde3657596f'),
    ('bd821756697fb71a2465b2f4c850fd2af15d65f6bef13b952e55e4d47b3d4ed3', DATE '2026-01-27', '11:00:00', '0023551', -49.0000, 1552.0000, '日常消费', '小额消费/红包', 'CONSUMO INTERNACIONAL MC TERMINACION 3268 27ENE26 APPLE.COM/BILL CUPERTINO', '0023551', 'cc9761ce72f6e384e4f1beb781b333f1f67e5cefa7994a56bbcf1f2ae9e92ee8'),
    ('1dc51d9c2ba9d8024d026ff159d2ba9cd63cdc7bc9cb37dc70e1c73654a8c1d4', DATE '2026-01-30', '02:16:56', '2953492', 9106.9400, 10658.9400, '收入', '工资', 'ABONO TRANSFERENCIA SPEI RECIBIDO DE CITI MEXICO HUAWEI TECHNOLOGIES DE MEXICO SA DE CV', '80043', 'ea04727c058403ee9c7a2e7e51e3288a5c391e50ef48801c6edb9dd011ce9fc8'),
    ('2b9b6889dc06584210335656e0808829ef97214ae07ce1ff46828ced7133bcdf', DATE '2026-01-30', '05:53:26', '3128945', 109265.5800, 119924.5200, '收入', '工资', 'ABONO TRANSFERENCIA SPEI RECIBIDO DE CITI MEXICO HUAWEI TECHNOLOGIES DE MEXICO SA DE CV', '23424', '2ac8bddfaa621198bb5f78f6b0ec4e29f141d6cc1c5ad93d872b78f83ecbb10e')
),
upsert_rows AS (
    INSERT INTO pams_bank_statement_rows (
        import_id,
        source_uid,
        profile_code,
        bank,
        account_code,
        account_kind,
        transaction_date,
        posting_date,
        amount,
        currency,
        balance,
        original_amount,
        original_currency,
        category,
        subcategory,
        auto_classified,
        summary,
        description,
        counterparty,
        raw_json
    )
    SELECT
        selected_import.id,
        statement_rows.source_uid,
        'santander_4209',
        'Santander Mexico',
        'santander',
        'debit',
        statement_rows.transaction_date,
        statement_rows.transaction_date,
        statement_rows.amount,
        'MXN',
        statement_rows.balance,
        statement_rows.amount,
        'MXN',
        statement_rows.category,
        statement_rows.subcategory,
        true,
        statement_rows.summary,
        statement_rows.summary,
        statement_rows.summary,
        jsonb_build_object(
            'row_idx', row_number() OVER (ORDER BY statement_rows.transaction_date, statement_rows.transaction_time),
            'transaction_time', statement_rows.transaction_time,
            'branch', statement_rows.folio,
            'reference', statement_rows.reference,
            'source_file', '/host/account-ledger/Santander/Santander_4209_20260101-20260131.csv',
            'statement_template', 'santander_mx_account_csv',
            'source_pdf', 'Estado de cuenta enero 2026.pdf'
        )
    FROM statement_rows
    CROSS JOIN selected_import
    ON CONFLICT (source_uid) DO UPDATE SET
        category = EXCLUDED.category,
        subcategory = EXCLUDED.subcategory,
        auto_classified = EXCLUDED.auto_classified,
        summary = EXCLUDED.summary,
        description = EXCLUDED.description,
        counterparty = EXCLUDED.counterparty,
        raw_json = COALESCE(pams_bank_statement_rows.raw_json, '{}'::jsonb) || EXCLUDED.raw_json
    RETURNING
        id,
        source_uid,
        transaction_date,
        posting_date,
        account_code,
        amount,
        currency,
        category,
        subcategory,
        counterparty,
        summary,
        description
),
fx_rows AS (
    SELECT
        r.*,
        sr.source_hash,
        COALESCE(
            CASE
                WHEN sr.source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7'
                THEN 0.3886947500::numeric
            END,
            fx_hist.rate,
            fx_latest.rate,
            1
        ) AS fx_rate_to_cny,
        CASE
            WHEN sr.source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7'
            THEN 15547.7900::numeric
            ELSE r.amount * COALESCE(fx_hist.rate, fx_latest.rate, 1)
        END AS amount_cny
    FROM upsert_rows r
    JOIN statement_rows sr ON sr.source_uid = r.source_uid
    LEFT JOIN LATERAL (
        SELECT rate
        FROM pams_fx_rates_daily fx
        WHERE fx.base_currency = 'CNY'
          AND fx.quote_currency = r.currency
          AND fx.as_of <= r.transaction_date
        ORDER BY fx.as_of DESC
        LIMIT 1
    ) fx_hist ON TRUE
    LEFT JOIN LATERAL (
        SELECT rate
        FROM pams_fx_rates_daily fx
        WHERE fx.base_currency = 'CNY'
          AND fx.quote_currency = r.currency
        ORDER BY fx.as_of DESC
        LIMIT 1
    ) fx_latest ON TRUE
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
    review_status
)
SELECT
    transaction_date,
    posting_date,
    account_code,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN 'ccb_7332'
        ELSE NULL
    END,
    amount,
    currency,
    fx_rate_to_cny,
    amount_cny,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN 'internal_transfer'
        WHEN amount > 0 THEN 'operating_income'
        ELSE 'operating_expense'
    END,
    category,
    subcategory,
    counterparty,
    summary,
    description,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN false
        ELSE true
    END,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN false
        ELSE true
    END,
    false,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN true
        ELSE false
    END,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7'
        THEN 'peer_fx_transfer:ccb_7332_to_santander:20260108_20260109:cny15547_79_mxn40000'
        ELSE NULL
    END,
    'pams_bank_statement_rows',
    id::text,
    source_uid,
    source_hash,
    jsonb_build_object('statement_row_id', id, 'source_pdf', 'Estado de cuenta enero 2026.pdf'),
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN 'manual_transfer_match'
        ELSE 'bank_statement'
    END,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN 0.9800
        ELSE 0.6000
    END,
    CASE
        WHEN source_uid = '03c4d62c1aaac45901d221230a47db5775a12007b6853e3d394810ee5c5c54a7' THEN 'confirmed'
        ELSE 'auto'
    END
FROM fx_rows
ON CONFLICT (source_hash) DO UPDATE SET
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    counter_account_code = EXCLUDED.counter_account_code,
    fx_rate_to_cny = EXCLUDED.fx_rate_to_cny,
    amount_cny = EXCLUDED.amount_cny,
    cashflow_type = EXCLUDED.cashflow_type,
    affects_spending = EXCLUDED.affects_spending,
    affects_nav = EXCLUDED.affects_nav,
    affects_investment_capital = EXCLUDED.affects_investment_capital,
    is_internal_transfer = EXCLUDED.is_internal_transfer,
    transfer_group_id = EXCLUDED.transfer_group_id,
    classification_source = EXCLUDED.classification_source,
    confidence = EXCLUDED.confidence,
    review_status = EXCLUDED.review_status,
    raw_json = COALESCE(pams_cashflow_fact.raw_json, '{}'::jsonb) || EXCLUDED.raw_json,
    updated_at = NOW();

UPDATE pams_cashflow_fact
SET
    counter_account_code = 'santander',
    cashflow_type = 'internal_transfer',
    category = '内部划转',
    subcategory = '换汇转账',
    affects_spending = false,
    affects_nav = false,
    affects_investment_capital = false,
    is_internal_transfer = true,
    transfer_group_id = 'peer_fx_transfer:ccb_7332_to_santander:20260108_20260109:cny15547_79_mxn40000',
    classification_source = 'manual_transfer_match',
    confidence = 0.9800,
    review_status = 'confirmed',
    updated_at = NOW()
WHERE account_code = 'ccb_7332'
  AND event_date = DATE '2026-01-09'
  AND amount_cny = -15547.7900
  AND (counterparty LIKE '%黄星宇%' OR transfer_group_id = 'peer_fx_transfer:ccb_7332_to_santander:20260108_20260109:cny15547_79_mxn40000');

DELETE FROM pams_cashflow_fact
WHERE source_table = 'manual_cashflow_correction'
  AND source_id = 'peer_fx_transfer_20260108_santander_huangxingyu_40000_mxn';
