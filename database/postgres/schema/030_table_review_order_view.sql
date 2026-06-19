-- 030_table_review_order_view.sql
--
-- A read-only helper view for manual table review in Adminer.
-- It does not change business data or application query paths.

DROP VIEW IF EXISTS public.pams_table_review_order;

CREATE OR REPLACE VIEW public.pams_table_review_order AS
WITH review_order (
    review_order,
    priority,
    schema_name,
    table_name,
    review_group,
    meaning,
    check_focus
) AS (
    VALUES
        (10, 'P0', 'public', 'pams_accounts', '账户主数据', '账户维度；现金流、持仓、交易、收益归属的共同维度。', '逐行确认账户是否真实存在；重点看 account_type、account_role、base_currency、status。'),
        (20, 'P0', 'public', 'pams_asset_master', '资产主数据', '资产身份、类型、市场、币种、估值方式的核心表。', '确认资产身份是否唯一；重点看 asset_id、primary_symbol、asset_type、market、currency、status。'),
        (30, 'P0', 'public', 'pams_holdings_snapshot', '持仓快照', '当前/历史持仓状态事实表。', '重点看 asset_id、account_code、major_category、minor_category、market_value、cost、as_of。'),
        (40, 'P0', 'public', 'pams_cashflow_fact', '现金流事实', '收支、内部划转、投资入出金、现金收益事实表。', '重点看 account_code、counter_account_code、cashflow_type、amount、category、source_table/source_id。'),
        (50, 'P0', 'public', 'pams_investment_trade_fact', '投资交易事实', '买卖、分红、费用、换汇、券商入出金事实表。', '重点看 account_code、asset_id、trade_type、quantity、net_cash、cashflow_id、source_table/source_id。'),
        (60, 'P0', 'public', 'pams_holding_return_snapshot', '持仓收益快照', '收益曲线与收益卡片的预计算结果。', '重点看 snapshot_date、section、market_value、cost、total_pnl、return_rate、source_hash。'),
        (70, 'P0', 'public', 'pams_investment_trade_snapshot', '交易活动快照', '由投资交易事实聚合生成的交易活动快照。', '重点看 snapshot_date、account_code、buy/sell/deposit/withdraw/dividend、rows_count。'),
        (80, 'P0', 'public', 'pams_fx_rates_daily', '汇率日表', '跨币种估值、收益和归因的汇率表。', '重点看 as_of、base_currency、quote_currency、rate、source。'),

        (110, 'P1', 'public', 'pams_asset_alias', '资产识别辅助', '外部代码、名称、别名到资产主数据的映射。', '检查 alias_value 是否重复指向多个 active asset；重点看 alias_type、priority、source。'),
        (120, 'P1', 'public', 'pams_asset_exposure_relation', '资产暴露关系', '行业、主题、基金/ETF 底层成分统一关系表。', '重点看 relation_type、relation_key、related_asset_id、weight、as_of、coverage_type。'),
        (130, 'P1', 'public', 'pams_asset_market_daily', 'PAMS 估值辅助日表', '基金净值、ETF 日度等 PAMS 自用估值数据；不含外部 OHLCV 大表。', '重点看 asset_id、as_of、source、source_key、close_price、unit_nav。'),
        (140, 'P1', 'public', 'pams_source_row', '导入源行', '银行流水/外部导入源行统一表。', '重点看 source_system、source_table、source_id、account_code、row_hash、raw_json。'),
        (150, 'P1', 'public', 'pams_external_transaction_source', '外部交易源', 'IBKR、TradingView、旧投资流水等交易源统一表。', '重点看 source_system、account_code、trade_date、symbol、raw_json。'),
        (160, 'P1', 'public', 'pams_reconciliation_case', '对账 case', '银行转账、投资入出金、人工审核的统一对账记录。', '重点看 case_type、from/to_account_code、amount、status、review_action。'),
        (170, 'P1', 'public', 'pams_data_job_run', '数据任务审计', '同步任务、数据版本、运行审计统一表。', '重点看 job_key、job_type、target_date、status、rows_affected、checksum。'),

        (210, 'P2', 'public', 'pams_risk_snapshot', '风控模块', '低频风控状态快照。', '确认是否仍被风控页或策略模块使用；看 as_of、b_nav、risk 字段。'),
        (220, 'P2', 'public', 'pams_risk_events', '风控模块', '风险事件记录。', '确认事件 code/severity 是否有业务含义，是否能从 risk_state 重建。'),
        (230, 'P2', 'public', 'pams_event_monitor', '事件监控模块', '事件监控预留。', '确认是否有 owner；无 owner 可降级清理。'),
        (240, 'P2', 'public', 'pams_notifications', '通知模块', 'PAMS 通知记录。', '确认是否仍发送通知；无使用则降级。'),
        (250, 'P2', 'public', 'pams_task_queue', '任务模块', '内部任务队列。', '确认是否有后台 worker 使用；无 owner 可清理。'),
        (260, 'P2', 'public', 'pams_order_drafts', '策略模块', '订单草稿，不代表自动交易。', '确认只作为草稿/模拟，不扩展成自动交易。'),
        (270, 'P2', 'public', 'pams_policy', '策略模块', '策略/系统政策配置。', '确认 key/value 是否仍有效。'),
        (280, 'P2', 'public', 'pams_position_cooldown', '策略模块', '持仓冷却/交易约束。', '确认是否被策略扫描使用。'),
        (290, 'P2', 'public', 'pams_strategy_state', '策略模块', '策略运行状态。', '确认是否仍被策略页或扫描器使用。'),
        (300, 'P2', 'public', 'pams_strategy_candidates', '策略模块', '策略候选/信号。', '确认字段是否只是研究输出，不要进入 P0 事实链路。'),
        (310, 'P2', 'public', 'pams_strategy_blacklist', '策略模块', '策略黑名单。', '确认 active/expires_at 语义。'),
        (320, 'P2', 'public', 'pams_watchlist', '自选模块', '自选/观察列表。', '确认是否仍用于更多页或研究页。'),
        (330, 'P2', 'public', 'pams_watchlist_log', '自选模块', '自选变更日志。', '确认是否需要长期保留。'),
        (340, 'P2', 'public', 'pams_sentiment_state', '市场情绪模块', '情绪状态预留。', '确认是否仍由策略/风控使用。'),
        (350, 'P2', 'public', 'pams_budgets', '预算模块', '预算配置预留。', '当前若未启用，可降级。'),
        (360, 'P2', 'public', 'pams_asset_fixed_income_profile', '资产扩展画像', '固收资产扩展画像。', '当前为空时确认 owner。'),
        (370, 'P2', 'public', 'pams_asset_real_asset_profile', '资产扩展画像', '实物/另类资产扩展画像。', '当前为空时确认 owner。'),
        (380, 'P2', 'public', 'pams_fundamental_snapshot', '基本面模块', '基本面快照预留。', '确认是否仍服务研究页/策略页。'),
        (390, 'P2', 'public', 'pams_news_items', '新闻模块', '新闻数据预留。', '确认是否启用；未启用可降级。'),
        (400, 'P2', 'public', 'pams_sim_accounts', '模拟账户模块', '模拟账户状态。', '确认是否仍需要模拟账户体系。'),
        (410, 'P2', 'public', 'pams_sim_cash_ledger', '模拟账户模块', '模拟现金流水。', '确认是否可由模拟结果文件重建。'),
        (420, 'P2', 'public', 'pams_sim_holdings_snapshots', '模拟账户模块', '模拟持仓快照。', '确认是否可由模拟结果文件重建。'),
        (430, 'P2', 'public', 'pams_sim_reconcile_summaries', '模拟账户模块', '模拟对账摘要。', '确认是否可由模拟结果文件重建。'),
        (440, 'P2', 'public', 'pams_sim_sync_logs', '模拟账户模块', '模拟同步日志。', '确认是否可由模拟结果文件重建。')
),
stats AS (
    SELECT
        n.nspname AS schema_name,
        c.relname AS table_name,
        CASE c.relkind WHEN 'v' THEN 'view' ELSE 'table' END AS object_kind,
        c.reltuples::bigint AS estimated_rows,
        pg_total_relation_size(c.oid) AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v')
),
columns AS (
    SELECT table_schema AS schema_name, table_name, count(*) AS column_count
    FROM information_schema.columns
    GROUP BY table_schema, table_name
),
constraints AS (
    SELECT table_schema AS schema_name, table_name, count(*) AS constraint_count
    FROM information_schema.table_constraints
    GROUP BY table_schema, table_name
)
SELECT
    r.review_order,
    r.priority,
    r.review_group,
    r.schema_name,
    CASE WHEN r.priority = 'P2' THEN 'pams_module' ELSE r.schema_name END AS storage_schema_name,
    r.table_name,
    s.object_kind,
    COALESCE(s.estimated_rows, 0) AS estimated_rows,
    pg_size_pretty(COALESCE(s.total_bytes, 0)) AS total_size,
    COALESCE(c.column_count, 0) AS column_count,
    COALESCE(k.constraint_count, 0) AS constraint_count,
    r.meaning,
    r.check_focus
FROM review_order r
LEFT JOIN stats s
  ON s.schema_name = CASE WHEN r.priority = 'P2' THEN 'pams_module' ELSE r.schema_name END
 AND s.table_name = r.table_name
LEFT JOIN columns c
  ON c.schema_name = CASE WHEN r.priority = 'P2' THEN 'pams_module' ELSE r.schema_name END
 AND c.table_name = r.table_name
LEFT JOIN constraints k
  ON k.schema_name = CASE WHEN r.priority = 'P2' THEN 'pams_module' ELSE r.schema_name END
 AND k.table_name = r.table_name
ORDER BY r.review_order;

GRANT SELECT ON public.pams_table_review_order TO pams_readonly;
