# pams-web

PAMS 前端是本地个人资产系统的移动端 Web UI，由 nginx 提供静态页面，并通过 `/api/` 代理访问 FastAPI。

## 主入口

- `investments.html`：默认首页。承载投资持仓、收益分析、交易流水、风险子页和目标配置。
- `ledger.html`：现金流与收支账本。承载收入支出、资金流转、待确认流水和账户异常摘要。
- `more.html`：低频入口聚合。承载数据源健康、账户核准、账单导入、资产底表入口、研究看板和系统信息。

## 更多页子入口

- `accounts.html`：账户核准、余额矩阵、内部转账匹配和账单导入。
- `data_sources.html`：同步中心、数据质量、数据源状态和修复提示。
- `research.html`：观察池、IPS 状态、策略候选和决策记录。

## 已收敛入口

- `index.html` 已删除，运行态重定向到 `investments.html`。
- `risk.html` 已删除，完整风险内容收敛到 `investments.html#portfolio-risk-disclosure`。
- 旧入口和已下线页面通过 nginx 返回 404 或跳转到新的主入口，避免形成隐藏页面。

## 边界

- 前端只做展示和交互。
- 财务口径、估值、风险、核准状态应来自 API。
- 投资页风险子页是风险展示主入口；不要再新增独立风险页。
- 新页面改动前先检查 `docs/PAMS_PAGE_BOUNDARY.md` 和 `AGENTS.md`。
