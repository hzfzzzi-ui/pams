# PAMS 个人资产管理系统

PAMS 管理多账户资产、收支、投资交易、收益归因、持仓穿透和风险暴露。业务事实由后端统一计算，移动端 Web 展示结果。策略研究、回测、观察池及外部策略信号不属于 PAMS。

## 项目结构

下列结构描述当前检出的源码。代码合并、生产镜像、数据库结构和用户验收分别核对，不能从文件树推断已上线。

```text
pams/
├─ backend/
│  ├─ api/
│  │  ├─ main.py                 API 装配及兼容接口
│  │  ├─ endpoints/              HTTP 参数、响应和业务编排
│  │  ├─ domain/                 业务规则与读取服务
│  │  │  ├─ accounts/            账户余额和账户视图
│  │  │  ├─ ledger/              收支事实、流水与资金流转
│  │  │  ├─ investments/         交易、持仓、估值、收益和 NAV
│  │  │  ├─ assets/              资产主数据、分类与基金穿透
│  │  │  ├─ market/              行情、净值、汇率和交易日历
│  │  │  ├─ risk/                风险状态、目标配置与披露
│  │  │  │  └─ core/             风险规则、因子与集中度计算
│  │  │  ├─ imports/             资料解析、预检、暂存及发布
│  │  │  ├─ brokers/ibkr/        券商报告、日结、费用与对账
│  │  │  └─ operations/          同步健康状态
│  │  └─ collectors/             外部数据源连接和原文解析
│  └─ scheduler/                 定时刷新、确认与检查点
├─ frontend/
│  ├─ public/
│  │  ├─ investments.html        投资：持仓、交易、分析、风控
│  │  ├─ ledger.html             收支与资金流转
│  │  ├─ more.html               账户、数据源和导入入口
│  │  ├─ accounts.html           账户及银行对账
│  │  ├─ data_sources.html       同步状态与数据质量
│  │  ├─ account_import.html     资料导入及核对
│  │  └─ assets/
│  │     ├─ js/                  页面交互和 API 绑定
│  │     ├─ css/                 基础样式及生成的手机样式
│  │     └─ icons/               应用图标
│  └─ tools/                     手机样式构建工具
├─ tests/
│  ├─ accounts/ ledger/ assets/   对应业务测试
│  ├─ investments/ market/ risk/  估值、收益、行情与风险回归
│  ├─ imports/ brokers/ibkr/     导入、券商及数据库事务测试
│  ├─ scheduler/ operations/     调度与发布身份检查
│  ├─ frontend/                  页面契约、JS 和交互回归
│  └─ support/                   隔离数据库与演练辅助
├─ database/postgres/schema/     结构定义与历史 SQL；不可无条件全量执行
├─ config/                       应用本位币、账户、收益、分类、风控
├─ gateway/                      Caddy 网关配置
├─ scripts/                      日结、来源核验与发布身份工具
├─ docs/                         当前业务契约和开发规则
├─ .github/workflows/            CI：测试、静态检查和候选镜像
├─ compose.yaml                  默认服务与挂载声明
└─ AGENTS.md                     Agent 工作入口
```

业务方向为：原始资料 → 事实表 → 领域服务 → API → 页面。调度器通过 API 驱动刷新。测试与应用代码分离，数据库集成测试只连接显式隔离环境。

## 文档与运行入口

- [文档入口](docs/README.md)：四个业务主题、页面规则和开发流程。
- [配置来源](docs/CONFIGURATION.md)：YAML、环境、Compose、挂载与依赖。
- [空库初始化与清理候选 PR #90](https://github.com/hzfzzzi-ui/pams/pull/90)：合并及执行状态分别核对；生产迁移、删表仍需独立授权。
- [Agent 规则](AGENTS.md)：范围、验证、提交和部署边界。
- [GitHub PR](https://github.com/hzfzzzi-ui/pams/pulls) 与 [CI](https://github.com/hzfzzzi-ui/pams/actions)：开发进度和验证；部署记录归对应功能 PR 评论。

主入口为 http://pams.local，底部导航为投资、收支、更多。web 使用构建镜像，本地改文件不会直接更新生产。候选验证、生产部署和数据库操作分别遵守用户授权。
