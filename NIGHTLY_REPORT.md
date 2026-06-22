# PAMS Autonomous Improvement Report

## 2026-06-23

### 本轮目标

- 检查后端对账链路是否能从交易入账、收支入账开始自动闭环。
- 优先修复低风险、高收益的自动化断点，不改前端 UI 和资产事实数据。

### 已修复问题

- 银行流水导入完成后，自动执行转账匹配和 `pams_cashflow_fact` 回填，避免新导入数据停留在 staging 表。
- 转账匹配结果同步写入统一对账表 `pams_reconciliation_case`，减少旧 `pams_bank_transfer_matches` 与页面消费表之间的口径漂移。
- 双边匹配的转账 case 标记为 `auto`；只有单边来源的 source-only 匹配标记为 `open`，避免把未到账的投资入金误判为已确认。
- `cashflow_fact` 回填增加 `source_uid` 去重，避免重导入导致同一流水被反复扫描为待事实化。

### 待确认问题

- 同花顺、理财通等投资账户的入金仍需要继续补全银行侧现金流与投资交易 fact 的 `cashflow_id` 绑定。
- 旧表 `pams_bank_transfer_matches` 仍保留兼容；后续需要确认是否可以降级为只读兼容层。
- source-only 投资入金进入 `open` 后，账户页和流水页的提示文案是否需要进一步区分“在途”与“待确认”。

### 风险项

- 本轮没有直接修改生产资产数据；只有在后续触发银行导入或转账匹配接口时，新逻辑才会写入 facts/cases。
- 当前工作区存在大量历史脏状态，本轮只触碰对账链路相关 5 个文件。

### 验证记录

- `python -m pytest tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py tests/test_bank_reconcile_matching.py tests/test_ledger_view_service.py tests/test_accounts_view_service.py tests/test_portfolio_valuation_linkage.py -q`
- `python -m py_compile backend/api/endpoints/bank_reconcile.py backend/api/services/cashflow_fact.py`
- `docker compose up -d --build pams-api`
- Smoke：`/api/bank-reconcile/transfer-matches?limit=1` 返回 200，`/api/ledger/workbench` 返回 200。

### 美股夜盘收益刷新专项

发现的问题：

- 美股/海外价格刷新仍按北京时间自然日写入快照；凌晨 0:00-7:00 的 US 市场数据会和持仓页 daily PnL 窗口错位。
- 海外持仓刷新按账户 bucket 取最新快照时，6/23 只有港股一行会遮挡 6/22 的 USD 完整持仓，导致 `market=us` 刷新找到 0 个标的。
- 当前本机 IB Gateway 已启动，但 4000/4001/4002/7497/7496 与 Client Portal 5000 在容器内均为 `Connection refused`；未登录或 API socket 未监听时，美股夜盘无法取到 IBKR 高可信价格。

已修复问题：

- 美股/海外刷新写入日期改为 07:00 北京时间 daily PnL 窗口日期，和持仓页、分析页今日收益标签保持一致。
- 海外刷新取数改为按市场 bucket 选择各自最新快照，避免港股/韩股/美股互相遮挡。
- IBKR 估算价进入持仓快照前增加与上一快照价格的 sanity guard，防止明显固定点尺度漂移。

验证记录：

- `python -m pytest tests/test_prices_refresh_endpoint.py tests/test_frontend_static_contract.py -q`
- `python -m pytest tests/test_portfolio_summary_service.py tests/test_return_attribution.py -q`
- `python -m py_compile backend/api/endpoints/prices.py`
- `docker compose up -d --build pams-api`
- Smoke：`/api/prices/refresh/us-stock?market=us&priceBasis=estimate` 可找到 7 个 USD 持仓；在 IBKR socket 未监听时拒绝 Yahoo fallback 写入。

## 2026-06-22

### 本轮目标

- 按低风险、高收益、小 diff 原则执行首轮自主改进。
- 不改 UI 主结构，不改资产事实数据，不改变收益/风控口径。
- 先补稳定性契约，再记录后续需要拆分的小修方向。

### 发现的问题

- 基金预估净值表 `pams_fund_nav_estimates` 已被持仓 API 和同步中心依赖，但正式 schema 目录缺少建表迁移，fresh DB 可能在基金刷新前访问持仓页失败。
- 自动同步链路对失败、partial、过期数据的判断不足，失败刷新后仍可能继续重建收益快照，需要后续拆成单独 PR。
- 历史投资交易折汇存在使用最新汇率的路径，后续应统一为交易日当日或之前最近汇率，并暴露 fallback 状态。
- data-quality 接口实际加载了大量明细数据，后续可改为聚合查询，降低页面预热成本。
- 前端通用 empty/error/loading 状态缺少可访问性语义，后续可小范围补 `role` 和 `aria-live`，不改视觉风格。

### 已修复问题

- 新增正式 schema 迁移 `067_fund_nav_estimates.sql`，让基金预估收益状态表不再只依赖运行时 DDL。
- 新增 schema 契约测试，锁定基金预估表的核心字段、唯一键和常用查询索引。
- 修正 `/api/fx-rates/latest` 顶层 `as_of`：从“第一行日期”改为所有返回币种里的最新日期，避免不同币种日期不一致时页面误读。
- 给通用 loading/error/empty 数据状态补 `role` 与 `aria-live`，并把空状态 badge 从“空”改为“暂无”，保持原视觉但提升可理解性。

### 待确认问题

- 同步调度器是否应在任一关键市场数据失败时跳过收益快照重建，还是允许 partial 但在页面显式标注。
- IBKR Flex 旧 statement 是否允许对齐到最新组合日期，需要设定最大容忍天数。
- NAV、账户页、收益页的 FX fallback 字段是否统一通过一个服务输出。

### 风险项

- 本轮未改生产资产数据；运行态只重建 `pams-api` 和 `pams-web` 以验证 API 与静态资源改动。
- 工作区存在大量历史脏状态，本轮只应提交 schema、FX 服务、前端通用状态、版本号、测试和本报告相关文件。

### 验证记录

- `python -m pytest tests/test_fx_rates_service.py tests/test_fund_nav_estimates_schema_contract.py tests/test_holdings_view_service.py tests/test_frontend_static_contract.py -q`
- `python -m py_compile backend/api/services/fx_rates.py backend/api/services/fund_nav_estimates.py backend/api/services/holdings_view.py`
- `node --check` 已覆盖本轮触碰的前端模块。
- `docker compose config --quiet`
- `docker compose up -d --build pams-api`
- `docker compose up -d --build pams-web`
- Smoke：`investments.html`、`ledger.html`、`more.html`、`/health`、`/api/fx-rates/latest?base_currency=CNY` 均返回 200。

### 下一轮优化建议

1. 给基金 NAV 刷新和市场数据刷新返回 `ok / partial / error`，并让调度器基于结果决定是否重建收益快照。
2. 优先修复历史交易折汇：所有交易 fact 写入路径按 `fx.as_of <= trade_date` 取最近汇率。
3. 将 `portfolio/data-quality` 改为聚合查询，避免页面启动时拉取全量明细。
4. 给通用错误和空状态补可访问性语义，并补保存按钮防重复提交。
