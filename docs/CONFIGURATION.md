# 配置来源与运行输入

配置分为业务 YAML、进程环境、服务部署参数和运行挂载。各模块读取顺序不同，不能假设存在统一的“环境 → YAML → 数据库”覆盖链。

## 业务配置

| 文件 | 使用者 | 读取方式 |
| --- | --- | --- |
| config/accounts.yaml | 账户、流水、组合状态 | PAMS_ACCOUNTS_CONFIG 或 /app/config/accounts.yaml；部分读取者在文件不存在时回退源码 config |
| config/investment_returns.yaml | 投资收益、交易及归因 | PAMS_INVESTMENT_RETURNS_CONFIG 或 /app/config/investment_returns.yaml；不存在时回退源码 config |
| config/lookthrough.yaml | 资产主数据种子、持仓穿透 | PAMS_LOOKTHROUGH_CONFIG 或 /app/config/lookthrough.yaml；读取与显式同步主数据是不同操作 |
| config/risk_limits.yaml | 风险规则、资产本位币 | domain/risk/core/config.py 从源码根目录 config 读取；函数可显式传入路径 |
| config/factor_map.yaml | 风险因子 | 同一风险配置读取器加载，不设通用环境覆盖器 |
| config/risk_concentration.yaml | 集中度计算 | domain/risk/core/concentration.py 从源码根目录 config 读取 |
| bank_reconcile.yaml（私有） | 银行对账 | PAMS_BANK_RECONCILE_CONFIG 或 /app/config/bank_reconcile.yaml；不存在时回退源码 config |

前六份 YAML 是已跟踪的默认业务输入。银行来源文件含私有路径，当前仓库未提供默认文件。配置异常和缺失的处理以各模块为准，变更回退行为须按业务变更验证。

当前源码不再使用策略 policy、扫描参数、回测参数或外部信号配置；资产本位币读取 risk_limits.yaml，不依赖策略数据库配置。运行中的旧镜像及数据库可能仍保留旧配置或消费者，按实际版本、挂载和发布记录核对。

## 环境与能力开关

| 类别 | 主要输入 |
| --- | --- |
| PostgreSQL | PGHOST、PGPORT、PGDATABASE、PGUSER、PGPASSWORD |
| 数据来源 | TUSHARE_TOKEN、IBKR_FLEX_*、PAMS_DATABENTO_API_KEY、券商连接参数 |
| 导入 | PAMS_IMPORT_PUBLISH_ENABLED；访问边界、schema、预览和发布分别核对；旧的独立口令变量不再读取 |
| 调度 | PAMS_API_BASE、PAMS_REFRESH_STATE_PATH、日结和现金事件开关 |
| 隔离验证 | PAMS_TEST_POSTGRES_DSN；必须显式指向测试环境，不回退生产 PG* |
| 来源修复 | PAMS_REPAIR_DSN；与应用连接及隔离测试连接分开 |

根 .env 是 Compose 输入之一；还须核对实际启动时的 --env-file、进程环境及部署文件。config/.env 没有应用自动加载约定。秘密值不进入源码、文档、日志或 PR。

## 部署与挂载来源

- compose.yaml 声明默认服务、构建上下文、环境引用、网络和卷。实际部署可能使用额外覆盖文件，不能凭根 Compose 推断线上输入。
- API Dockerfile 复制 backend/api 和 config 到镜像。/app/config 被挂载时，挂载内容覆盖镜像内同路径默认文件。
- 默认 API 挂载为 config → /app/config（只读）、data → /app/data、账户台账 → /host/account-ledger（只读）、scheduler_state → /scheduler-state（只读）。资料目录不是源码。
- scheduler 写独立检查点卷；该卷只描述调度状态，不作为金融事实。
- web 从 frontend/Dockerfile 构建，nginx 配置在该文件中生成，静态文件不会通过修改工作区自动上线。
- gateway/Caddyfile 定义统一网关；PostgreSQL、Firefly、Caddy 和 AdGuard 分别使用自己的持久卷。

运行核对使用带时间的 docker inspect、实际镜像 ID、挂载、能力和健康观察。核准清单描述预期；机器观察描述实际。操作方式见 [发布身份](REPAIR_RELEASE_AND_RECOVERY.md#发布身份)，不得从未知现场自动生成核准预期。

## 依赖归属

| 入口 | 职责 |
| --- | --- |
| backend/api/requirements.txt、Dockerfile | API Python 依赖及 OCR 系统依赖 |
| backend/scheduler/requirements.txt、Dockerfile | 独立调度运行环境 |
| frontend/tools/requirements.txt | 手机 CSS 构建工具 |
| frontend/Dockerfile | 前端构建及 nginx 运行环境 |
| .github/workflows/quality.yml | 测试、静态检查和候选镜像验证 |

仓库不使用 VERSION 文件判断发布版本。开发进度与 CI 查询 GitHub，部署记录查询对应功能 PR 评论；实际镜像、配置和能力必须重新观察。
