# PAMS 数据库 Schema 分层目录

本文档记录当前空库初始化清单 [manifest.txt](../database/postgres/bootstrap/manifest.txt) 中生效的 **24 个 Schema 脚本** 与核心数据表职责分层。

> **铁律**：`database/postgres/schema/` 下的所有文件必须与 `bootstrap/manifest.txt` 保持 **100% 一一对应**（仅用于空库初始化，禁止对生产库批量重放）。已被新架构取代的历史脚本与零引用退役脚本归档至 `database/postgres/archive/`。

---

## 一、来源凭证与导入层（不可删、保留原始证据）

| 表名 | 所在脚本 | 职责 |
| --- | --- | --- |
| `source_templates`、`source_batches`、`source_files`、`source_records`、`source_items` | `079_source_records.sql`、`080_source_items_redesign.sql` | 统一资料导入模板、批次、原件、解析记录与条目 |
| `ibkr_source_originals`、`tonghuashun_source_originals` | `085_broker_source_originals.sql` | 券商原始日结与对账凭证原件 |
| `evidence_confirmation` | `077_confirmed_transfers.sql` | 人工核准与跨账户划转确认凭证 |
| `valuation_evidence` | `076_valuation_input_evidence.sql` | 组合估值输入证明与归属依据 |

## 二、核心业务事实与主数据层（单一事实来源）

| 表名 | 所在脚本 | 职责 |
| --- | --- | --- |
| `pams_accounts`、`account_snapshot_version`、`account_snapshot_member` | `014_accounts.sql`、`025a_asset_snapshots.sql` | 账户主数据与账户快照版本/成员 |
| `pams_asset_master`、`pams_asset_alias`、`pams_fund_underlying_position`、`asset_classifications` | `023_asset_master.sql`、`011_industry_fallback.sql` | 标的身份主数据、别名映射、基金底层持仓与行业分类 |
| `pams_cashflow_fact`、`cashflow_classification` | `081_fact_redesign.sql`、`082_cashflow_classification.sql` | 现金收支、内部划转与投资资金进出事实及分类 |
| `pams_investment_trade_fact`、`investment_trade_review` | `081_fact_redesign.sql`、`083_trade_review_redesign.sql` | 投资买卖/分红/费用/换汇事实与独立复盘标注 |
| `pams_holdings_snapshot` | `026_core_architecture_refactor.sql`、`029a_holding_asset_reference_constraints.sql`、`084_holding_source_link.sql` | 持仓快照及与资产主数据/来源条目的关联约束 |

## 三、市场行情与只读参考层

| 表名 | 所在脚本 | 职责 |
| --- | --- | --- |
| `pams_fx_rates_daily` | `004_fx_rates.sql` | 每日历史汇率（严格同日折算依据） |
| `pams_ohlcv_daily`、`market_observations` | `010_market_ohlcv.sql`、`013a_alter_ohlcv.sql` | 日线行情与市场报价观察 |
| `pit_security_master`、`pit_industry_history`、`pit_index_daily`、`pams_fundamental_snapshot` | `013_market_reference.sql` | 证券名称、行业历史、指数日线与基本面只读参考（仍被 `asset_names` / `lookthrough_state` / `price_history` / `risk_state` 引用） |
| 基准种子数据 | `033a_benchmark_asset_seed.sql`、`034_gold_spot_benchmark.sql` | 基准资产与黄金现货基准配置 |

## 四、派生估值与页面投影层（设计上可重建）

| 表名 | 所在脚本 | 职责 |
| --- | --- | --- |
| `valuation_results` | `025_valuation_results.sql` | 组合与账户估值结果 |
| `broker_processing_state` | `078_broker_publication_state.sql` | 券商同步与日结发布状态机 |
| `investment_page_projection`、`investment_projection_invalidation` | `086_investment_page_projection.sql` | 投资页读投影与失效队列 |
| `pams_data_version` | `013_market_reference.sql` | 数据版本元数据 |

---

## 五、已归档脚本（`database/postgres/archive/`）

以下脚本已移出 `schema/`，不再参与空库初始化（现网已存在的历史表保持不动，待独立清理授权）：
- `011_shared_sentiment.sql`：退役舆情表 `pams_sentiment_state`（代码零引用）。
- `028a_account_reference_constraints.sql`、`035_trade_review_fields.sql`、`036_trade_position_identity.sql`、`071_investment_trade_fee_currency.sql`、`074_unified_source.sql`、`075_source_fact_links.sql`：均已被 `079`–`084` 的 redesign 脚本完整取代。
