-- 建行 0453 信用卡近两月官方 CSV 明细补录。
-- 来源：账户台账/建行/downfile.csv；用户确认 2026-05-24 至今未出账单已入账金额 CNY 16,514.29。
-- 口径：CSV 5/1-5/20 已存在于 cashflow fact，本迁移仅补 2026-05-21 之后缺失的 63 条明细，避免重复计入消费。

BEGIN;

WITH imported AS (
    INSERT INTO pams_bank_statement_imports (profile_code, bank, account_code, account_kind, source_file, statement_start, statement_end, statement_balance, row_count)
    SELECT 'ccb_credit_0453_unbilled_20260524_20260621', 'CCB', 'ccb_credit_0453', 'credit_card', '账户台账/建行/downfile.csv', DATE '2026-05-24', DATE '2026-06-21', 16514.2900, 63
    WHERE NOT EXISTS (
        SELECT 1 FROM pams_bank_statement_imports WHERE profile_code = 'ccb_credit_0453_unbilled_20260524_20260621'
    )
    RETURNING id
), import_ref AS (
    SELECT id FROM imported
    UNION ALL
    SELECT id FROM pams_bank_statement_imports WHERE profile_code = 'ccb_credit_0453_unbilled_20260524_20260621'
    ORDER BY id DESC
    LIMIT 1
), raw_rows (id, source_uid, transaction_date, posting_date, amount, currency, balance, original_amount, original_currency, category, subcategory, auto_classified, summary, description, counterparty, raw_json) AS (
    VALUES
        (1148, 'ccb_credit_0453_downfile_20260524_20260621_138', DATE '2026-05-22', DATE '2026-05-25', -40.5300, 'CNY', 40.5300, 40.5300, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXO SANTA FE MEXICO DF MX', 'OXXO SANTA FE MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "40.53", "raw_line_index": 138, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1149, 'ccb_credit_0453_downfile_20260524_20260621_140', DATE '2026-05-22', DATE '2026-05-26', -39.8600, 'CNY', 80.3900, 39.8600, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "39.86", "raw_line_index": 140, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1150, 'ccb_credit_0453_downfile_20260524_20260621_134', DATE '2026-05-23', DATE '2026-05-26', -44.0800, 'CNY', 124.4700, 44.0800, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "44.08", "raw_line_index": 134, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1151, 'ccb_credit_0453_downfile_20260524_20260621_136', DATE '2026-05-23', DATE '2026-05-26', -70.3400, 'CNY', 194.8100, 70.3400, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "70.34", "raw_line_index": 136, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1152, 'ccb_credit_0453_downfile_20260524_20260621_132', DATE '2026-05-24', DATE '2026-05-26', -23.2000, 'CNY', 218.0100, 23.2000, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'STARBUCKS CITY WALK CMX MX', 'STARBUCKS CITY WALK CMX MX', '{"card_no": "4895920958780453", "csv_amount": "23.20", "raw_line_index": 132, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1153, 'ccb_credit_0453_downfile_20260524_20260621_130', DATE '2026-05-25', DATE '2026-05-28', -40.5200, 'CNY', 3799.5300, 40.5200, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "40.52", "raw_line_index": 130, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1154, 'ccb_credit_0453_downfile_20260524_20260621_128', DATE '2026-05-26', DATE '2026-05-29', -29.0600, 'CNY', 3828.5900, 29.0600, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "29.06", "raw_line_index": 128, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1155, 'ccb_credit_0453_downfile_20260524_20260621_110', DATE '2026-05-27', DATE '2026-05-27', -3541.0000, 'CNY', 3759.0100, 3541.0000, 'CNY', '交通出行', '机票火车', TRUE, '信用卡消费', '程支付-上海华程西南国际旅行社有限公司', '程支付-上海华程西南国际旅行社有限公司', '{"card_no": "4895920958780453", "csv_amount": "3541.00", "raw_line_index": 110, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1156, 'ccb_credit_0453_downfile_20260524_20260621_114', DATE '2026-05-27', DATE '2026-05-29', -27.5600, 'CNY', 3856.1500, 27.5600, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "27.56", "raw_line_index": 114, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1157, 'ccb_credit_0453_downfile_20260524_20260621_118', DATE '2026-05-27', DATE '2026-05-29', -38.1600, 'CNY', 3894.3100, 38.1600, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "38.16", "raw_line_index": 118, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1158, 'ccb_credit_0453_downfile_20260524_20260621_120', DATE '2026-05-27', DATE '2026-05-29', -64.9700, 'CNY', 3959.2800, 64.9700, 'CNY', '交通出行', '机票火车', TRUE, '信用卡消费', 'TRAVEL MARKET T2 CIUDAD DE MEXMX', 'TRAVEL MARKET T2 CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "64.97", "raw_line_index": 120, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1159, 'ccb_credit_0453_downfile_20260524_20260621_122', DATE '2026-05-27', DATE '2026-05-29', -90.0300, 'CNY', 4049.3100, 90.0300, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'COM RAP LPQ AICM T2 CIUDAD DE MEXMX', 'COM RAP LPQ AICM T2 CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "90.03", "raw_line_index": 122, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1160, 'ccb_credit_0453_downfile_20260524_20260621_124', DATE '2026-05-27', DATE '2026-05-29', -137.7600, 'CNY', 4187.0700, 137.7600, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'EBSSATAXI CUU CAJA 1 CHIHUAHUA CHIMX', 'EBSSATAXI CUU CAJA 1 CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "137.76", "raw_line_index": 124, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1161, 'ccb_credit_0453_downfile_20260524_20260621_126', DATE '2026-05-27', DATE '2026-05-29', -181.0700, 'CNY', 4368.1400, 181.0700, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "181.07", "raw_line_index": 126, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1162, 'ccb_credit_0453_downfile_20260524_20260621_112', DATE '2026-05-27', DATE '2026-05-30', -207.3300, 'CNY', 4575.4700, 207.3300, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "207.33", "raw_line_index": 112, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1163, 'ccb_credit_0453_downfile_20260524_20260621_116', DATE '2026-05-27', DATE '2026-05-30', -224.7100, 'CNY', 4800.1800, 224.7100, 'CNY', '交通出行', '酒店住宿', TRUE, '信用卡消费', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "224.71", "raw_line_index": 116, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1164, 'ccb_credit_0453_downfile_20260524_20260621_102', DATE '2026-05-28', DATE '2026-05-30', -8.8300, 'CNY', 4809.0100, 8.8300, 'CNY', '购物消费', '商超便利', TRUE, '信用卡消费', 'CLIP MX*TIENDA SYSCOM CIUDAD DE MEXMX', 'CLIP MX*TIENDA SYSCOM CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "8.83", "raw_line_index": 102, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1165, 'ccb_credit_0453_downfile_20260524_20260621_104', DATE '2026-05-28', DATE '2026-05-30', -33.6100, 'CNY', 4842.6200, 33.6100, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "33.61", "raw_line_index": 104, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1166, 'ccb_credit_0453_downfile_20260524_20260621_106', DATE '2026-05-28', DATE '2026-05-30', -39.3100, 'CNY', 4881.9300, 39.3100, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "39.31", "raw_line_index": 106, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1167, 'ccb_credit_0453_downfile_20260524_20260621_108', DATE '2026-05-28', DATE '2026-05-30', -39.3100, 'CNY', 4921.2400, 39.3100, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'ABTS LA ESKINA CHIHUAHUA CHIMX', 'ABTS LA ESKINA CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "39.31", "raw_line_index": 108, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1168, 'ccb_credit_0453_downfile_20260524_20260621_100', DATE '2026-05-28', DATE '2026-06-02', -263.6500, 'CNY', 5297.3700, 263.6500, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "263.65", "raw_line_index": 100, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1169, 'ccb_credit_0453_downfile_20260524_20260621_96', DATE '2026-05-29', DATE '2026-06-01', -112.4800, 'CNY', 5033.7200, 112.4800, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'UBER *EATS HELP.UBER.COM Ciudad de MexMX', 'UBER *EATS HELP.UBER.COM Ciudad de MexMX', '{"card_no": "4895920958780453", "csv_amount": "112.48", "raw_line_index": 96, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1170, 'ccb_credit_0453_downfile_20260524_20260621_98', DATE '2026-05-29', DATE '2026-06-02', -116.3700, 'CNY', 5413.7400, 116.3700, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "116.37", "raw_line_index": 98, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1171, 'ccb_credit_0453_downfile_20260524_20260621_88', DATE '2026-05-30', DATE '2026-06-02', -26.6500, 'CNY', 5440.3900, 26.6500, 'CNY', '交通出行', '酒店住宿', TRUE, '信用卡消费', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "26.65", "raw_line_index": 88, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1172, 'ccb_credit_0453_downfile_20260524_20260621_90', DATE '2026-05-30', DATE '2026-06-02', -51.9400, 'CNY', 5492.3300, 51.9400, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXO TRILLENIUM CUU CHIHUAHUA CHIMX', 'OXXO TRILLENIUM CUU CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "51.94", "raw_line_index": 90, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1173, 'ccb_credit_0453_downfile_20260524_20260621_92', DATE '2026-05-30', DATE '2026-06-02', -83.5400, 'CNY', 5575.8700, 83.5400, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'MC DONALD S JUVENTUD CHIHUAHUA MX', 'MC DONALD S JUVENTUD CHIHUAHUA MX', '{"card_no": "4895920958780453", "csv_amount": "83.54", "raw_line_index": 92, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1174, 'ccb_credit_0453_downfile_20260524_20260621_94', DATE '2026-05-30', DATE '2026-06-02', -224.8000, 'CNY', 5800.6700, 224.8000, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "224.80", "raw_line_index": 94, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1175, 'ccb_credit_0453_downfile_20260524_20260621_82', DATE '2026-05-31', DATE '2026-06-03', -34.1200, 'CNY', 5834.7900, 34.1200, 'CNY', '交通出行', '酒店住宿', TRUE, '信用卡消费', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "34.12", "raw_line_index": 82, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1176, 'ccb_credit_0453_downfile_20260524_20260621_84', DATE '2026-05-31', DATE '2026-06-03', -54.5400, 'CNY', 5889.3300, 54.5400, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'MC DONALD S JUVENTUD CHIHUAHUA MX', 'MC DONALD S JUVENTUD CHIHUAHUA MX', '{"card_no": "4895920958780453", "csv_amount": "54.54", "raw_line_index": 84, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1177, 'ccb_credit_0453_downfile_20260524_20260621_86', DATE '2026-05-31', DATE '2026-06-03', -163.9500, 'CNY', 6053.2800, 163.9500, 'CNY', '餐饮', '外卖餐饮', TRUE, '信用卡消费', 'DLO*UBER EATS CDMX MX', 'DLO*UBER EATS CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "163.95", "raw_line_index": 86, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1178, 'ccb_credit_0453_downfile_20260524_20260621_74', DATE '2026-06-01', DATE '2026-06-03', -3.9300, 'CNY', 6057.2100, 3.9300, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "3.93", "raw_line_index": 74, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1179, 'ccb_credit_0453_downfile_20260524_20260621_76', DATE '2026-06-01', DATE '2026-06-03', -29.9800, 'CNY', 6087.1900, 29.9800, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "29.98", "raw_line_index": 76, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1180, 'ccb_credit_0453_downfile_20260524_20260621_78', DATE '2026-06-01', DATE '2026-06-03', -42.3900, 'CNY', 6129.5800, 42.3900, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "42.39", "raw_line_index": 78, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1181, 'ccb_credit_0453_downfile_20260524_20260621_80', DATE '2026-06-01', DATE '2026-06-03', -44.7000, 'CNY', 6174.2800, 44.7000, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'ABTS LA ESKINA CHIHUAHUA CHIMX', 'ABTS LA ESKINA CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "44.70", "raw_line_index": 80, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1182, 'ccb_credit_0453_downfile_20260524_20260621_72', DATE '2026-06-01', DATE '2026-06-04', -293.4200, 'CNY', 9304.7000, 293.4200, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'UBER *EATS HELP.UBER.COM Ciudad de MexMX', 'UBER *EATS HELP.UBER.COM Ciudad de MexMX', '{"card_no": "4895920958780453", "csv_amount": "293.42", "raw_line_index": 72, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1183, 'ccb_credit_0453_downfile_20260524_20260621_70', DATE '2026-06-02', DATE '2026-06-03', -2837.0000, 'CNY', 9011.2800, 2837.0000, 'CNY', '交通出行', '机票火车', TRUE, '信用卡消费', '财付通-携程', '财付通-携程', '{"card_no": "4895920958780453", "csv_amount": "2837.00", "raw_line_index": 70, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1184, 'ccb_credit_0453_downfile_20260524_20260621_64', DATE '2026-06-02', DATE '2026-06-04', -67.9200, 'CNY', 9372.6200, 67.9200, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'STARBUCKS AERO CHIH CHH MX', 'STARBUCKS AERO CHIH CHH MX', '{"card_no": "4895920958780453", "csv_amount": "67.92", "raw_line_index": 64, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1185, 'ccb_credit_0453_downfile_20260524_20260621_66', DATE '2026-06-02', DATE '2026-06-04', -74.5100, 'CNY', 9447.1300, 74.5100, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "74.51", "raw_line_index": 66, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1186, 'ccb_credit_0453_downfile_20260524_20260621_68', DATE '2026-06-02', DATE '2026-06-04', -196.0900, 'CNY', 9643.2200, 196.0900, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*UBER RIDE CDMX MX', 'DLO*UBER RIDE CDMX MX', '{"card_no": "4895920958780453", "csv_amount": "196.09", "raw_line_index": 68, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1187, 'ccb_credit_0453_downfile_20260524_20260621_62', DATE '2026-06-02', DATE '2026-06-05', -1570.0900, 'CNY', 11213.3100, 1570.0900, 'CNY', '交通出行', '酒店住宿', TRUE, '信用卡消费', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', 'FIESTAINN FS MALL CHIH CHIHUAHUA CHIMX', '{"card_no": "4895920958780453", "csv_amount": "1570.09", "raw_line_index": 62, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1188, 'ccb_credit_0453_downfile_20260524_20260621_60', DATE '2026-06-03', DATE '2026-06-05', -23.2300, 'CNY', 11236.5400, 23.2300, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'REST PICKUP COFFEE CIUDAD DE MEXMX', 'REST PICKUP COFFEE CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "23.23", "raw_line_index": 60, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1189, 'ccb_credit_0453_downfile_20260524_20260621_58', DATE '2026-06-03', DATE '2026-06-06', -40.6600, 'CNY', 11277.2000, 40.6600, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "40.66", "raw_line_index": 58, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1190, 'ccb_credit_0453_downfile_20260524_20260621_56', DATE '2026-06-04', DATE '2026-06-07', -40.6600, 'CNY', 11317.8600, 40.6600, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "40.66", "raw_line_index": 56, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1191, 'ccb_credit_0453_downfile_20260524_20260621_50', DATE '2026-06-05', DATE '2026-06-08', -40.6000, 'CNY', 11358.4600, 40.6000, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "40.60", "raw_line_index": 50, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1192, 'ccb_credit_0453_downfile_20260524_20260621_52', DATE '2026-06-05', DATE '2026-06-08', -94.3900, 'CNY', 11452.8500, 94.3900, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "94.39", "raw_line_index": 52, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1193, 'ccb_credit_0453_downfile_20260524_20260621_54', DATE '2026-06-05', DATE '2026-06-08', -152.3300, 'CNY', 11605.1800, 152.3300, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*Uber Ciudad de Me MX', 'DLO*Uber Ciudad de Me MX', '{"card_no": "4895920958780453", "csv_amount": "152.33", "raw_line_index": 54, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1194, 'ccb_credit_0453_downfile_20260524_20260621_48', DATE '2026-06-06', DATE '2026-06-08', -498.6000, 'CNY', 12103.7800, 498.6000, 'CNY', '购物消费', '商超便利', TRUE, '信用卡消费', 'SUPER ASIATICO CIUDAD DE MEXMX', 'SUPER ASIATICO CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "498.60", "raw_line_index": 48, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1195, 'ccb_credit_0453_downfile_20260524_20260621_46', DATE '2026-06-06', DATE '2026-06-09', -126.0500, 'CNY', 12229.8300, 126.0500, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*Uber Rides Ciudad de Me MX', 'DLO*Uber Rides Ciudad de Me MX', '{"card_no": "4895920958780453", "csv_amount": "126.05", "raw_line_index": 46, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1196, 'ccb_credit_0453_downfile_20260524_20260621_40', DATE '2026-06-08', DATE '2026-06-10', -44.8900, 'CNY', 12274.7200, 44.8900, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'Starbucks Mexico City MX', 'Starbucks Mexico City MX', '{"card_no": "4895920958780453", "csv_amount": "44.89", "raw_line_index": 40, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1197, 'ccb_credit_0453_downfile_20260524_20260621_42', DATE '2026-06-08', DATE '2026-06-10', -68.1200, 'CNY', 12342.8400, 68.1200, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'Starbucks Mexico City MX', 'Starbucks Mexico City MX', '{"card_no": "4895920958780453", "csv_amount": "68.12", "raw_line_index": 42, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1198, 'ccb_credit_0453_downfile_20260524_20260621_44', DATE '2026-06-08', DATE '2026-06-10', -3617.6100, 'CNY', 15960.4500, 3617.6100, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'BAR CASA PRIME STA FE CIUDAD DE MEXMX', 'BAR CASA PRIME STA FE CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "3617.61", "raw_line_index": 44, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1199, 'ccb_credit_0453_downfile_20260524_20260621_36', DATE '2026-06-09', DATE '2026-06-11', -40.7000, 'CNY', 16001.1500, 40.7000, 'CNY', '交通出行', '打车', TRUE, '信用卡消费', 'DLO*Uber Rides Ciudad de Me MX', 'DLO*Uber Rides Ciudad de Me MX', '{"card_no": "4895920958780453", "csv_amount": "40.70", "raw_line_index": 36, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1200, 'ccb_credit_0453_downfile_20260524_20260621_38', DATE '2026-06-09', DATE '2026-06-12', -28.9000, 'CNY', 16030.0500, 28.9000, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "28.90", "raw_line_index": 38, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1201, 'ccb_credit_0453_downfile_20260524_20260621_34', DATE '2026-06-10', DATE '2026-06-13', -119.9900, 'CNY', 16150.0400, 119.9900, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "119.99", "raw_line_index": 34, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1202, 'ccb_credit_0453_downfile_20260524_20260621_32', DATE '2026-06-12', DATE '2026-06-12', 20055.2500, 'CNY', 16030.0500, -20055.2500, 'CNY', '内部划转', '信用卡还款', TRUE, '信用卡还款', '约定还款 黄展峰', '约定还款 黄展峰', '{"card_no": "4895920958780453", "csv_amount": "-20055.25", "raw_line_index": 32, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1203, 'ccb_credit_0453_downfile_20260524_20260621_30', DATE '2026-06-12', DATE '2026-06-15', -37.0600, 'CNY', 16187.1000, 37.0600, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "37.06", "raw_line_index": 30, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1204, 'ccb_credit_0453_downfile_20260524_20260621_28', DATE '2026-06-13', DATE '2026-06-15', -44.5700, 'CNY', 16231.6700, 44.5700, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'STARBUCKS CITY WALK CMX MX', 'STARBUCKS CITY WALK CMX MX', '{"card_no": "4895920958780453", "csv_amount": "44.57", "raw_line_index": 28, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1205, 'ccb_credit_0453_downfile_20260524_20260621_26', DATE '2026-06-13', DATE '2026-06-16', -58.4100, 'CNY', 16372.0800, 58.4100, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'STARBUCKS CITY WALK CMX MX', 'STARBUCKS CITY WALK CMX MX', '{"card_no": "4895920958780453", "csv_amount": "58.41", "raw_line_index": 26, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1206, 'ccb_credit_0453_downfile_20260524_20260621_24', DATE '2026-06-14', DATE '2026-06-17', -46.7400, 'CNY', 16418.8200, 46.7400, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "46.74", "raw_line_index": 24, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1207, 'ccb_credit_0453_downfile_20260524_20260621_22', DATE '2026-06-15', DATE '2026-06-15', -82.0000, 'CNY', 16313.6700, 82.0000, 'CNY', '其他支出', '手动确认', TRUE, '信用卡消费', '财付通-码上跃动', '财付通-码上跃动', '{"card_no": "4895920958780453", "csv_amount": "82.00", "raw_line_index": 22, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1208, 'ccb_credit_0453_downfile_20260524_20260621_20', DATE '2026-06-17', DATE '2026-06-20', -29.2900, 'CNY', 16448.1100, 29.2900, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "29.29", "raw_line_index": 20, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1209, 'ccb_credit_0453_downfile_20260524_20260621_18', DATE '2026-06-18', DATE '2026-06-21', -29.1500, 'CNY', 16477.2600, 29.1500, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'OXXOCITY WALK MEX MEXICO DF MX', 'OXXOCITY WALK MEX MEXICO DF MX', '{"card_no": "4895920958780453", "csv_amount": "29.15", "raw_line_index": 18, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb),
        (1210, 'ccb_credit_0453_downfile_20260524_20260621_16', DATE '2026-06-19', DATE '2026-06-21', -37.0300, 'CNY', 16514.2900, 37.0300, 'CNY', '餐饮', '日常餐饮', TRUE, '信用卡消费', 'REST PICKUP COFFEE CIUDAD DE MEXMX', 'REST PICKUP COFFEE CIUDAD DE MEXMX', '{"card_no": "4895920958780453", "csv_amount": "37.03", "raw_line_index": 16, "source_file": "账户台账/建行/downfile.csv", "source_note": "2026-06-22 建行 0453 信用卡官方 CSV 对账补录；仅补充 2026-05-21 之后缺失流水", "statement_template": "ccb_credit_csv", "unbilled_posted_balance": "16514.29"}'::jsonb)
)
INSERT INTO pams_bank_statement_rows (
    id, import_id, source_uid, profile_code, bank, account_code, account_kind,
    transaction_date, posting_date, amount, currency, balance, original_amount, original_currency,
    category, subcategory, auto_classified, summary, description, counterparty, raw_json
)
SELECT
    r.id, i.id, r.source_uid, 'ccb_credit_0453_unbilled_20260524_20260621', 'CCB', 'ccb_credit_0453', 'credit_card',
    r.transaction_date, r.posting_date, r.amount, r.currency, r.balance, r.original_amount, r.original_currency,
    r.category, r.subcategory, r.auto_classified, r.summary, r.description, r.counterparty, r.raw_json
FROM raw_rows r
CROSS JOIN import_ref i
ON CONFLICT (source_uid) DO UPDATE SET
    import_id = EXCLUDED.import_id,
    transaction_date = EXCLUDED.transaction_date,
    posting_date = EXCLUDED.posting_date,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    balance = EXCLUDED.balance,
    original_amount = EXCLUDED.original_amount,
    original_currency = EXCLUDED.original_currency,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    auto_classified = EXCLUDED.auto_classified,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    counterparty = EXCLUDED.counterparty,
    raw_json = EXCLUDED.raw_json;

SELECT setval(pg_get_serial_sequence('pams_bank_statement_rows', 'id'), GREATEST((SELECT COALESCE(MAX(id), 1) FROM pams_bank_statement_rows), 1210), true);

WITH source_rows AS (
    SELECT *
    FROM pams_bank_statement_rows
    WHERE profile_code = 'ccb_credit_0453_unbilled_20260524_20260621'
), facts AS (
    SELECT
        r.transaction_date AS event_date,
        r.posting_date,
        r.account_code,
        CASE WHEN r.summary = '信用卡还款' THEN 'ccb_7332' ELSE NULL END AS counter_account_code,
        r.amount,
        r.currency,
        1.0000000000::numeric AS fx_rate_to_cny,
        r.amount AS amount_cny,
        CASE WHEN r.summary = '信用卡还款' THEN 'debt_repayment' ELSE 'operating_expense' END AS cashflow_type,
        r.category,
        r.subcategory,
        r.counterparty,
        r.summary,
        r.description,
        CASE WHEN r.summary <> '信用卡还款' THEN TRUE ELSE FALSE END AS affects_spending,
        CASE WHEN r.summary <> '信用卡还款' THEN TRUE ELSE FALSE END AS affects_nav,
        FALSE AS affects_investment_capital,
        FALSE AS is_internal_transfer,
        CASE WHEN r.summary = '信用卡还款' THEN 'debt_repayment:ccb_7332_to_ccb_credit_0453:20260612:20055.25' ELSE NULL END AS transfer_group_id,
        'pams_bank_statement_rows' AS source_table,
        r.id::text AS source_id,
        r.source_uid,
        CASE r.source_uid
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_138' THEN '72786d32aed3e46d4e1e9dbd36ace98d820bc5193ee279e7d55c8af48d73c61e'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_140' THEN '48336044244a36859f9b3ec073f3725fdcd8ab2a700b25bd876aca9cc24b1921'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_134' THEN '1da4b47de0e6bbf8eec28c3a612d9255395b49cc129fdbaf8b1b1468c64c041f'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_136' THEN '7b68f3f5d8f24b2228d6cd3aaa5916a704ecce1f5702516fb252500cb910de64'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_132' THEN 'bbebb9fc8560416ef8b7a466cb5279b326b89d40e8890919b3bef1d029550f04'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_130' THEN '3c6fb974d1fce43ce6e103e9f17875754fc904243442f6badd72a902c3f76703'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_128' THEN 'cc231e6b895b71b83a7f0043f054429f9ce6e741f0d7430baa600a0a5af51111'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_110' THEN 'f62710e401698622a8ead271b05be14410abd8175de47513847a6d9a6d7d5357'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_114' THEN '2e3490adb0a913d3a236ac785bc23df69a23ecb257344199f5a6d73ad10d51eb'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_118' THEN '8c3155721dcecdc2ff567636171676fcc9c8df10c3db7de9f58f2cfd9df8b4fa'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_120' THEN '4e0d93320b78390416d3cba617163a98fa4506e36b9999d76187ec81975d8d02'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_122' THEN 'f90d7960a5d41d0d3edad39790a27a86485ce365d214407195d4f0043a30b996'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_124' THEN '1407365d96754e99aac1f95b0804e6d3aec4110fac576c559fbc9bf39c48d586'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_126' THEN '3163bd2717c7b34f62e547d0b139567c05a00acee52c8c4837c5285737e0f64a'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_112' THEN '82184daf62ea7ce549d35ae5e0286de49bf4ffed30a07378389d63f709ad9731'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_116' THEN 'd84ab9faabfdf28a8c629e1fd67a3db511e3bcb290070a07f31faf3af448e5cc'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_102' THEN '49980f25dbdd06b948d80e56570e8b4eb423a33dc211c49324a49cb12346ade9'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_104' THEN 'a55738524e972af327dfb765fe7bf63a2fe0946f0ceb07b22f2f3022e30fb6cb'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_106' THEN '95d72e104ede58ae8d533a92774ec3ddad780cfa7085891e9a93000cf87423f4'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_108' THEN '99093b2348fb15fc9bbbeea1b13c0779743b1ceb447ccf0da86a6c1ae3f420bb'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_100' THEN '550e116bbeb2d22a9447322a15e7e355f9ce55c8f50b520cf467ad6104c90504'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_96' THEN 'a52439541b855f97069140623cc520242b8e1096c2dd0acf1f0796b582a14eee'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_98' THEN '0f21d152aaee6a58ee3b4eb1e6edbb49aac79a7140fba6172ffce8793f825e8f'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_88' THEN '26eb71bd1a1d2b5da33f722811e5ac9c736a43f9f66a7a40ed71b5b152bcd1b3'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_90' THEN '743dda6a9dd564343e51f8d71c9fb9b5908c4d3e88b0685cd55dc9729b943f89'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_92' THEN 'd34c6f55eb44f7868c7d9264465ac92da064021c98f1389b662f61ac031b97aa'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_94' THEN 'bcc793f497ef489c4f48abe68389958eb4cce257f152348e192e8c8cf0e4c270'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_82' THEN '02f6ae8a173de3ec33b3354d3a43cb138d1c2e9df441da6c15911ba2d497a48a'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_84' THEN '18763ef26cacf0ecf229636ea7f6927b0a991068913212047b598c830e318c67'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_86' THEN '1c6b00dfe3a5ef1aa39ed39858a391ad2ab887fa80f53da1c9f57798869e98ba'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_74' THEN '76142c2f6c74a6fcbff5f907b30221f9abd3f298b75015828d6ac21396838f7b'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_76' THEN '261fceccb0aed8438957b3fe001cdd830970a5470d0738a6071c4ba068c1c187'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_78' THEN '5775f77e1b152066afe20085434e53e244b9bd609e981554e0453b78a9748654'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_80' THEN '19c3a8a727dc89ef68c5f4c88e6fd47217351a07c6bbe857be9a76008c04bbec'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_72' THEN 'ac486a7fd03ed2c196bdc169027d6a5dd46a0021b3da9489df4a4760f43fa70c'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_70' THEN '29cfd13121394a92cea4de5d3d2c8e73d3d64e3f6190b6c866464fbc10ac8b1e'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_64' THEN 'd482f8a06a2db2509a1722e7e5074371b4b46467b42486fbcca293ca5e7607ae'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_66' THEN 'e231d8f1d48020fe04898421e1a41a7c4fc1c7d7293a02554e1c19e4decae78f'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_68' THEN '28ee91c1a8bd4a1b62dc65c8cda7cd273ec97add62c2d0014b9eab1540f91ef0'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_62' THEN '77e92ee23411d068e6ec11bbd3dcf01188445f2ad9447e7d4d8e57e85221887f'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_60' THEN 'f8d846112c0e8d5dde61461afb0ca144ce3fe84f159a7087375dd9929c110547'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_58' THEN '1858b726f9cdbacce9e34ee1ef9fb417a219e8aa2f3d8dd41c75fe2b10493a26'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_56' THEN 'a7befc978860fa503b57bf40703068508384ea62d289a3e8638ee0cd7a462732'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_50' THEN '4a851c2d297340554c5975e77c46b4523abfad76fc01789fe70a87301eeff3a6'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_52' THEN '7dd2ae1019b328207ab6e0a18256ce1154a0cfe5fb74f8b065d643abd5eb70a5'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_54' THEN '261d8fe0f2b64f3e13b0dab7793e3f2536b8d39c912c403e0bc5cab82bf3d37a'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_48' THEN '217fbcc3760cd5381d28bf6f659d2b72d9311d6bb5364eebfd1fb63f3fe99a17'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_46' THEN 'd46e45b0d66822b6c27df8451ee81c0ff5c17be07835b553df501284d256af0e'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_40' THEN '664d7bf871546ef9f75bad5e313158941f75ead5d32b1b35c9695e52404cf643'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_42' THEN '1c56bf0ac55167f4693800ee0573c170c41e2aa1f31f7a0c29e78c1b6716bc87'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_44' THEN '581a4992b356f145e2697dd51e621e9fbb80d3343a841bb7ce5d75ba7ef95e59'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_36' THEN '14649ef7af990121dbc028b87373da5e20e645b9aa8978b72a6d446944c288df'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_38' THEN '568a711704eb9bf15c16db8f11ae89b6ed36e9159b40f9e6c538244d647f99b2'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_34' THEN '25b8f66e996b9f52e5b722c87902cae4040c96eb0820b02351a17f838963fb77'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_32' THEN '8f7b12e853c8cd13ba96a3d72d3b33e674c379e43556832becb1881ae92acccc'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_30' THEN 'a14be0418256b5db33d6d616cc4a4797d66be0557b6752de71f8d7505e5df59a'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_28' THEN '43de480d1d3b313ed9b1e7e799ee10d271a5802775168b0538a11ee7bc253437'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_26' THEN '7a50be82309c591aa58777466ec3b42ac2c2962c3e328fea6aedd62d790638b6'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_24' THEN '7418a5bc964cd762c27b1ae7d8a4662ae1d733245a94051b3060ad47e6821da4'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_22' THEN '68bce250f0194942954064be0f0d5616a586dcc809ee1a417d09f5f914a2b46c'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_20' THEN '1b547c5496c84212a0ef876811afc7495843e93a98618a47c011f7e103683470'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_18' THEN '2f22da5dac769518eb2d0f37d87a524cfa1c52cdcf1a148950cd4877406bbdc2'
            WHEN 'ccb_credit_0453_downfile_20260524_20260621_16' THEN 'ac63957c027e9cbde2a78031112a47eadba34464cbc171c5a1c33e95cee7d31d'
        END AS source_hash,
        jsonb_build_object(
            'statement_row', jsonb_build_object(
                'id', r.id,
                'source_uid', r.source_uid,
                'transaction_date', r.transaction_date,
                'posting_date', r.posting_date,
                'amount', r.amount::text,
                'balance', r.balance::text,
                'original_amount', r.original_amount::text,
                'original_currency', r.original_currency,
                'category', r.category,
                'subcategory', r.subcategory,
                'summary', r.summary,
                'description', r.description,
                'raw_json', r.raw_json
            ),
            'source_note', '2026-06-22 建行 0453 信用卡 CSV 对账补录；未出账单已入账金额 16,514.29'
        ) AS raw_json,
        'ccb_credit_csv_20260622' AS classification_source,
        0.9800::numeric AS confidence,
        'auto' AS review_status
    FROM source_rows r
)
INSERT INTO pams_cashflow_fact (
    event_date, posting_date, account_code, counter_account_code, amount, currency, fx_rate_to_cny, amount_cny,
    cashflow_type, category, subcategory, counterparty, summary, description, affects_spending, affects_nav,
    affects_investment_capital, is_internal_transfer, transfer_group_id, source_table, source_id, source_uid, source_hash,
    raw_json, classification_source, confidence, review_status
)
SELECT
    event_date, posting_date, account_code, counter_account_code, amount, currency, fx_rate_to_cny, amount_cny,
    cashflow_type, category, subcategory, counterparty, summary, description, affects_spending, affects_nav,
    affects_investment_capital, is_internal_transfer, transfer_group_id, source_table, source_id, source_uid, source_hash,
    raw_json, classification_source, confidence, review_status
FROM facts
ON CONFLICT (source_table, source_id) DO NOTHING;

INSERT INTO pams_bank_transfer_matches (
    match_key, match_type, relation_code, from_account_code, to_account_code, source_row_id, target_row_id, amount, currency, match_date, date_delta_days, confidence, status, note
)
SELECT
    'debt_repayment:ccb_7332_to_ccb_credit_0453:20260612:20055.25',
    'credit_card_payment',
    'ccb_to_ccb_credit_0453',
    'ccb_7332',
    'ccb_credit_0453',
    source_row.id,
    target_row.id,
    20055.2500,
    'CNY',
    DATE '2026-06-12',
    0,
    0.9900,
    'confirmed',
    '2026-06-22 建行 7332 约定还款与建行 0453 信用卡入账侧配对'
FROM pams_bank_statement_rows source_row
JOIN pams_bank_statement_rows target_row ON target_row.source_uid = 'ccb_credit_0453_downfile_20260524_20260621_32'
WHERE source_row.source_uid = 'ccb_7332_20260101_20260622_1139'
ON CONFLICT (match_key) DO UPDATE SET
    source_row_id = EXCLUDED.source_row_id,
    target_row_id = EXCLUDED.target_row_id,
    amount = EXCLUDED.amount,
    match_date = EXCLUDED.match_date,
    status = EXCLUDED.status,
    confidence = EXCLUDED.confidence,
    note = EXCLUDED.note,
    updated_at = NOW();

UPDATE pams_cashflow_fact
SET raw_json = COALESCE(raw_json, '{}'::jsonb) || jsonb_build_object(
        'paired_credit_card_cashflow_source', 'pams_bank_statement_rows:ccb_credit_0453_downfile_20260524_20260621_32',
        'manual_pairing', '2026-06-22 建行 7332 约定还款与建行 0453 信用卡入账侧配对'
    ),
    updated_at = NOW()
WHERE source_table = 'pams_bank_statement_rows'
  AND source_id = '1139';

COMMIT;
