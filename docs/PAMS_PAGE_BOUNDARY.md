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

## 统一外壳与内容页

`app.html` 持有唯一的“投资、收支、更多”主导航和居中内容视口，上限 430px，小屏铺满。默认展示投资；六个业务 HTML 是内容文档，移除各自的主导航。为保留既有控制器、DOM ID、导入编辑及 API 行为，第一阶段在同源 iframe 内加载内容文档，不将不同业务脚本合并到同一个全局 document。此实现不是已完成的业务模块组件化。

Nginx 将直接访问业务 HTML 的旧路径转入外壳，保留查询参数和片段；外壳内部通过 `content=1` 加载内容页。该参数仅区分页面呈现，不是授权/权限开关。`app_shell.js` 负责主导航、历史和同源页面跳转，不读取或计算金融数据；旧式程序跳转进入内层外壳时通过校验来源窗口及 origin 的消息回到外层。

投资的持仓/分析/交易/风险/配置与收支的总览/流水/账户属于相同的二级导航层，放在内容首部。收支旧页头的“流水”、副标题、口径说明和“资金流转匹配”快捷链接已移除；账户页的转账匹配功能保留。更多页移除顶部重复标题，保留主导航标签和全部资产工具入口。

内容切换仍加载对应页面，只有外层导航实例常驻，不承诺切换后保留所有页面局部筛选或滚动状态。导入的原生离页保护继续由内容页处理；真实移动设备、输入法与浏览器原生离页对话框须在上线前另行验收。技术检查不替代用户验收，未授权不合并部署。
