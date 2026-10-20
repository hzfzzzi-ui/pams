# PAMS Autonomous Improvement Report

## 2026-10-20：持仓状态别名兼容（M 级）

- 页面复测发现旧 `daily_pnl_status` 仍先于新 `daily_return_status` 被读取，可能一边 pending、一边 confirmed。
- API 统一两套兼容字段及文案，消除前端继续显示历史确认的路径，不改变金额与任何资产事实。
- 新增旧字段覆盖回归；需部署后以正式页面确认基金不再标记今日已确认。

## 2026-10-20：运行调试第一轮（M 级）

- 发现：旧基金 NAV 的来源状态会被当成今日已确认，即便净值日期早于展示日期。
- 修复：持仓状态只认可同一展示日期的来源，历史来源显示未刷新；保留来源日期，不改变金额、份额、成本和盈亏。
- 测试：增加旧基金 NAV、旧行情确认和当日基金确认回归；正式 `tests/` 497 项通过，定向 lint、mypy 和 API 镜像构建通过。
- 运行：Docker 已恢复；已构建并部署 API、前端和调度器，核准原项目挂载及既有数据库卷。
- 备份：资产相关 `public.pams_*` 表备份已完整读取校验。全库/全 public 备份含大量研究行情，因 Compose 重建数据库容器被中断，已标为 `.incomplete`，不能用作完整备份。
- 数据：部署前后 28 张既有 PAMS 表校验值一致，只新增 069 中三张空状态表。旧版本过期已发布 NAV 覆盖被移除，页面值因此变化，不代表发生交易，也不能宣称资产总值已核准。
- 同步：重启前后检查点文件校验一致；启动补跑明确阻断历史回填，未写入收益快照。历史完整性仍为未核验。
- 待办：统一质量面板未展示 API 已返回的警告；A股、基金、贵金属行情过期；账户账单缺口、研究任务异常、归因跨日期差异仍需单独处置。
- 边界：本轮未执行真实交易录入、导入确认、账户核准或付费行情请求。根目录无参数 pytest 会收集已退出正式验证范围的旧 backtest_V2 并报缺模块；正式验收命令为 CI 中的 `python -m pytest tests -q`。

## 2026-10-20：恢复主线与同步可靠性

### 发现的问题

- 21 条历史 PR 未收敛；开发分支、main 与本地历史修改不一致。
- 干净检出缺少导入模块和旧入口说明；镜像遗漏服务代码及显式运行依赖，并引用未跟踪的运行数据目录。
- 调度器把 HTTP 成功当成业务成功，部分更新、旧 Flex 和基金净值缺失仍可能继续发布收益快照。
- 停机后无持久化成功日期和历史缺口记录。

### 已修复

- 原工作区保留，Git bundle、修改补丁及未跟踪清单已保存到本地 recovery 目录。
- 历史 PR 按依赖合并或注明替代关系后关闭；#28–31 分别收敛打包、行情状态、持仓状态、依赖及镜像验证。
- 调度增加业务结果校验、失败阻断、三次以内网络重试和只读同步健康输出。
- 检查点原子持久化、成功窗口去重、失败补跑、历史缺口保留；禁止用当前价伪装历史数据。
- 详细处置、补跑边界及部署限制见 `docs/RELEASE_RECOVERY_20261020.md`。

### 验证与风险

- 干净基线 451 项测试通过；增加同步边界测试后，独立虚拟环境 494 项通过。
- API ASGI 路由 smoke、Compose 配置、Python 编译、定向 lint 和核心类型检查已执行。
- PR #31 云端 CI 已验证临时 Postgres、API/前端镜像；同步改动还增加调度镜像与纽约时区检查，最终结果以 PR #32 的最终提交检查为准。
- 本机 Docker Desktop 启动失败，自动审批拒绝清理遗留 socket，生产部署及真实数据验收尚未完成。
- 单项刷新已落库的数据不自动回滚；历史交易日完整回补、交易所特定节假日以及生产资产对账仍需后续验证。

### 下一轮

- 恢复 Docker 后保存数据库底稿，核对历史价格与交易缺口，再启用生产调度。
- 对资产、负债、内部划转和期初/期末 NAV 做独立对账，不把 CI 通过等同于真实资产已核准。

## 2026-06-28 追加：A股账户总盈亏绑定修复

### 本轮目标

