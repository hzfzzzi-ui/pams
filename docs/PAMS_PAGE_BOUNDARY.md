# PAMS 页面与接口边界

本文定义当前页面与 API 的职责，实际能力开关及运行版本按配置与对应发布记录核对。

| 页面 | 职责 | 主要模块／接口来源 |
| --- | --- | --- |
| investments.html | 默认首页；持仓、交易、分析、风险、目标配置 | holdings、holdings_categories、transactions、lookthrough、portfolio_risk、portfolio_config；由对应 API 服务给出业务结果 |
| ledger.html | 收支、资金流转、待确认与异常摘要 | ledger_cashflow；cashflow fact 与 ledger 服务 |
| more.html | 低频功能入口 | more；账户、数据源及导入子入口 |
| accounts.html | 账户核准、余额、银行账单与划转匹配 | bank_reconcile；accounts、bank-reconcile 服务 |
| data_sources.html | 同步状态、覆盖与数据质量 | data_sources；sync center |
| account_import.html | 导入暂存、核对、预检和显式发布入口 | account_import；访问、schema、预检和发布开关分别生效 |

页面存在不代表权限已开启、事实可写或业务已验收。开发阶段见 [GitHub PR](https://github.com/hzfzzzi-ui/pams/pulls)，实际能力核对对应发布记录/配置，导入细节见 [契约](IMPORT_AND_FACTS.md)。

## 导航和兼容

- 底部主导航为投资、收支、更多，风险主入口在投资页内。
- index.html 重定向 investments.html；risk.html 重定向投资风险子页。
- 旧 holdings、transactions、cashflow、backtest、strategy、watchlist 独立 HTML 入口在构建 nginx 配置中返回 404；研究页面及相关 JS 已移除。
- nginx 配置由 frontend/Dockerfile 生成，是构建配置的唯一来源。
- 生成的手机样式与基础样式的选择见 [前端设计规则](PAMS_FRONTEND_DESIGN_SYSTEM.md)。

## 主要 API


| API | 功能 |
| --- | --- |
| `GET /summary2/latest` | 投资总览，顶层组合价值使用已发布估值状态 |
| `GET /api/portfolio/valuation-state` | 查询当前已发布估值状态 |
| `POST /api/portfolio/valuation-state/publish` | 发布或重算组合估值状态 |
| `GET /holdings/latest` | 查询最新持仓快照 |
| `GET /holdings/by-category/{category}` | 按分类查询持仓 |
| `GET /investment/return-series` | 查询组合收益曲线 |
| `GET /investment/section-return-series` | 查询资产桶收益曲线 |
| `GET /nav/weekly?latest=true` | 查询 NAV 序列，并让终点对齐最新估值状态 |
| `GET /api/portfolio/risk-disclosure` | 查询风险披露页数据 |
| `GET /api/portfolio/config` | 查询配置页目标规则和偏离情况 |
| `GET /api/investment-transactions` | 查询投资交易事实列表 |
| `GET /api/accounts` | 查询账户视图 |
| `GET /api/ledger/workbench` | 查询流水工作台数据 |
| `POST /api/prices/*` | 刷新行情、基金净值或价格数据 |

接口前缀、查询参数和响应以当前路由及对应业务契约为准。

## 展示责任

- 前端展示及交互消费统一业务结果，不能新增临时分类或补差金额。
- 持仓、账户净值、区间收益、NAV 与归因分别按 [数据契约](DATA_AND_RETURNS.md) 追溯。
- 修改页面前核对调用方、模板和关键状态，并按 [工作流程](DEVELOPMENT_WORKFLOW.md) 选择验证范围。

## 前端整改与并行后端重构的边界

前端整改负责页面、组件、样式、交互和前端代码组织，保留投资、收支、更多三项主导航及现有资产功能。后端和数据库的“主表、来源表、辅助表”重构由另一任务负责；前端通过 API 消费业务结果，不依赖物理表名、内部目录或数据库迁移顺序。

- 当前 API 是兼容基准。接口不可用时，候选页面可使用明确标注的固定测试数据；不能把测试数据放进生产回退路径，不能用它证明线上金额正确。
- 新增或修改接口前，先在对应 PR 列出字段、类型、单位、日期、账户范围、空值和状态语义，再由负责后端的任务确认；前端不自行计算收益、NAV、分类、风控或汇兑补差。
- 现有前端公式属于待梳理的历史依赖，不构成继续扩展算法的许可。替换前须明确服务端字段和固定输入对照，不能为了消除公式先删掉资产功能或将未知值补零。
- 使用独立 `codex/` 分支，显式暂存本轮文件，通过中文 PR 和最终 head CI 同步。原工作区的未提交、未跟踪和删除状态均保留；不同工作区的规则只合并相关段落，不整文件覆盖。
- PR、合并、部署、财务核准和用户验收分别记录。文档梳理不触发生产取数、刷新、写入、构建或部署。

## 页面与交互保留清单

以下对应当前六页源码结构；旧检出可能仍有研究页，不能把旧页面清单直接恢复到已退役的主线。保留要求不表示这些页面已经通过用户验收。

| 页面 | 页面内模块与入口 | 整改时必须保留的交互 |
| --- | --- | --- |
| 投资 | 组合摘要；持仓、分析、交易、风险、配置五个 tab | hash 深链与 tab 对应；分类筛选、持仓详情、已平仓查看；区间曲线、NAV、收益/交易归因与资产底表；交易筛选、默认十条及展开更多；风险详情和配置规则 |
| 收支 | 总览、流水、账户；趋势、分类排行、资金流转 | 期间切换、月/周趋势、收支切换、筛选、流水详情与既有编辑入口；编辑失败保留输入，不自动提交 |
| 更多 | 待处理、导入资料、账户管理；高级工具 | 待处理区分正常、异常、不可用；高级工具默认折叠；NAV 增值解释按需加载；资产底表深链和系统信息 |
| 账户 | 总览、余额、转账、导入 | 核准清单、余额矩阵、内部转账匹配、待确认流水、导入批次与原有下钻；写操作只能由显式操作触发 |
| 数据诊断 | 同步中心、覆盖与数据质量；`view=issues` 待处理视图 | 问题与处理建议、状态详情、返回更多；HTTP 成功不能替代业务成功，刷新不等于核准 |
| 导入资料 | 交易/持仓/账单；文件/截图/手动；逐图核对；配置确认 | 顶部返回及左缘返回、原件查看、编辑草稿、映射重解析、预检与发布门禁；返回不自动确认或发布 |

导入编辑权限、十进制精度、版本冲突、来源确认、余额独立确认和发布条件继续以 [导入契约](IMPORT_AND_FACTS.md) 为准。加载中、空数据、缺字段、过期、预估、确认、阻断、服务不可用须分开呈现；不能把请求失败显示成“无异常”或把缺金额显示成零。

## 前端代码分工

路径均相对 `frontend/public/assets/js/`；这些是现有模块，不是框架组件目录。

| 模块 | 当前职责 | 整理边界 |
| --- | --- | --- |
| `holdings.js`、`account_cash.js` | 持仓列表、移动端详情、现金行 | 保留普通证券、基金预估/确认、现金/融资、平仓、多账户模板；数据汇总与展示拆开审阅 |
| `holdings_categories.js` | 分类明细、组合/分类曲线、NAV、账户收益展示 | 按请求、页面状态、曲线渲染、详情拆分；金融计算先登记接口依赖 |
| `lookthrough.js` | 穿透维度、收益归因、资产底表及图形布局 | 保留行业/主题/地区/币种等独立维度；图形布局不产生业务分类或金额 |
| `transactions.js` | 交易列表、筛选、复盘标注和归因 | 保留服务端方向/金额语义、折叠入口及输入状态，不恢复独立直写流程 |
| `portfolio_risk.js`、`portfolio_config.js` | 风险披露、目标规则与偏离展示 | 消费服务端风险结论、分母及配置比例，不新增阈值判断或再平衡算法 |
| `ledger_cashflow.js`、`bank_reconcile.js` | 收支工作台、账户核准和划转交互 | 不把投资入出金并入生活收支，不在浏览器认定匹配或余额核准 |
| `more.js`、`data_sources.js`、`sync_center.js`、`nav_growth_explanation.js` | 入口、问题和运行诊断、审计解释 | 诊断只解释已有结果，不成为资产或 NAV 的另一来源 |
| `account_import.js` | 独立导入客户端、编辑器及步骤状态 | 保持同源请求、来源只读限制和预检 token；不并入可缓存的普通 GET/POST 流程 |
| `api.js` | HTTP、超时、缓存、请求去重及部分格式化 | 传输与展示辅助可逐步分离；缓存键、时效、失败和写后失效行为须保留 |
| `ui.js`、`utils.js` | 导航、弹层、数据状态、预加载、格式化与图表辅助 | 提取共享组件先核对调用方；`utils.js` 中的历史收益公式不能当通用格式化复用 |
| `investment_startup.js`、`investment_requests.js` | 投资首屏启动顺序、共享摘要请求 | 保留首屏依赖和并发请求共享；不能把共享请求变成无限期结果缓存 |

目标依赖顺序为“页面入口 → 页面状态/渲染 → 共享 UI 与只做字段适配的数据访问 → 现有 API”。目录拆分逐模块实施，不一次改名所有入口、不引入框架迁移、不在适配层藏入金融算法。模块入口与 HTML 版本参数保持可追溯。

## 现有 API 消费面

这是源码调用面，尚不能代替运行态接口核验；字段只列代表项，不是完整 schema。

| 页面用途 | 现有读取接口 | 需保留的业务语义 |
| --- | --- | --- |
| 投资摘要/估值 | `/summary2/latest`、`/api/portfolio/valuation-state` | 组合净值、资产/负债、业务日期、质量与归属范围；不拿证券市值代替 NAV |
| 持仓与账户 | `/holdings/latest`、`/holdings/by-category/{category}`、`/api/accounts/overview` | 账户、标的、币种、数量、市值、成本、盈亏及确认日期；原币、CNY、gross 和归属后值不可混用 |
| 账户收益 | `/api/ibkr/performance`、`/api/account-performance/tonghuashun`、`/api/investment-returns/reported` | `return_ratio` 的具体口径及资本基数，账户收益不能充作单个证券收益 |
| 曲线/NAV | `/investment/return-series`、`/investment/section-return-series`、`/nav/weekly` | 区间、序列日期、收益口径、有效终点及缺口；NAV 变化不直接等于投资收益 |
| 穿透/归因 | `/api/analysis/lookthrough`、`/api/analysis/return-attribution` | 维度、覆盖、未解释部分、日汇兑解释；主题可重叠、不要求加总 100% |
| 交易 | `/api/investment-transactions` 及其 options、campaigns、attribution 子接口 | 事实身份、方向、币种、金额/份数/税费、复盘标注与归因结果 |
| 风险/配置 | `/api/portfolio/risk-disclosure`、`/api/portfolio/config` | 服务端状态、规则结果和配置偏离；已退役的旧风险汇总接口不重新接入 |
| 收支/账户核准 | `/api/ledger/workbench`、`/api/accounts/overview`、`/api/bank-reconcile/*` | 收支、划转、账户余额、核准状态分别表达；编辑/核准/匹配等写请求不能用于只读底稿 |
| 更多/诊断 | `/api/portfolio/data-quality`、`/api/sync/center` | 服务端质量汇总、覆盖及同步结果，不从金额或页面时钟推断业务成功 |
| 导入 | `/api/account-import/*` | `schema_ready`、`publish_enabled`、revision、preview_token、can_publish、记录状态和编辑权限分别生效 |

### 历史前端计算与接口协作项

以下函数在当前源码中存在。这里只登记依赖，不修改计算，也不宣称金额已核准。后端已有等价字段时优先复用；新增字段须由后端任务确认后实现。

| 当前依赖 | 需要后端明确的结果 | 前端替换条件 |
| --- | --- | --- |
| `utils.pnlPct`、两处 `displayPnlPct` 从盈亏/成本求比例 | 单持仓收益率、分母定义、有效性 | 确认与账户 `return_ratio` 的区别；无定义显示 `--`，不能补成 0% |
| `holdings.currentFloatPnl`、`realizedPnl` 的本地差额回退 | 浮动盈亏、已实现盈亏及各自范围 | 逐一对应原币/CNY、gross/归属后值，不能以总盈亏减市值成本差额制造已实现值 |
| `holdings.aggregateMobileHoldingRows` 跨账户汇总、单价和收益状态推导 | 合并分组身份、成员列表、汇总值及状态 | 保留多账户下钻；同币种、同日期、同口径的合并规则由服务端负责 |
| `holdingGroupLabel` 等文字分类回退 | 展示分组代码/标签及独立分类维度 | 不从名称、代码前缀推断资产身份、行业、主题或地区 |
| `holdings_categories` 的曲线事件金额/区间摘要推导 | 同一序列范围的事件金额、摘要和日期 | 图表只做坐标、排版、筛选；不跨日期补差以对齐另一口径 |
| `portfolio_risk` 的已有状态标签计数回退 | 服务端已有的检查项及计数 | 区分计数展示与风险判定；不把计数回退扩展为风控评分 |

### 接口变更提案的字段模板

下列 `display.*`、`period.*` 等名称是**待后端确认的提案**，不是现有响应字段，也不要求另开新 endpoint。前端不能提前依赖它们；应先验证已有字段能否满足需求。

| 提案字段 | 类型/单位 | 语义与缺失处理 |
| --- | --- | --- |
| `display.holding_return_ratio` | decimal 字符串或 null；比例 0.12 表示 12% | 同一持仓范围的收益率；配套 `return_basis` 说明分母；无定义为 null |
| `display.unrealized_pnl_cny`、`display.realized_pnl_cny` | 有符号 decimal 字符串或 null；CNY | 分别为浮动与已实现盈亏，须声明区间及归属；不能互相相减补全 |
| `display.group_id`、`group_label`、`member_ids` | 字符串、字符串、字符串数组 | 服务端合并身份与成员，不以页面名称拼接充当资产 ID；汇总字段另逐项列出 |
| `display.group_status`、`quality_flags` | 已约定枚举、原因数组 | 合并日期/币种/完整性状态；未知保持不可核准，不默认为 confirmed |
| `period.start_date`、`end_date`、`basis` | ISO 业务日期、口径代码 | 区间端点及 NAV/收益的不同语义；不得用响应生成时间代替业务日期 |
| `period.pnl_cny`、`period.nav_change_cny` | 有符号 decimal 字符串或 null；CNY | 收益与净值变化分列，由服务端桥接现金流；缺一不由另一个推算 |

每个提案还须写清：现有 endpoint/JSON 路径、请求参数、账户和资产身份、币种与汇率日、gross/归属后范围、业务日期/更新时间、合法枚举、null 与真实零、精度、来源/质量、向后兼容策略，以及正常/缺失/过期/多账户的固定响应示例。前后端的对应 PR 相互引用；后端表结构变化不直接变成前端取表逻辑。
