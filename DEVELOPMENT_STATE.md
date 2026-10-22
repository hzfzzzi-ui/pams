# PAMS Development State

最后更新：2026-10-23。此文件是开发阶段、版本身份和验收状态的入口，不是资产事实表。

## 当前决策

- 当前任务：用户确认本月目标后批准先做“IBKR 同步事务一致性”；分级 M，阶段待用户验收（本地自测通过）。独立中文 [PR #42](https://github.com/hzfzzzi-ui/pams/pull/42) 已创建；不合并、不部署。
- 允许范围：把现有交易事实补录放到 collector 成功提交之前，补充失败回滚与重试回归；不改 XML 解析、报表选源、日期覆盖规则、金额/收益/NAV 公式、HTTP 字段、UI、schema 或调度配置。
- 根因与修复：原 collector 的成功日志提前提交，endpoint 后续补录失败仍留下已更新业务数据和 success 日志。现通过可选 after_write 回调把原补录纳入同一事务，再记录并提交成功；collector 的无回调调用方式保留。失败先回滚，再独立保存失败日志；失败日志异常保留原错误并写应用日志。
- 工作区：复用发布 worktree，新分支 `codex/ibkr-sync-transaction-20261023` 基于 `be2320c`；其父为 main `e664ccd4b9b93190c011d4be8b45ea7124a6a977`。显式携带上一轮 PR #41 部署文档，不单开部署记录 PR；部署中文评论已在原 #41。原脏工作区只同步状态/报告，不覆盖业务修改；旧 return-status 分支/worktree 保留。
- 底稿：`C:/Users/hzfzzzi/.codex/pams-evidence/ibkr-sync-transaction-20261023/`，含源码 ZIP/SHA256、接口/配置/容器/工作区指纹、只读 9 表校验、改前失败复现、固定输入对照。含财务信息，不上传；只读校验不是可恢复数据库备份。本轮没有生产写入、刷新、迁移或数据恢复。
- 本地验证：570 项测试通过（新增 17 项，显式使用独立 PostgreSQL）；六个中途失败点、提交前异常、真实延迟约束提交失败、重试、重复导入、默认 collector 兼容、日期选项及 HTTP 错误覆盖。CI 使用既有 PostgreSQL 服务，在新建随机测试库运行，不使用应用 PG* 凭据。
- 等价证据：固定输入的改前/后成功响应及 6 张业务表相同，仅排除原始文件路径、审计起止时间、行创建/更新时间；金额、日期、数量、汇率、业务 ID、备注未忽略，业务提交从 2 次减至 1 次。170 份现有 XML（169 成功、1 份同样失败）解析结果完整相等；AST 仅两个同步编排函数变化。
- 静态/镜像：定向 Ruff 和既有 CI mypy 通过；API 验证镜像构建、断网导入/OpenAPI、独立测试库 health smoke 通过。额外扩大至两个旧模块的 mypy 发现 4 条既有诊断，改前源码同样 4 条，不新增，也不趁机改解析逻辑；anyio 弃用警告保留。
- 生产只读前后核对：9 表行数/MD5、配置、四个生产组件镜像/容器/启动时间、原工作区业务差异和保留旧分支均相同。9 项 GET 中 8 项完整相等，账户总览仅顶层 as_of（源码明确为响应生成时间 datetime.utcnow）变化，其余业务日期/金额/收益率相同；不把未部署时的生产数值不变充当新同步逻辑的运行验收。
- 风险边界：原始 XML 落盘不属于数据库事务，performance 等按文件 mtime 选源的读取链路未改，因此本 PR 不能宣称“所有读取均使用上一有效报告”。报表选源/过期状态留待第二项独立确认；提交应答丢失的结果不确定性、并发调度和数据库完全不可用时的审计持久化未获全面保证。
- 当前运行版本：web 仍为下文 PR #41、API 仍为 PR #40，scheduler/Postgres 不变。验证镜像/独立测试库不属于生产部署；不会因为构建成功自动更新容器。
- 交付：业务修复提交 `40acfc93eabe38945c2a1213a93f8c5578d05c24`，PR #42 含本轮六个文件及 #41 保留文档；最终 head 的 CI 独立核对并记录在该 PR 中文评论，不以本地 570 项通过替代 CI。独立测试库容器和本轮验证镜像已清理，未留下额外运行实例。
- 下一步：用户审阅 PR #42 与其 CI/中文核验评论，确认后才决定合并和部署。不因自测通过登记全资产可信，不自动启动下一项。
- 验收稳定基线：未建立，待用户确认；不得把当前运行版本或技术标签追认为稳定版。

### 本月已确认边界（分项执行，不是本轮全部实施范围）

- A 资产/NAV 对账、B 收益正确性、C 自动化可靠性、D 开发效率贯穿推进；沿用当前账户目录，从 2025-12-31 期初核对至今，必要时前溯。
- 公积金、养老/医保、ESOP、应收暂均纳入 NAV；纳入不代表余额已核准。先以 IBKR/Flex 为自动化试点，不新增付费源。
- 同花顺历史 62.5% 归属比例已过期，新比例和生效日期待用户提供，不推断或改金额。必须考虑迟报、漏报和归属比例过期，不能把旧比例套到新净值并标为确认。
- 按各市场交易日、以日终确认为优先；官方账单/确认净值与预估行情分层，来源冲突待核准。关键资料未及时补齐时展示上一有效值并标过期，不补零或伪确认，不覆盖人工确认。
- 仅用现有同步中心提示；不擅自上云、新增付费服务或更改布局。已披露的待补资料可作为验收例外，已知金额错误必须修复或隔离；按周用户验收，每次合并/部署独立授权。

## 版本身份

### PR #41 部署记录（当前 web 运行版本）

- 合并 commit：`e664ccd4b9b93190c011d4be8b45ea7124a6a977`，PR #41 已 MERGED，origin/main 已更新；PR CI run `39738894204`、main CI run `39818517762` 均全通过。没有更改业务源码来发布。
- 北京时间 2026-10-23 01:05:50 更新 web；镜像 `pams-pams-web:pr41-20261023`，ID `sha256:6f37edb8a3771d7e68bff28a3406577b8377f8b8c93dd00fc6dd88253d269940`，revision 标签匹配上述提交，生产默认标签已指向该镜像。
- API 仍为 `sha256:9b3603f1c8240ed51640543d0590f1adea7d89405930ce831c31c118d37c8560`；scheduler 仍为 `sha256:68e9fb4dbc428c0abac4b965025ea2292210da0be69713fa1055c819016dd702`；两者和 Postgres 的容器 ID、启动时间、环境及挂载均未变。下文 PR #40 里的 web 为当时历史版本，不是当前 web。
- 发布方式：从合并源码直接构建带 revision 的 web 镜像；40 个静态文件与运行版相比仅 holdings.js、ui.js、investments.html 变化，包含生成手机样式在内的其他文件完全一致。使用原 project-directory/.env 和发布 compose，仅执行 `up -d --no-build --no-deps pams-web`，不重启其他服务。
- 证据：performance、portfolio-state、summary、holdings、完整日频 NAV、A股曲线响应逐字相同；账户总览仅生成时间变化；总收益仅少一条上述 SIVE coverage note。5 张事实/快照/状态表行数/MD5、配置文件哈希均相同，没有执行数据刷新、迁移或数据恢复。
- 页面：390px/1280px 两组实际线上页面、共 72 次手机详情，除新增状态行外完整 HTML 相等，组合头部及列表金额/收益率/尺寸/字体未变，无页面脚本错误；右侧详情确认可见，交易折叠入口、分析/风险入口可读取。
- 原右侧标签页此前停留 10/21 缓存，刷新后显示 10/22 数据；部署前后新会话均已显示 10/22 且逐项相同，不能把旧标签页刷新后的数字变化误称为部署引入的重算，也不隐去这项可见差异。
- 回退候选：`pams-pams-web:before-pr41-20261023`，ID `sha256:c7cb10ebc42508e4bec6cf394fd1f2b5576552736409c4761835f0be5276f2d4`，旧源码 `efae594fb38e1c72b0687ad0c00ce7295bfa69ab`；无 schema/API 变化，只需匹配 web 镜像，不回灌数据库。候选存在不代表允许自动回退。
- 完整中文部署记录归属原 PR #41 评论，不新建部署文档 PR；技术验证通过不等于用户验收或历史资产核准。

### PR #40 部署记录（当前 API 运行版本）

- 合并 commit：`75c67636dc2cd43c57b74cf5f3b655aa9fe70fc9`；PR #40 已 MERGED，`origin/main` 已更新。最新 head `da442cd7` 的 CI verify 全部通过，run `39721491327`；合并提交的 main CI `39722248257` 同样通过。
- 发布工作区：`C:/Users/hzfzzzi/.codex/worktrees/release-recovery/pams`，发布记录分支 `codex/pr40-release-20261022` 基于上述合并提交；原脏工作区保留 `3065f35` 及历史业务修改，仅同步规则/状态/报告。
- API 镜像：`pams-pams-api:pr40-20261022`，ID `sha256:9b3603f1c8240ed51640543d0590f1adea7d89405930ce831c31c118d37c8560`，revision 标签为上述合并 commit；启动于 `2026-10-22T04:27:05.559326231Z`（北京时间 12:27）。
- web 保持 `sha256:c7cb10ebc42508e4bec6cf394fd1f2b5576552736409c4761835f0be5276f2d4`；scheduler 保持 `sha256:68e9fb4dbc428c0abac4b965025ea2292210da0be69713fa1055c819016dd702`；两者及 Postgres 的容器 ID、镜像、启动时间均未变。
- 发布方式：合并源码构建并标记 revision，断网导入/OpenAPI 和固定输入回放通过后，使用原 project-directory/.env 与发布 compose 执行 `up -d --no-build --no-deps pams-api`。API 环境变量内容、挂载实际路径/读写权限、配置文件 SHA256 均未变；Docker inspect 的 Windows/Linux 主机路径表示已规范化对照。
- API 回退点：`pams-pams-api:before-pr40-20261022`，ID `sha256:a9b0bb97795d2b2aa43c7744a8d0294e6d1f264fdcfb4f5067b9379607c7a962`。仅为候选，不代表允许自动回退；无 schema 变更，不回灌数据库。
- 私有底稿：原目录 `.codex-artifacts/pr40-deploy-20261022/`，含前后接口、容器、配置哈希、只读表校验、结构化对比结果和页面截图；包含资产/环境信息，不上传 GitHub。
- 验证：合并镜像重放 8 条读取链路全部相等、1439 事件完全消费；实时 performance、portfolio-state、summary、holdings、完整日频 NAV 和 A股收益曲线逐字相同；账户总览仅响应生成时间变化，其余字段完全相等。总收益曲线全部金额、日期、收益率及其余字段不变，仅增加下述行情覆盖提示。
- 5 张关键表（cashflow fact、investment trade fact、holdings snapshot、asset daily pnl、portfolio valuation state）行数及内容 MD5 前后一致；未执行业务刷新、数据修正或迁移。health/OpenAPI HTTP 200，144 路由；持仓、交易列表、收益/NAV 图表及风险页可读取，原手机布局保留。
- 明确例外：总收益接口新增一条 `missing Yahoo close: SIVE` coverage note，不能写成完整响应逐字不变。该行情服务源码与改前相同，既有缓存命中时返回空 warnings，重启后冷读会重新出现缺价提示；本轮未修这项来源/告警缓存问题，曲线数值无差异。PR #38 已披露的行业/主题映射顺序不确定性仍待核准。
- 验证工具边界：生产镜像不含测试专用 httpx，最初 TestClient smoke 因此未运行；未给生产添加测试依赖，改用断网导入/OpenAPI 及部署后的标准库 HTTP 检查通过。不将工具依赖缺失记为接口失败。

### 第二项余额拆分（开发与自测历史，现已随 PR #40 部署）

- 分支 `codex/account-balance-service-20261022`，基于 `origin/main` 的 `45070c940dfc201776fe0da15c6047bb60a16959`。
- 已显式 cherry-pick 原保留文档提交 `2551293`、`00bbda4`，本分支对应 `ec8b9c8`、`9d6fd90`；AGENTS.md 的部署记录评论规则、PR #38 部署记录和 #39 关闭状态一并带入，不另开文档 PR、不重新打开 #39。
- 原工作区仍保留 `3065f35` 及历史业务修改；只同步规则/状态/报告，不把本轮业务代码覆盖到原脏工作区。
- `account_balance.py` 负责原有完整快照选择、锚点净值与现金变动滚动、前一净值及归属换算；不计算总收益、已实现收益和收益率。`portfolio_state` 与 `investment_returns` 共同依赖它，余额不再调用完整收益接口。
- 保留原查询、参数、日期及选源规则；同花顺专属/CNY 限制、初始资金配置校验失败回退、缺失/异常回退均有回归；没有借机更改旧字段或收益口径。
- 私有底稿：原目录 `.codex-artifacts/account-balance-20261022/`，含改前源码/归档、冻结配置/Flex、6 项实时 GET 底稿、容器身份及 SQL/行情录制回放；不得上传财务数据、凭据或 pickle 底稿。
- 固定输入证明：8 个读取链路（账户表现、账户总览、组合状态、summary、holdings、完整日频 NAV、A股及总收益曲线）输出一致；725 次查询 SQL/参数一致，总计 1439 个查询/读取/外部事件完全消费，5 次外部行情读取固定回放。只冻结账户总览的响应生成时间，不忽略余额/价格/估值日期或任何金额。
- 基线录制使用只读数据库连接；候选镜像在 `--network none` 下仅回放输入，不访问生产数据库或外部行情。既有行情来源有失败/回退，不在本轮修复；等价证据不等于历史数据准确或完整。
- 正式 tests 553 项通过，定向 Ruff/mypy、候选 API 构建、断网导入/OpenAPI smoke 通过；anyio/TestClient 弃用警告保留。候选 `pams-api:account-balance-pr-20261022` 只用于隔离验证，没有切换生产标签。
- 上述开发自测期间生产 API/web/scheduler 与 PR #38 部署记录一致；此后的 API 发布身份以本文件 PR #40 部署记录为准，web/scheduler 不变。

### PR #38 部署记录（上一 API 运行版本，保留历史）

- 记录归属：依用户新规则，完整部署记录迁至 PR #38 的中文评论，PR #39 已关闭、未合并；保留的规则和记录提交已显式带入本轮余额拆分分支，随本轮 PR 审阅，不另开部署记录 PR。

- 合并 commit：`45070c940dfc201776fe0da15c6047bb60a16959`，PR #38 已 MERGED；在独立分支 `codex/pr38-deploy-record-20261022` 记录部署，不覆盖原工作区历史业务修改。
- API 镜像：`sha256:a9b0bb97795d2b2aa43c7744a8d0294e6d1f264fdcfb4f5067b9379607c7a962`，revision 标签为上述 commit；启动于 `2026-10-22T03:42:15.630130296Z`。
- web 仍为 `sha256:c7cb10ebc42508e4bec6cf394fd1f2b5576552736409c4761835f0be5276f2d4`；scheduler 仍为 `sha256:68e9fb4dbc428c0abac4b965025ea2292210da0be69713fa1055c819016dd702`，两者启动时间未变。
- API 回退镜像：`pams-pams-api:before-pr38-20261022`，指向旧 `sha256:4ff3e9ce543c60c3a1a7b82caacb1e0399b21ee40ff4108b3f0d9961fe3ba288`。未改 schema，不需数据恢复；回退只切 API 镜像，不回灌数据库。
- 发布方式：从已合并源码构建带 revision 标签的镜像，断网导入/OpenAPI smoke 后，用 `--no-build --no-deps pams-api` 更新 API。配置及挂载仍来自原目录，未更改。
- 私有底稿：原目录 `.codex-artifacts/pr38-deploy-20261022/`；含部署前后响应、关键表校验、容器清单和风险只读 A/B，不上传。
- 验证：performance/summary/holdings 前后响应逐字一致；现金流事实、交易事实、持仓快照、单资产日收益、估值状态 5 表行数及内容 MD5 一致。容器内 health 返回 200，页面风险标签可加载。
- 风险例外：risk-disclosure 前后有 25 个叶字段变化，主要为行业/主题暴露金额及派生文案/比例，不能写成全接口不变。同输入、同 PYTHONHASHSEED=0/1 时旧/新镜像完整风险 JSON 均相等；仅改变种子，旧镜像自身即不相等。代码存在 set 身份键遍历后首匹配，属于既有映射顺序问题，本轮未修。风险结果仍待核准，不是完整资产审计。

### 此前实施现场（历史记录，非当前运行版本）

2026-10-22 更新：PR #36、#37 已按用户授权合并，`origin/main` 为 `fdae2a17bd876268e9af692e0d8d612ec8a8fe70`；本轮从该提交建立 `codex/ibkr-performance-service-20261022`。原工作区仍保留 `3065f35` 及历史修改，仅同步规则状态文档，不覆盖业务代码。下表及旧轮收尾内容为此前现场记录，不能替代本段和当前决策。

本轮生产现场：API `4ff3e9ce...`（2026-10-20T02:28:08Z）、web `c7cb10eb...`（源码 `efae594`，2026-10-20T03:05:13Z）、scheduler `68e9fb4d...`（2026-10-20T02:20:20Z）。API/scheduler 来源 commit 尚缺镜像标签，不推断其等同 main。配置和挂载仍来自原工作区，未更改。

| 层次 | 当前证据 | 验收结论 |
|---|---|---|
| 用户原工作区 | `C:/Users/hzfzzzi/openclaw/pams`，`codex/holding-cost-display-v1`，HEAD `3065f35`，含历史修改/未跟踪文件 | 是保留现场，不是完整可重建稳定版 |
| 集成/发布源码 | `C:/Users/hzfzzzi/.codex/worktrees/release-recovery/pams`，本轮文档基于 `4957152d90768f4370e51c9bec7341b2a8f826d5` | PR #28–35 已在此前合并；用户本轮反馈存在回退，未验收 |
| 本轮文档分支 | `codex/development-governance-20261020` | 仅文档 PR；提交/推送不代表允许合并或上线 |
| 本轮 UI 分支 | `codex/restore-mobile-investments-20261020`，基于上述文档分支 | UI PR 独立于 #36；不自动合并，非稳定基线 |
| 实际生产入口 | `http://pams.local`；compose 来源为发布工作区，project-directory 仍指向原工作区 | 本轮仅 web 恢复手机样式；API/scheduler 不变 |
| 技术标签 | `recovery-20261020-baseline`、`verified-recovery-20261020`、`runtime-verified-20261020` | 仅历史检查点，全部不是用户已验收的 stable 标签 |

当前运行镜像（本轮开始时只读核验）：

| 服务 | 镜像 ID |
|---|---|
| API | `sha256:4ff3e9ce543c60c3a1a7b82caacb1e0399b21ee40ff4108b3f0d9961fe3ba288` |
| web | `sha256:2182c8f2826f59abd4c67889ed76834754dcad59813a0bf03ef8edcaac79ebe1` |
| scheduler | `sha256:68e9fb4dbc428c0abac4b965025ea2292210da0be69713fa1055c819016dd702` |

镜像不含可核验的 commit 标签。根据上一轮构建记录，API 含 #35 的业务修复，web 含 #34，scheduler 含 #32；本轮没有重新构建，也不把混合构建时间说成一次原子发布。后续发布需补齐发布清单。

配置/状态来源：原目录 `config`、`data`、`账户台账`；数据库卷 `pams_pg_data`；调度检查点卷 `pams_scheduler_state`。配置未单独形成已验收版本。

## 能力保留清单

下列能力来自既有指令和代码记录，是核对清单；不代表当前实现已经被用户验收。

| ID | 必须核对和保留的能力 | 现有依据 | 当前状态 |
|---|---|---|---|
| KEEP-01 | dark app 风格、投资/收支/更多三项底部导航；交易录入/筛选折叠，明细前 10 单 | AGENTS.md；frontend static contract | 核心文件与旧镜像相同，不代表所有视觉表现无回退 |
| KEEP-02 | 持仓成本展示；A股账户级总盈亏；IBKR 半字段保护 | `54477bd`、`0a4657d`、`3065f35` | 当前源码保留；账户数值变化仍待验收 |
| KEEP-03 | 易沪深300归入A股基金；基金/美股预估和确认状态；Databento/Flex分工 | `99e3809`、`456e407`、`4ece872`；PR #29–30、#33、#35 | 未删除；历史行情时点与实时覆盖待核准 |
| KEEP-04 | NAV、账户净值、收益归因、已实现盈亏口径分别留痕 | 投资服务、PR #9–15 等历史记录 | 运行显示已变化，不能认定已对上 |
| KEEP-05 | 收支排行/饼图、内部转账、福利收入、原币显示及账户流水 | ledger 页面和 cashflow fact 服务 | 主页面源码保留；历史账单/分类仍需独立确认 |
| KEEP-06 | 穿透行业/主题/地区/币种分离，收益归因和资产底表可切换 | exposure/lookthrough/return_attribution 服务 | 当前可读取；日期一致性与映射缺口待核验 |
| KEEP-07 | 已有历史工作区增强不能无记录消失 | 原工作区修改与旧镜像离线比对 | 四个辅助 JS 已确认内容被替换；其余区分未发布/待确认 |

## 回退候选（未授权执行）

1. 重启前镜像组合：`pams-pams-api:before-recovery-20261020`、`pams-pams-web:before-recovery-20261020`、`pams-pams-scheduler:before-recovery-20261020`。完整 ID 和适用边界见盘点报告。
2. 原工作区 `3065f35 + 历史修改 + 未跟踪文件`：可做差异来源，不能直接当作可发布版本；bundle/补丁/清单并不等于全部未跟踪文件内容备份。
3. 当前 `4957152`：仅作为冻结现场，不是推荐恢复点或已验收基线。
4. 数据：上一轮资产表备份 `pams-assets-before-runtime-20261020.dump` 覆盖 `public.pams_*`，已读完整归档但未在隔离库完成恢复演练。两份全库/public 的 `.incomplete` 文件不可用；禁止回灌覆盖之后交易。
5. 旧镜像与当前 schema、配置、读写接口兼容性尚未验证；三张新空状态表存在，不能未经评估删除。代码/部署回退与数据恢复必须分开确认。

## 验证和证据

- 上一轮正式 `tests/`：499 项通过，lint/type/build/CI 通过；这是技术自测，不是用户验收或资产核准。
- 上一轮 28 张既有 PAMS 表校验一致；只证明当时事实未变，不证明显示口径未变。
- 本轮只读离线导出旧镜像，比较镜像内容、原工作区、集成源码；未运行旧服务。
- 本地底稿：原目录 `.codex-artifacts/recovery-20261020/`。不提交账单、API 金额底稿、配置明文或镜像归档。
- 专项清单：[本轮回退盘点](docs/RECOVERY_REGRESSION_INVENTORY_20261020.md)。
- 过程记录：[NIGHTLY_REPORT.md](NIGHTLY_REPORT.md)。先前“已验证/稳定”措辞必须按本文件的验收边界理解。

## 本轮收尾

### 2026-10-22：IBKR 风控依赖拆分（未部署）

- 本地底稿：原工作区 `.codex-artifacts/ibkr-service-20261022/`，含改前源码、账户配置、固定 Flex/XML 和汇率、四项 GET 响应、容器清单及对比脚本；含私有数据，不提交 GitHub。
- `services/ibkr_performance.py` 原样接收 7 个函数，endpoint 保留同名兼容导出；风控仅修改 import，所有其余逻辑保留。只读服务依赖 collector，不依赖 endpoint。
- 固定真实底稿：29 个字段相等、HTTP 响应字节相等、风控 IBKR 输入相等；7 个函数 AST 与基线相同。不是整套 NAV/收益数据核准。
- 验证：正式 tests 519 项通过；新增服务/测试 Ruff、服务 mypy、API 镜像构建、断网导入/OpenAPI smoke 通过。TestClient 依赖存在一条弃用警告，不影响测试结果。
- 候选镜像仅本地构建：`pams-api:ibkr-service-pr-20261022`；未运行生产更新。无数据库迁移或资产写入，生产回退不涉及本轮，因为没有部署。
- 代码回退候选为本轮单独提交；底层源码基线是上述 main，禁止覆盖原工作区历史修改。

### 此前 UI 恢复记录

- UI 恢复范围：复用旧手机 CSS 的级联与字体/间距，构建时将投资页虚拟宽度上限固定为 430px；原样式不修改，收支/更多等页面不加载专用样式。
- 仅投资页导航采用既有手机交互；pending 不再显示“未刷新”徽章，但 API 状态不变，预估/已确认标记保留。
- 技术自测：正式 tests 504 项通过，定向 lint/type、JS 语法、web 构建与 nginx 配置检查通过。
- 运行验证：1039px 宽屏内容区 430px，390px 手机无横向溢出；五项标签可切换，收益率/NAV 图表非空，交易默认 10 单且录入/筛选折叠，浏览器无 error/warn。
- 部署前后 `/summary2/latest?baseCurrency=CNY` 与 `/holdings/latest?limit=500` 响应逐字一致；API/scheduler 镜像及启动时刻一致。只证明本轮未改变这些接口，不代表资产已审计可信。
- 本轮 web：`sha256:c7cb10ebc42508e4bec6cf394fd1f2b5576552736409c4761835f0be5276f2d4`，源码 `efae594`；部署使用 `--no-build --no-deps pams-web`。后续文档提交不改变该构建身份。
- 本轮 web 回退点：`pams-pams-web:before-mobile-restore-20261020`（恢复前镜像 ID 同上）；API/scheduler 保持原镜像、原容器。
- 本轮批准仅解除 UI 范围冻结，其余盘点项继续冻结。

- 前一轮文档范围仅限 `AGENTS.md`、本文件和专项盘点文档；本轮新增上述投资页恢复与测试，原工作区历史业务修改保留。
- 文档 PR：[PR #36](https://github.com/hzfzzzi-ui/pams/pull/36) 与 UI PR #37 已于前轮获准合并；合并后未再次部署。合并不等于整套资产数据核准。
- 除本轮明确批准的 web 构建部署外，不执行业务刷新、数据修正或其他服务重建。
- 待用户确认：实际感知丢失的页面/能力与 REG 编号的对应关系、回退优先级、候选基线。