- 修复持仓页 A 股账户顶部“总盈亏”显示退回持仓口径的问题。
- 保持后端 API、数据库、事实表和持仓行展示不变，只修正前端汇总字段绑定。

### 已修复问题

- `holdings_categories.js` 中 A 股账户汇总现在显式将 `accountTotalPnl` 映射为通用 `totalPnl`，避免顶部卡片继续使用持仓行汇总盈亏。
- A 股账户收益率优先使用 `accountReturnRate`，持仓收益率仅保留为行项目和持仓口径展示。
- 同步提升 `holdings_categories.js` 静态资源版本号，避免浏览器继续加载旧缓存。
- 新增前端静态契约测试，锁定 A 股账户卡必须使用账户级总盈亏。
- 追加修复 IBKR 半字段账户 performance：当接口没有 `total_pnl_cny` 或 `net_deposit_cny` 时，不再用账户净值反推总盈亏，避免把融资现金负债推成组合大额亏损。

### 验证记录

- `node --check frontend\public\assets\js\holdings_categories.js`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `docker compose --env-file C:\Users\hzfzzzi\openclaw\pams\.env up -d --build pams-web`
- HTTP smoke：`/investments.html` 已引用 `holdings_categories.js?v=20260628-account-total-pnl-v2`，线上脚本包含账户级总盈亏映射和 IBKR 半字段保护。

## 2026-06-28

### 本轮目标

- 修复持仓页移动端条目“持有金额”误显示为市值的问题。
- 保持总资产、市值、权重和收益口径不变，只调整单个展示字段。

### 已修复问题

- 移动端持仓条目的主金额由 `market_value` 改为 `cost`，文案同步为“持仓成本”。
- 详情页仍保留“市值”和“剩余成本”，用于区分市场估值与成本口径。
- 新增前端静态契约测试，防止该字段再次绑定回市值。

### 验证记录

- `node --check frontend\public\assets\js\holdings.js`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `git diff --check -- frontend/public/assets/js/holdings.js frontend/public/investments.html tests/test_frontend_static_contract.py`
- `docker compose --env-file C:\Users\hzfzzzi\openclaw\pams\.env up -d --build pams-web`
- 运行态 HTTP 校验：`/investments.html` 已引用 `holdings.js?v=20260628-holding-cost-display-v1`，新版 JS 包含“持仓成本”展示。

### 风险项

- 本轮没有修改后端资产事实表，也没有改变总资产、收益率、权重和 NAV 口径。
- in-app browser 可视化刷新检查超时，已用 HTTP 静态资源校验替代；若右侧仍显示旧文案，需要浏览器强刷。

## 2026-06-24

### 本轮目标

- 先保存当前资产与收益数据底稿，再做小范围后端口径修正。
- 修正 PortfolioState、NAV bridge、收益归因之间的净值口径漂移。
- 每完成一项改动后自检，禁止用前端临时计算掩盖后端数据问题。

### 已保存底稿

- 开工底稿：`.codex-artifacts/audits/baseline-20260624-163222`
- 改后快照：`.codex-artifacts/audits/after-20260624-165136`

### 已修复问题

- `PortfolioState` 的投资交易组件改为读取 `pams_investment_trade_fact`，不再依赖旧的空表口径；当前运行态读取到 331 笔投资交易。
- `PortfolioState` 的估值序列改为读取 `pams_portfolio_valuation_state`，并保留投资市值、总盈亏、收益率等统一字段。
- 数据质量接口新增结构化检查：组件覆盖、账户余额缺失、持仓日期缺失、估值序列滞后、外币汇率缺失。
- NAV bridge 新增可审计的逐步桥接：期初 NAV + 外部现金流 + 投资/估值影响 + 残差 = 期末 NAV。
- 收益归因的 `portfolio_nav_cny` 改为使用统一 NAV 账户估值口径，避免 IBKR 等杠杆账户用总资产或账户余额重复统计。
- NAV bridge 不再只看总残差为 0 就显示可信；若投资增值与收益归因仍有差额，会标记 `unreconciled`。

### 当前审计结论

