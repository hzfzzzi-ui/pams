# PAMS 面试 Demo Guide

> Vibe Coding demo 主题：用 AI 快速推进一个真实个人资产系统，但用事实表、统一估值、回归测试和前端契约把复杂口径固定住。

## 30 秒介绍

PAMS 是一个本地化个人资产管理系统，用来统一管理银行卡、现金账户、券商账户、多市场投资持仓、现金流水、投资交易、收益归因和风险暴露。

它不是一个简单 dashboard。核心问题是：真实个人资产分散在多个账户和市场里，入金、出金、现金划转、持仓收益、汇率、融资负债和基金穿透很容易混在一起。PAMS 的做法是先建立业务事实表，再由服务层统一估值、收益、风险和配置口径，前端只展示 API 结果。

一句话包装：

```text
我用 Vibe Coding 快速迭代真实个人资产系统，但把 AI 生成的每个展示和口径都收束到事实表、服务层和回归测试里。
```

## 为什么适合作为 Vibe Coding Demo

| 维度 | 展示点 |
|---|---|
| 真实复杂度 | 多账户、多币种、多市场、现金流水、投资交易、融资负债、基金穿透 |
| AI 协作方式 | 用 Codex 快速实现页面、接口、事实表修复、口径修正和测试补强 |
| 工程约束 | 数据分层、事实表、统一服务层、静态前端契约测试、Docker 运行态验证 |
| 产品完整度 | 移动端 dark app 风格，底部主导航为投资、收支、更多 |
| 可验证证据 | pytest 回归、前端静态契约、schema 文件、Docker Compose |

## 演示路线

### 3 分钟版本

1. 打开 `README.md`，先讲 PAMS 不是记账页面，而是个人资产事实系统。
2. 打开 `frontend/public/investments.html`，讲投资页如何展示持仓、分析、交易、风险和配置。
3. 打开 `backend/api/services/portfolio_valuation_state.py`，讲组合顶层价值只认统一估值状态。
4. 打开 `tests/test_portfolio_valuation_state.py`，展示净清算价值和发布状态有测试约束。
5. 打开 `tests/test_frontend_static_contract.py`，展示 UI 不允许回退到旧导航、旧入口和旧文案。

### 8 分钟版本

1. 讲数据分层：原始导入表 -> 事实表 -> 当前状态/资产底表 -> 服务层 -> API -> 前端。
2. 讲现金流事实：`pams_cashflow_fact` 区分生活收支、内部划转、投资入金和现金收益。
3. 讲投资交易事实：`pams_investment_trade_fact` 区分买卖、分红、费用、换汇、入金和出金。
4. 讲统一估值：`pams_portfolio_valuation_state` 避免投资页、分析页、风险页各算各的。
5. 讲收益口径：入金不算收益，IBKR 使用净清算价值，美股收益按北京时间每日窗口对齐。
6. 讲前端收敛：移动端只保留投资、收支、更多，低频入口收进更多页。
7. 讲回归测试：事实表服务、估值状态、资产主数据、持仓穿透、风险状态和前端静态契约。

## 核心目录

| 路径 | 用途 | 面试讲法 |
|---|---|---|
| `backend/api/main.py` | FastAPI 应用入口 | 聚合投资、收支、账户、风险、同步、分析等 API |
| `backend/api/endpoints/` | HTTP endpoint | endpoint 只做参数和响应封装 |
| `backend/api/services/` | 业务服务层 | 分类、估值、收益、风险和穿透逻辑集中在这里 |
| `database/postgres/schema/` | 数据库 schema 和回填脚本 | 事实表和状态表的可追溯演进 |
| `frontend/public/` | 移动端 Web UI | 面向手机使用的 dark app 静态前端 |
| `config/` | 口径配置 | 资产、分类、穿透和对账配置入口 |
| `tests/` | 回归测试 | 用测试固定数据口径、接口口径和前端边界 |
| `compose.yaml` | 本地运行编排 | Postgres、API、Web、Caddy、scheduler 等服务 |

## 重点演示文件

| 主题 | 文件 |
|---|---|
| 项目总览 | `README.md` |
| 运行编排 | `compose.yaml` |
| 统一估值状态 | `backend/api/services/portfolio_valuation_state.py` |
| 投资收益服务 | `backend/api/services/investment_returns.py` |
| IBKR 收益重建 | `backend/api/services/investment_return_ibkr.py` |
| 现金流事实服务 | `backend/api/services/cashflow_fact.py` |
| 投资交易事实服务 | `backend/api/services/investment_trade_fact.py` |
| 持仓穿透与分类 | `backend/api/services/exposure_engine.py` |
| 风险状态 | `backend/api/services/risk_state.py` |
| 投资页前端 | `frontend/public/investments.html` |
| 前端共享交互 | `frontend/public/assets/js/ui.js` |
| 交易页展示 | `frontend/public/assets/js/transactions.js` |
| 前端契约测试 | `tests/test_frontend_static_contract.py` |
| 估值状态测试 | `tests/test_portfolio_valuation_state.py` |

## 我们的迭代历史可以怎么讲

这套系统的 Vibe Coding 价值不在“一次生成”，而在多轮真实修正：

- 从旧 dashboard 收敛成移动端 dark app，固定底部导航为投资、收支、更多。
- 把投资流水和现金流水拆成独立事实表，避免入金、划转被误算成收益。
- 统一 NAV / 净清算价值口径，修复页面之间的估值不一致。
- 修复 IBKR 杠杆账户、A 股账户、基金预估、银行对账等真实边界问题。
- 给前端结构、枚举文案、事实表服务和收益口径补回归测试。

面试表达：

```text
AI 帮我提高了实现吞吐，但我没有让页面直接堆逻辑。每次发现口径漂移，都会把逻辑下沉到服务层或事实表，并补一条回归测试。
```

## 本地验证命令

文档改动通常不需要重建容器。涉及页面或接口时可以按范围验证：

```powershell
python -m pytest tests/test_frontend_static_contract.py -q
python -m pytest tests/test_portfolio_valuation_state.py tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q
python -m pytest tests/test_asset_master_contract.py tests/test_exposure_engine.py tests/test_risk_state_service.py -q
node --check frontend\public\assets\js\transactions.js
```

涉及运行态时：

```powershell
docker compose up -d --build pams-web
docker compose up -d --build pams-api
docker ps --filter "name=pams-web" --filter "name=pams-api"
```

## Demo 边界

- 这是个人资产管理和研究展示系统，不是公开投顾产品。
- 仓库不应包含真实密钥、token 或账户登录凭据。
- 前端只消费 API，不在多个 JS 组件里重复实现行业、主题、地区、币种或收益口径。
- 任何收益、风险、配置相关变化都必须回到事实表、服务层和测试约束。
