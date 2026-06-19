# PAMS Agent 工作守则

本文件是本仓库内 AI agent 的首读规则。目标是减少返工、避免前端样式回退、避免数据口径漂移，并让每次修正都可追溯。

## 0. 开工前先确认

每次改动前先快速判断并在心里确认：

1. 本次是数据修正、后端接口、前端展示、架构重构、测试补强，还是运行部署。
2. 是否会触碰投资页、收支页、风控页的前端结构或样式。
3. 是否会改变事实表、分类口径、收益口径或风控口径。
4. 是否需要同步重建 `pams-web` 或 `pams-api`。
5. 是否需要新增回归测试防止同类问题复发。

若用户明确说“禁止动前端”或“固定当前前端”，只能改数据、后端服务、接口字段绑定或必要文案映射，不得改视觉设计、布局、导航结构。

## 1. 变更分级与 GitHub 同步

每次代码或文档改动都必须先按影响范围分级，并在结论里说明本轮属于哪一级。分级用于决定验证范围、提交粒度和 PR 粒度。

变更分级：

- **S 级：低风险小改动**。只改文案、README、注释、字段展示映射、单点样式微调、单个接口的小修小补。验证以静态检查或对应单测为主。
- **M 级：中等改动**。修改一个明确模块的业务逻辑、接口返回、数据口径、前端组件交互、同步任务或一组同源 bug。需要跑对应模块测试，必要时重建容器并做页面验证。
- **L 级：高影响改动**。新增页面、新增功能、事实表 / schema 变化、收益 / NAV / 风险 / 配置口径变化、跨页面联动、架构重构或数据迁移。必须补充或更新回归测试，并做运行态验证。

GitHub 同步规则：

- **所有改动都要同步到 GitHub**。不能只停留在本地工作区，除非用户明确要求暂不提交。
- 每次同步都必须通过 PR，PR 标题和正文必须使用中文，说明改动范围、变更分级、验证结果和剩余风险。
- 新增功能、新增页面、schema 变更、口径变更、跨页面联动等 **L 级改动必须单独对应一条 PR**，不能和无关 bug 或样式调整混在一起。
- bug 修复可以按同一模块、同一根因或同一页面合并成一条 PR；不同根因、不同口径的 bug 不要强行塞进同一 PR。
- 纯文档改动可以单独提交一条文档 PR；如果文档是某个功能变更的必要说明，也可以跟随该功能 PR。
- 工作区存在历史脏状态时，必须显式暂存本轮相关文件，禁止 `git add .` 把无关文件、缓存、报表、Excel 或旧实验结果带进提交。
- PR 合并后要确认 `origin/main` 已更新；若无法合并，要明确说明阻塞原因和下一步。

## 2. 当前稳定前端边界

投资页、收支页、更多页、风控页已经是移动端 dark app 风格。不要因为数据问题重做 UI。

硬规则：

- 底部移动端 nav 只能有：`投资`、`收支`、`更多`。
- 不要恢复旧的顶部 `statusbar` 到收支页和风控页。
- 不要恢复旧的多按钮入口条，例如同步、口径、风控一排大按钮。
- 交易页手动录入和筛选是折叠按钮入口，不要恢复成常驻大表单。
- 交易明细默认显示前 10 单，通过“展开更多”显示全部，不要显示“最近10单”冗余标签。
- 前端组件只展示 API 结果，不能在多个 JS 组件里重复实现行业、主题、地区、币种、资产类型分类。

相关回归测试优先看：

- `tests/test_frontend_static_contract.py`
- `frontend/public/investments.html`
- `frontend/public/assets/js/transactions.js`
- `frontend/public/assets/js/ui.js`
- `frontend/public/assets/css/style.css`

## 3. 运行态注意事项

当前 `compose.yaml` 下的 `pams-web` 是构建镜像，不是挂载本地 `frontend/public`。

因此：

- 改前端静态文件后，仅修改本地文件不会让 `http://pams.local` 立刻生效。
- 需要执行 `docker compose up -d --build pams-web`。
- 改 API Python 代码后，需要执行 `docker compose up -d --build pams-api`。
- 若只是查数据库或改数据，不要无意义重建容器。
- 修改 JS 后要 bump 对应 HTML 中的 `?v=` 版本号，避免浏览器沿用旧 module 缓存。

常用验证：

```powershell
node --check frontend\public\assets\js\transactions.js
python -m pytest tests/test_frontend_static_contract.py -q
python -m pytest tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q
docker ps --filter "name=pams-web" --filter "name=pams-api"
```

## 4. 数据分层必须清楚

不要让页面、临时 YAML、旧分类接口成为新的事实来源。优先按下面链路处理：

```text
原始导入表
  -> 业务事实表
  -> 当前状态 / 资产底表 / 穿透引擎
  -> 页面 API
  -> 前端展示
```

核心事实表：

- `pams_cashflow_fact`：收支、内部划转、投资入金/出金、现金收益。
- `pams_investment_trade_fact`：投资买卖、分红、费用、换汇、投资账户入金/出金。
- `pams_holdings_snapshot`：当前持仓快照。
- `pams_asset_master`、`pams_asset_alias`、`pams_asset_industry_map`、`pams_asset_theme_map`、`pams_fund_underlying_position`：资产标的底表和基金底层。

