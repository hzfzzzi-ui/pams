# PAMS 个人资产管理系统

PAMS 用于在手机上查看个人净资产、投资收益和日常收支。每笔投资交易和现金流水都归属到具体子账户，各页面共用同一组业务事实。已部署环境的局域网入口为 [pams.local](http://pams.local)。

## 从哪里开始

| 我想看什么 | 页面入口 |
| --- | --- |
| 持仓值多少钱，收益来自哪里 | **投资** → 持仓、分析。点开标的可看个人持仓收益率、大盘对比、持仓市值和成交记录。交易、风险、配置也在投资页。 |
| 钱花在哪里，转账是否配对 | **收支** → 总览看收支趋势和分类排行，流水按账户筛选。内部划转的已确认和待配对记录、借还款明细都在流水里。 |
| 某个账户的余额、资料更新到哪天 | **更多** → 账户中心。选机构可看各子账户的金额和更新时间，从对应子账户进入「导入资料」，按该账户支持的方式录入和核对。 |

## 本地开发

首次在本机启动 API，可按[本机开发库指南](database/postgres/bootstrap/README.md#本机开发库)创建隔离的 PostgreSQL 16 空库，初始化后启动 FastAPI。示例使用本机 8000 端口，仅启动开发 API。需要运行 Web、网关或调度器时，先核对[服务配置](docs/CONFIGURATION.md)和 [Compose 服务定义](compose.yaml)。

开发库的初始结构由[空库清单](database/postgres/bootstrap/manifest.txt)指定。已有数据库的结构升级、历史数据回填有各自的操作流程，见[数据库初始化与升级](docs/REPAIR_RELEASE_AND_RECOVERY.md#数据库初始化与升级)。

## 数据怎样到达页面

资料导入和外部采集先保留来源，再形成账户余额、持仓、交易和现金流水等事实。持仓数量沿用最近确认的记录，行情、基金净值及汇率按估值日期更新市值。现金和出入金独立记录，例如银行卡申购基金时，收支页据关联记录识别资金划转，投资页据申购交易更新份额。涉及金额和日期时可查[数据与收益口径](docs/DATA_AND_RETURNS.md)及[资料导入与事实写入](docs/IMPORT_AND_FACTS.md)。

| 改动位置 | 对应职责 |
| --- | --- |
| [frontend/public](frontend/public) | 移动端页面、交互与 API 数据展示 |
| [backend/api/endpoints](backend/api/endpoints) · [backend/api/domain](backend/api/domain) | 接口组织请求与响应，领域服务统一计算账户、收支、投资和风险结果 |
| [backend/api/collectors](backend/api/collectors) · [backend/scheduler](backend/scheduler) | 对接外部来源，调度数据刷新 |
| [database/postgres](database/postgres) · [config](config) | PostgreSQL 结构、初始化清单与业务配置 |

## 验证与发布

仓库采用单人开发流程，GitHub CI 保持关闭。按改动范围在本地验证，只有关键财务口径变化或实际缺陷需要防止复发时才补自动测试。验证结果记录在对应 PR，部署后按发布文档一次核对运行版本和受影响的业务结果。见[开发工作流程](docs/DEVELOPMENT_WORKFLOW.md#02-单人开发的本地验证)和[部署核验](docs/REPAIR_RELEASE_AND_RECOVERY.md#发布身份)。

其他业务与页面说明见[文档索引](docs/README.md)。仓库协作规则见 [AGENTS.md](AGENTS.md)。
