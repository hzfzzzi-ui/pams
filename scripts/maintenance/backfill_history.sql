-- PAMS 历史数据回填脚本
-- 用途：基于当前快照生成过去30天的模拟历史数据
-- 注意：这是模拟数据，仅用于测试图表展示
-- 如果有真实历史数据，应该从Excel/CSV导入

-- ==========================================
-- 方案A：模拟历史数据（用于测试）
-- ==========================================
-- 基于2026-02-15的快照，生成前30天的数据
-- 每天随机波动±5%

DO $$
DECLARE
    base_date DATE := '2026-02-15';
    days_back INTEGER := 30;
    i INTEGER;
    current_date DATE;
    random_factor NUMERIC;
BEGIN
    FOR i IN 1..days_back LOOP
        current_date := base_date - i;
        random_factor := 0.95 + (random() * 0.10); -- 0.95 to 1.05

        -- 复制当前快照，调整日期和市值
        INSERT INTO pams_holdings_snapshot (
            as_of, venue, account, type, symbol, name, currency,
            market_value, holding_pnl, daily_pnl,
            quantity, cost_basis, cost_currency, created_at
        )
        SELECT
            current_date,
            venue,
            account,
            type,
            symbol,
            name,
            currency,
            market_value * random_factor, -- 随机波动
            holding_pnl * random_factor,
            (market_value * random_factor - market_value) / NULLIF(market_value, 0) * 100, -- 模拟日涨跌
            quantity,
            cost_basis,
            cost_currency,
            NOW()
        FROM pams_holdings_snapshot
        WHERE as_of = base_date;

        -- 插入对应日期的汇率
        INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
        VALUES
            (current_date, 'CNY', 'CNY', 1.0, 'manual'),
            (current_date, 'CNY', 'USD', 7.20 + (random() * 0.10), 'simulated'), -- 7.20-7.30
            (current_date, 'CNY', 'MXN', 0.40 + (random() * 0.04), 'simulated')  -- 0.40-0.44
        ON CONFLICT (as_of, base_currency, quote_currency)
        DO UPDATE SET rate = EXCLUDED.rate;

        RAISE NOTICE '✅ Generated snapshot for %', current_date;
    END LOOP;

    RAISE NOTICE '📊 Backfill complete: % days of history data', days_back;
END $$;

-- 验证生成的数据
SELECT
    as_of,
    COUNT(*) AS records,
    SUM(market_value) AS total_market_value
FROM pams_holdings_snapshot
GROUP BY as_of
ORDER BY as_of DESC
LIMIT 10;

-- ==========================================
-- 方案B：从真实历史数据导入（如果有Excel）
-- ==========================================
/*
如果您有历史的资产负债表Excel文件，请按以下步骤：

1. 准备数据
   - 整理历史Excel文件（按日期命名，如 balance_2026-02-14.xlsx）
   - 确保格式与现有 balance_sheet_export.csv 一致

2. 导出CSV
   cd /c/Users/hzfzzzi/openclaw/pams/import
   # 假设有历史文件：balance_2026-02-14.csv, balance_2026-02-13.csv...

3. 批量导入
   for file in balance_*.csv; do
     DATE=$(echo $file | grep -oP '\d{4}-\d{2}-\d{2}')
     echo "Importing $file for date $DATE..."
     node ../src/import_balance_sheet.js "$file" --date "$DATE"
   done

4. 刷新衍生表
   curl -X POST http://pams.local/api/admin/refresh
*/

-- ==========================================
-- 方案C：手动填充特定日期
-- ==========================================
/*
-- 示例：手动添加2026-02-14的数据
INSERT INTO pams_holdings_snapshot (
    as_of, venue, account, type, symbol, name, currency,
    market_value, holding_pnl, daily_pnl
) VALUES
    ('2026-02-14', 'manual', 'huawei_emp', 'equity_private', NULL, '华为员工内部持股', 'CNY', 318000.0, 0, 0),
    ('2026-02-14', 'guosen', 'cn_a_rmb', 'stock_cn', '601899', '紫金矿业', 'CNY', 31500.0, 4000.0, -200.0),
    -- ... 更多记录
;

-- 添加汇率
INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
VALUES
    ('2026-02-14', 'CNY', 'CNY', 1.0, 'manual'),
    ('2026-02-14', 'CNY', 'USD', 7.22, 'manual'),
    ('2026-02-14', 'CNY', 'MXN', 0.41, 'manual');
*/