页面职责：

- 投资持仓页以持仓快照和交易 fact 为准。
- 收支页以 cashflow fact 为准。
- 交易页以 investment trade fact 为准。
- 分析页和风控页应消费 `exposure_engine`、`lookthrough_state`、`return_attribution`、`risk_state` 的统一结果。

## 5. 数据修正规则

手动修正数据库时必须做到：

- 明确 `source_table`、`source_id`、`source_key`、`source_hash`。
- 能重复执行，避免重复插入。
- 保留业务备注和用户确认来源。
- 关联现金流与投资交易时必须写 `cashflow_id`。
- 对中文 SQL 备注使用 UTF-8 管道，避免 PowerShell 到容器后出现 `??`。

PowerShell 写中文到 Postgres 时先设置：

```powershell
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PGCLIENTENCODING='UTF8'
```

示例口径：

- 银行卡转券商是 `pams_cashflow_fact.cashflow_type = investment_funding`。
- 券商到账也应在 `pams_investment_trade_fact` 有一条现金 `deposit`，但不影响持仓数量和成本。
- 已确认的转账匹配应使用 `status = confirmed`，避免被自动重算删除。
- 自动匹配重算不得覆盖 confirmed 备注。

## 6. 投资交易页口径

交易页展示字段应统一为：

- 日期
- 标的名称
- 平台/账户
- 买入、卖出、入金、出金、分红、费用等中文方向
- 金额
- 份数
- 税费
- 币种

`deposit` 不得直接展示为英文，应显示“入金”。同类映射：

- `deposit`、`cash_deposit` -> `入金`
- `withdraw`、`withdrawal`、`cash_withdrawal` -> `出金`
- `transfer_in` -> `转入`
- `transfer_out` -> `转出`

买入/卖出类交易标签不要额外显示 `auto`。

## 7. 持仓穿透与分类口径

行业、主题、资产类型、市场、地区、币种、底层暴露必须分开。严禁混用。

行业大桶白名单：

- 金融地产
- 大消费
- 科技TMT
- 高端制造
- 周期资源
- 医药医疗
- 稳定公用
- 未穿透 / 未映射

不能进入行业图的标签：

- 现金、债券 / 货币基金、ETF / 基金、非上市股权
- Nasdaq-100 constituent、S&P 500 constituent、沪深300成分等指数成分
- AI / 算力、半导体、互联网平台、贵金属与铜等主题
- 人民币资产、美元资产、中国资产、美国资产等币种或地区

主题暴露是多标签，不要求加总 100%。主题主图不要用 donut；AI / 算力子主题只能在点击详情里展开，父主题要去重计算。

## 8. 前端改动最小化

除非用户明确要求 UI 改造，否则：

- 不新增大段 CSS。
- 不改颜色、圆角、阴影、页面密度。
- 不重排页面主结构。
- 只做字段绑定、文案修正、数据质量提示、缺失列表或必要交互修复。

若确实需要改 HTML/JS：

1. 先确认当前页面已有结构。
2. 只改对应模块。
3. 加静态契约测试。
4. bump JS/CSS 版本号。
5. 重建 `pams-web`。
6. 用 in-app browser 做一次可见验证。

## 9. 后端改动原则

- 图表组件不负责业务分类。
- 分类规则集中在服务层或资产底表，不散落在前端。
- 新增字段必须向后兼容。
- 不在 endpoint 里写运行时 DDL；表结构变更应走 `database/postgres/schema`。
- API read adapter 尽量放在 `backend/api/services/`，endpoint 只做参数和响应封装。

优先测试：

- `tests/test_asset_master_contract.py`
- `tests/test_exposure_engine.py`
- `tests/test_lookthrough_state.py`
- `tests/test_return_attribution.py`
- `tests/test_risk_state_service.py`
- `tests/test_cashflow_fact_service.py`
- `tests/test_investment_trade_fact_service.py`

## 10. 页面验证方法

本地页面以 `http://pams.local` 为准。完成前端或接口改动后，至少验证：

- 投资页能打开。
- 交易明细能加载。
- 分析页的持仓穿透 / 收益归因切换正常。
- 风控页不超时。
- 页面没有恢复已删除的旧 UI。
- 关键文案没有英文原始枚举泄漏，例如 `deposit`。

如果用 PowerShell 拉 API，中文输出可能乱码；判断数据库中文是否正确时优先用 `psql` UTF-8 管道或浏览器页面实际渲染。

## 11. 提交前最小验收

根据改动范围选择最小但足够的验证：

```powershell
python -m pytest tests/test_frontend_static_contract.py -q
python -m pytest tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q
python -m pytest tests/test_asset_master_contract.py tests/test_exposure_engine.py -q
node --check frontend\public\assets\js\transactions.js
```

涉及容器运行态时还要确认：

```powershell
docker compose up -d --build pams-web
docker compose up -d --build pams-api
docker ps --filter "name=pams-web" --filter "name=pams-api"
```

不要把无关脏状态、旧实验文件、压缩包、缓存目录混进本次结论。工作区已存在大量历史未跟踪文件，提交或汇总时必须只列本轮相关文件。
