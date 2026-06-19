# PAMS

PAMS 是一个面向个人资产管理的本地化系统，核心目标是把多账户、多市场、多币种的资产、交易、收益和风险口径统一到同一套事实表和 API 上，避免页面各自重复计算导致的口径漂移。

当前应用以移动端 dark app 体验为主，主要入口包括：投资、收支、更多，以及投资页内的持仓、分析、交易、风险、配置五个子页面。

## 核心功能

### 投资持仓

- 汇总 A 股、港股、美股、韩国股票、场外基金、贵金属、现金和非上市权益。
- 持仓页以 `pams_holdings_snapshot` 和投资交易事实表为基础展示当前持仓。
- 顶层组合价值优先使用已发布估值状态，避免美股杠杆市值、现金和负债重复计算。
- 美股账户按净清算价值口径纳入组合，即：持仓市值加券商现金，再扣除账户负债。

### 分析页

- 展示组合收益率、区间收益、NAV 变化和 Top 贡献。
- 收益曲线按统一交易日期对齐亚太市场和美股市场，不把美东日期单独漂移到另一自然日。
- NAV 图使用完整资产口径，包含银行卡、现金账户、券商现金、负债和 ESOP 等资产。
- 区间终点使用已发布估值状态，保证分析页与持仓页的组合价值一致。

### 交易页

- 交易事实表为 `pams_investment_trade_fact`。
- 支持买入、卖出、入金、出金、分红、费用、换汇等投资流水。
- 页面展示使用中文业务方向，不直接暴露 `deposit`、`withdraw` 等原始枚举。
- 交易页负责记录事实，收益、风险和配置页面不直接重算交易流水。

### 风险页

- 风险页消费统一的风险状态和持仓穿透结果。
- 用七类检查项披露组合风险：杠杆与融资、集中度、市场方向、行业主题、币种、流动性、数据质量。
- 风险项展示检查结果、当前值、阈值和调整方向。
- 风险披露只作为约束和提醒，不直接改变组合配置目标。

### 配置页

- 配置页维护目标配置规则，并与当前真实持仓做偏离对比。
- 结构按三层展开：大类配置、小类配置、具体标的。
- 目标配置用于指导再平衡，不直接作为持仓事实来源。
- 负债单独展示，不并入现金缓冲池。

### 收支和账户

- 收支事实表为 `pams_cashflow_fact`。
- 银行卡、现金账户、投资入金和内部划转按 cashflow fact 管理。
- 投资账户入金需要在现金事实表和投资交易事实表之间建立对应关系，避免现金和投资资产双算。

## 数据口径

系统的核心链路是：

```text
原始导入表
  -> 业务事实表
  -> 当前状态 / 资产底表 / 穿透引擎
  -> 页面 API
  -> 前端展示
```

页面只消费 API 结果，不在多个前端组件里重复实现分类、收益或估值逻辑。

### 已发布估值状态

当前组合总值以 `pams_portfolio_valuation_state` 为统一发布口径。它用于解决不同页面对组合价值、净清算价值、现金和负债重复计算的问题。

核心字段：

- `assets_cny`：总资产，包含持仓市值、银行卡、现金账户和券商现金。
- `liabilities_cny`：总负债，包含融资、保证金负债等。
- `net_liquidation_value_cny`：净清算价值。
- `status`：估值状态，例如 `complete`、`partial`。
- `quality_flags`：缺失账户、估值来源或数据质量提示。

统一公式：

```text
净清算价值 = 总资产 - 总负债
```

持仓页、分析页、风险页、配置页应共享同一个已发布估值状态作为组合顶层价值。

## 主要 API

| API | 说明 |
| --- | --- |
| `GET /summary2/latest` | 投资总览，顶层组合价值使用已发布估值状态 |
| `GET /api/portfolio/valuation-state` | 查询当前已发布估值状态 |
| `POST /api/portfolio/valuation-state/publish` | 发布或重算组合估值状态 |
| `GET /nav/weekly?latest=true` | NAV 序列，终点对齐已发布估值状态 |
| `GET /investment/return-series` | 收益曲线，终点对齐已发布估值状态 |
| `GET /api/portfolio/risk-disclosure` | 风险披露页数据 |
| `GET /api/portfolio/config` | 配置页目标规则和偏离数据 |
| `GET /api/investment-transactions` | 投资交易事实列表 |

## 重要目录

```text
backend/api/main.py                         FastAPI 主入口和页面 API
backend/api/endpoints/                      分业务路由
backend/api/services/                       业务服务层
database/postgres/schema/                   PostgreSQL 结构迁移
frontend/public/                            前端静态页面
frontend/public/assets/js/                  前端 ES modules
tests/                                      回归测试
```

估值和收益口径相关模块：

```text
backend/api/services/portfolio_valuation_state.py
backend/api/services/portfolio_summary.py
backend/api/services/valuation_nav.py
backend/api/services/investment_returns.py
backend/api/services/return_attribution.py
backend/api/services/risk_state.py
```

## 本地运行

```powershell
docker compose up -d --build pams-api pams-web
```

常用地址：

- 前端：`http://pams.local`
- 本地前端端口：`http://localhost:8080`
- API 文档：`http://localhost:8090/docs`

当前 `pams-web` 是构建镜像，不挂载本地 `frontend/public`。修改前端文件后必须重建 `pams-web`，否则浏览器不会看到新版本。

## 回归验证

核心验证命令：

```powershell
python -m pytest tests/test_valuation_nav_service.py tests/test_return_attribution.py tests/test_risk_state_service.py tests/test_portfolio_state_contract.py tests/test_portfolio_valuation_state.py tests/test_portfolio_valuation_linkage.py tests/test_holding_return_snapshot.py tests/test_investment_returns_service.py tests/test_holdings_view_service.py tests/test_portfolio_summary_service.py tests/test_frontend_static_contract.py tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q
node --check frontend\public\assets\js\api.js
node --check frontend\public\assets\js\dashboard.js
node --check frontend\public\assets\js\holdings_categories.js
```

涉及运行态时额外确认：

```powershell
docker compose up -d --build pams-api pams-web
docker ps --filter "name=pams-web" --filter "name=pams-api"
```

## 开发约束

- 前端只展示 API 结果，不在组件里重复维护行业、主题、地区、币种、资产类型或收益分类。
- 投资持仓页以持仓快照和交易 fact 为准。
- 收支页以 cashflow fact 为准。
- 交易页以 investment trade fact 为准。
- 分析页和风控页消费统一服务层结果，不直接绕过服务层查表重算。
- 新增字段需要保持向后兼容。
- 不在 endpoint 中写运行时 DDL，表结构变更应放在 `database/postgres/schema/`。