- 运行态 `portfolio/state` 组件行数：账户余额 24、当前持仓 31、投资交易 331、现金流水 359、转账匹配 10、汇率 6、估值序列 1。
- 运行态 NAV bridge：期初 NAV 798,725.37，期末 NAV 1,123,955.32。
- 收益归因 `portfolio_nav_cny` 已对齐 NAV bridge 期末 NAV：1,123,955.32。
- 数据质量状态为 `warning`：缺少 `alipay`、`gf_7443`、`gf_9711` 账户余额；估值序列日期仍滞后于当前持仓日期。
- NAV bridge 状态仍为 `unreconciled`：投资增值与收益归因存在 63,366.72 元解释缺口。该缺口现在会被显式暴露，不再被隐藏为可信。

### 验证记录

- `python -m compileall -q backend\api\services`
- `python -m pytest tests/test_portfolio_state_contract.py tests/test_portfolio_valuation_state.py tests/test_valuation_nav_service.py tests/test_portfolio_equity_bridge.py tests/test_return_attribution.py tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q`
- 结果：98 passed。
- `docker compose up -d --build pams-api`
- 运行态 smoke：`/health` 返回 ok，`/api/portfolio/state`、`/api/portfolio/data-quality`、`/api/portfolio/equity-bridge`、`/api/analysis/return-attribution` 均可返回。

### 下一轮建议

1. 继续追 NAV 投资归因缺口 63,366.72 元，优先检查已清仓/已实现盈亏、历史估值快照和期初持仓回溯。
2. 补齐或显式排除 `alipay`、`gf_7443`、`gf_9711` 账户余额，降低数据质量 warning。
3. 补齐连续估值序列，让 NAV 曲线不再依赖单日估值状态。

### 追加轮次：NAV 质量状态分层

#### 发现的问题

- NAV bridge 的总账 roll-forward 已经闭合：期初 NAV + 外部现金流 + 期间投资/估值影响 = 期末 NAV，运行态 `unexplained_residual_cny = 0.00`。
- 原先 `quality.status = unreconciled` 的主因不是总账不平，而是把 `period_investment_effect_cny = 14,484.20` 与 `canonical_total_pnl_cny = 77,850.93` 直接比较。
- 这两个数不是同一层口径：前者是年初至今 NAV 期间贡献，后者是当前持仓/清仓累计成本口径盈亏，可能包含期初已持仓浮盈、现金/汇率/融资影响和估值时点差。

#### 已修复问题

- `_bridge_quality` 拆分为 blocking 与 advisory：真实 NAV 残差或入金闭环失败仍进入 `blocking_checks`，累计持仓盈亏与期间 NAV 贡献的口径差进入 `advisory_checks`。
- equity bridge API 新增 `quality.nav_reconciled`、`quality.advisory_checks`、`quality.attribution_comparison_basis`、`investment.period_attribution_available`、`investment.comparison_basis` 和 `investment.comparison_note`。
- 更多页的 NAV 增值解释读取 `bridgePayload.quality`，总账闭合但归因未期间化时显示“总账闭合 · 归因口径差”，并展示 advisory 原因。

#### 运行态结论

- 当前 API 返回：`quality.status = warning`，`nav_reconciled = true`，`blocking_checks = []`。
- 保留显式提醒：`advisory_checks[0].gap_cny = -63,366.72`，说明当前收益归因仍是累计口径，不等同于年初至今 NAV 期间贡献。
- 资产数据没有被修改；本轮只调整后端质量状态语义和前端解释状态。

#### 验证记录

- `python -m pytest tests/test_portfolio_equity_bridge.py tests/test_return_attribution.py tests/test_portfolio_state_contract.py tests/test_valuation_nav_service.py -q`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `node --check frontend\public\assets\js\nav_growth_explanation.js`
- `node --check frontend\public\assets\js\more.js`
- `python -m compileall -q backend\api\services`
- `docker compose up -d --build pams-api pams-web`
- 运行态 smoke：`/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY` 返回 `quality.status = warning`、`nav_reconciled = true`、`blocking_checks = []`。

#### 下一轮建议

1. 实现真正的期间化标的归因：按账户计算 `ending_account_nav - beginning_account_nav - net_funding`，并单列证券持仓、现金/汇率、融资成本和估值校准。
2. IBKR 优先拆分现金/融资/FX 对 NAV 的影响，避免用证券毛盈亏解释净清算价值。
3. 继续补齐 `alipay`、`gf_7443`、`gf_9711` 账户余额或显式排除规则，降低 PortfolioState 数据质量 warning。

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
