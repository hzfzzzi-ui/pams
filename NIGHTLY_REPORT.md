# PAMS Autonomous Improvement Report

## 2026-10-23：IBKR 同步事务一致性（M 级，PR #42，待用户验收）

- 发现并复现：collector 成功日志先提交，随后交易事实补录失败，仍留下已更新持仓、历史、交易、资金确认和 success 日志。改前真实 PostgreSQL 失败注入证据私有保存。
- 最小修复：原有事实补录通过可选 after_write 回调纳入 collector 的同一事务，成功日志与业务一起提交；失败先回滚再保存失败日志，审计写失败不掩盖原异常。不改解析/金额/日期选源/接口字段，不增加表或前端改动。
- 验证：全量 570 项测试通过，新增 17 项覆盖六处失败、真实提交约束失败、重试/幂等、日期参数、默认调用和 HTTP 错误；定向 Ruff、既有 CI mypy、API 构建、断网导入/OpenAPI、独立库 health smoke 通过。CI 接入既有 PostgreSQL 服务，仅新建随机测试库。
- 等价：固定输入改前/后完整成功响应与六张业务表一致，仅忽略路径和审计/行时间；不忽略金额、业务日期、数量、汇率、ID 或备注。业务提交由 2 次变为 1 次；170 份 XML 的全部解析字段/异常相同（169 成功、1 原有失败）。仅两个同步编排函数 AST 改变。
- 生产只读核对：9 表行数/内容 MD5、配置和四个生产组件身份未变，原工作区业务差异/旧分支保留；9 项 API 中 8 项完整相等，总览仅顶层响应生成时间变化。没有把生产未更新时的数值不变当作新逻辑已上线验收。
- 风险与例外：额外扩大 mypy 检查出现 4 条旧诊断，改前同样 4 条，未修或新增；anyio 警告保留。raw XML 落盘和 mtime 选源未改，尚不能保证所有读取只发布上一有效报告；提交应答丢失、并发任务和数据库不可用时的日志持久化仍有边界，不能称全资产已核准。
- 底稿：`C:/Users/hzfzzzi/.codex/pams-evidence/ibkr-sync-transaction-20261023/`，含只读接口/9 表校验、配置/容器、源码归档及复现/等价证据，不上传财务数据或凭据；不是数据库恢复备份。
- 流程：复用原发布 worktree，新分支基于 `be2320c`，显式归入 #41 遗留部署文档；原工作区仅同步状态/报告。旧 return-status 分支保留，不合并、不部署、不改生产数据；下一步中文 PR 和 CI，报表选源/过期状态另行确认。
- 交付：[独立中文 PR #42](https://github.com/hzfzzzi-ui/pams/pull/42)，业务修复 `40acfc93eabe38945c2a1213a93f8c5578d05c24`；最终 head 的 CI 结果在原 PR 中文评论单独记录。测试库容器和验证镜像已清理，不增加常驻实例，未动生产标签/卷。

## 2026-10-23：PR #41 合并并仅发布 web（S 级，待用户验收）

- 用户明确授权合并部署；匹配 head `ff1194e7` 复核 PR CI 全通过后合并，main 更新到 `e664ccd4b9b93190c011d4be8b45ea7124a6a977`，main CI run `39818517762` 同样全部通过。
- 从合并源码构建 revision 标签匹配的 web 镜像 `sha256:6f37edb8a3771d7e68bff28a3406577b8377f8b8c93dd00fc6dd88253d269940`，北京时间 01:05:50 只更新 web；API/scheduler/Postgres 容器、镜像、启动时间、配置和挂载不变。
- 先保存部署前 8 项 API/5 表校验/配置及旧静态资源底稿，保留 `pams-pams-web:before-pr41-20261023` 回退点；原 project-directory/.env，使用 `--no-build --no-deps pams-web`，不刷新业务数据，不迁移或恢复数据库。
- 40 个静态文件中仅预期的三文件变化，所有 CSS/生成手机样式和其余文件相同；26 项前端/手机布局测试、JS 语法、前端构建和断网 nginx smoke 通过。PR 阶段正式 553 项测试保持通过。
- 实际线上 390px/1280px 各 36 个持仓完整详情、组合头部和列表前后比较通过，除新增状态行外 HTML 一致，金额、收益率、布局尺寸和字体无差异；右侧详情可见“今日收益已确认”，交易折叠入口及分析/风险页可读取。
- 接口差异严格披露：账户总览仅响应生成时间变化；收益曲线仅消失一条 `missing Yahoo close: SIVE`，现有缓存命中分支返回空 warnings，其余完整字段及数值不变，不代表缺价已解决。5 表行数/MD5 与配置哈希一致。
- 原右侧旧标签页显示 10/21 缓存，刷新后为 10/22；两组部署前后新会话均为 10/22 且一致，已区分缓存刷新和发布变更。旧风险映射、缺价覆盖、F541、nginx MIME 和依赖警告不在本轮修复范围。
- 证据私有目录 `C:/Users/hzfzzzi/.codex/pams-evidence/pr41-deploy-20261023/`；原工作区仅同步状态/报告，历史业务差异保留；旧 return-status 分支和 worktree 不删除。部署记录追加原 PR #41 中文评论，不单开 PR；必要文档推送既有分支，随下一条相关 PR 归入。
- 下一步：等待用户验收本轮显示，不登记 stable 或全资产已核准，不扩大到第三、四项拆分。

## 2026-10-22：补回两处持仓详情收益状态（S 级，未合并、未部署）

- 发现：旧未合并 `return-status-badges-v1` 独有两处详情状态行，当前主线已有名称徽标和后端状态字段；不认定这两行曾上线，不整体合并旧分支。
- 修复：表格展开详情、手机详情弹层在“今日盈亏”后复用现有徽标；confirmed/estimated 沿用已有标签，pending/缺失为 `—`。只加两行模板并同步持仓脚本加载/预加载版本，不改函数、CSS、布局或财务口径。
- 验证：553 项测试通过，扩充既有 VM/静态契约覆盖两种详情入口、六组状态/转义及金额不变；现有 CI lint/mypy、JS 语法、前端构建、断网 nginx smoke 通过。冻结底稿在 390px/1280px 各 36 个持仓的列表 HTML、尺寸和字体相同；全部详情仅新增状态行，其余 HTML（含金额/收益率）相同。
- 风险：额外静态测试文件 lint 发现原有 F541，改前底稿同位置同诊断，未新增或修复；anyio/Node VM 及 nginx 重复 MIME 既有警告保留。隔离页面验证仅加载持仓模块，其他模块未做新的全功能运行验收，不等于历史资产核准。
- 底稿：`C:/Users/hzfzzzi/.codex/pams-evidence/return-status-details-20261022/`，保存源码归档/哈希、5 项只读 API、配置/容器/受保护工作区指纹、前后截图和渲染等价结果；财务数据不上传。
- 保留：新分支复用原发布 worktree 并携带此前 PR #40 部署记录，不另开部署文档 PR；原工作区只同步状态/报告。旧分支/worktree 留存，不执行合并、部署、数据刷新或生产容器变更。
- 下一步：独立中文 PR 审阅及 CI；等待用户决定合并/发布，之后再单独确认旧分支清理。

## 2026-10-22：PR #40 合并与 API 部署（M 级，待用户验收）

- 用户授权“合并部署”；复核最新提交 CI verify 全通过后匹配 head 合并，main 更新到 `75c67636dc2cd43c57b74cf5f3b655aa9fe70fc9`。此前保留规则与 #38 部署记录均已随 #40 合入；#39 仍关闭。
- 从合并源码构建 API 镜像 `sha256:9b3603f1c8240ed51640543d0590f1adea7d89405930ce831c31c118d37c8560`，revision 匹配；仅重建 API 容器，web/scheduler/Postgres 的容器、镜像和启动时间均不变，UI/schema/配置未改。
- 先保存新私有底稿和回退标签 `before-pr40-20261022`（指向上一 API `a9b0bb97...`）；核对原 project-directory/.env/挂载后使用 `--no-build --no-deps pams-api` 更新。无数据写入、迁移或业务刷新。
- 合并镜像断网回放 8 条读取链路、1439 事件全部一致；实时余额/账户表现/summary/holdings/NAV/A股曲线前后逐字一致，账户总览仅生成时间变化。总收益曲线的金额、日期、收益率完全一致，仅多一条 `missing Yahoo close: SIVE` 覆盖提示。
- 该行情服务源码未变；既有进程缓存命中会丢失 warnings，冷启动后重新披露缺价。本轮不修改告警缓存或价格来源，不把提示变化或已有风险映射顺序问题隐去，也不把本轮等价验证说成全资产核准。
- 5 张关键事实/快照/状态表行数及 MD5、配置文件哈希一致；API 环境变量内容及实际挂载来源/权限一致。health/OpenAPI 200、144 路由；浏览器持仓和收益/NAV 图表可见，手机布局保留。
- 初次 TestClient smoke 因生产镜像没有测试专用 httpx 中止；不添加生产依赖，断网导入/OpenAPI 及上线标准库 HTTP smoke 均通过。私有比较器对环境变量顺序和 Windows/Linux 挂载表示做等价规范化，不排除金额、业务日期或行情数据。
- 合并提交的 main CI run `39722248257` 亦全部通过；交易列表和风险页已确认可读取，未尝试写入操作。
- 证据位于原目录 `.codex-artifacts/pr40-deploy-20261022/`，含前后响应/表校验/容器/配置哈希/截图，私有不上传。完整部署记录已追加到 [原 PR #40 中文评论](https://github.com/hzfzzzi-ui/pams/pull/40#issuecomment-6332827364)；状态文档同步两工作区，推送现有发布分支，待下条相关 PR 显式归入，不另开文档 PR。
- 下一步：等待本轮验收；历史行情完整性、SIVE 来源/告警缓存及映射非确定性另行确认，第三、四项拆分尚未实施，不登记 stable 基线。

## 2026-10-22：账户余额与收益模块解耦（M 级，未部署）

- 从已合并 main 45070c9 建立独立分支；显式带入此前保留的规则及部署记录提交，不混入原工作区其他历史修改，不重开 #39 或单开文档 PR。
- 提取只读 `account_balance.py`，共用现有完整快照和现金变动滚动净值计算。余额直接读基础层；收益继续负责总收益、已实现收益、收益率和今日收益展示，接口字段保持原样。
- 回归覆盖资金变动正负、归属比例含零、当日/未来/缺失锚点、负余额、空快照、无前一日、非法初始资金、非同花顺/非CNY与异常回退；余额来源选择未修改。
- 正式 tests 553 项通过，新增 34 项；定向 Ruff/mypy、候选镜像构建和断网导入/OpenAPI smoke 通过；anyio/TestClient 弃用警告未扩大范围修复。
- 固定输入回放覆盖 8 条账户/余额/持仓/NAV/收益链路，完整输出一致；725 次查询及参数、共 1439 个事件一致，5 次外部价格读取固定。只冻结账户总览响应生成时间，金额和业务日期不排除。候选断网运行，无生产 DB 连接。
- 改前底稿和私有 SQL/行情录制位于原工作区 `.codex-artifacts/account-balance-20261022/`，不上传；录制时数据库只读。既有部分行情失败已连同回退冻结，不代表来源数据质量通过。
- 未合并、未部署、未发业务刷新、未修改事实或 schema；三个生产服务镜像及启动时间未变。第三、四项拆分及既有风险映射顺序问题不在范围内。
- 下一步：提交本轮独立中文 PR，连同规则/部署记录等待用户审阅，不自动合并或发布。

## 2026-10-22：部署记录归属规则（S 级）

- 用户要求不再为部署记录另开 PR；AGENTS.md 已明确部署信息追加到原功能/修复 PR 的中文评论。
- #39 的部署范围、版本、验证、回退点和剩余风险已迁至 [#38 中文评论](https://github.com/hzfzzzi-ui/pams/pull/38#issuecomment-6332313957)；#39 已关闭，未合并记录分支。
- 原工作区与发布工作区的规则、状态及报告同步；文档提交留在现有分支，随下一条相关 PR 归并，不直接推送 main、不另开 PR。
- 仅文档与 GitHub 记录操作；不改业务代码，不部署、不重启、不运行数据刷新。

## 2026-10-22：PR #38 API 部署（M 级，待用户验收）

- 按用户本轮授权，只部署并重启 API；合并源码 `45070c9`，镜像 revision 标签匹配。web/scheduler/Postgres 不重启，UI、schema、配置和资产事实不修改。
- 先保存 API 回退标签 `before-pr38-20261022` 及本地私有底稿，再构建并 smoke，最后通过 `--no-build --no-deps pams-api` 部署。生产 health 200，浏览器风险页正常加载。
- performance/summary/holdings 部署前后响应逐字一致；5 张关键事实/快照/状态表的行数及内容校验一致。
- 发现不能忽略的例外：风险披露 25 个叶字段变化，涉及行业/主题暴露金额、文案和比例。旧/新镜像 Python 源码只有本 PR 的三个文件不同；在同只读数据库/配置、固定哈希种子 0/1 的 A/B 中完整风险结果均相等，旧镜像改变种子自身即发生差异。set 身份键遍历首匹配存在非确定性，非 PR #38 新增公式变化；不顺带修复，明确保留待核准风险。
- 临时 A/B 容器无公开端口、挂载只读、数据库连接强制只读，运行后自动删除；未对生产发同步写请求。验证不等于全套历史账目正确。
- 详细镜像 ID、回退点和证据路径见 DEVELOPMENT_STATE.md；记录归属已改为原功能 PR #38 中文评论，独立记录 PR #39 按用户要求关闭、不合并。

## 2026-10-22：风控与 IBKR endpoint 解耦（M 级，待 PR 审阅）

- 范围：仅第一项拆分；新增共用只读表现服务，endpoint 保留兼容名称，风控改变 import。解析、金额、字段、异常处理及数据库连接规则原样保留；不动 UI/schema/配置。
- 改前底稿保存在原工作区 `.codex-artifacts/ibkr-service-20261022/`；私有 Flex、配置、接口响应和容器信息不上传。
- 证据：迁移的 7 个函数 AST 与 main fdae2a1 相同；固定真实 Flex/汇率下全部 29 个字段和 HTTP 响应字节相同，风控 IBKR 输入相同。测试覆盖缺汇率、负现金、日期回退、空持仓、文件选择、404/422/500 及风控失败回退。
- 验证：正式 tests 519 项通过；定向 Ruff/mypy、候选 API 镜像构建、断网导入与 OpenAPI smoke 通过。依赖 anyio 的 TestClient 弃用警告保留，不在本轮改依赖。
- 限制：没有部署、合并、业务同步、数据库写入或其他三项拆分；本轮不是完整资产审计，不能将静态等价和固定样本对比解释为所有历史数据正确。
- 下一步：独立中文 PR，等待用户审阅；本轮明确禁止合并与部署。

## 2026-10-20：旧手机布局恢复（M 级，待用户验收）

- 用户确认投资页宽屏也使用旧手机布局；不重做页面，不修改后端与账务。
- 根因：全局 design-system 展示手机列表，但桌面断点没有应用手机行内样式，产生表格/列表重复及字体间距差异。
- 构建时复用原 CSS 级联生成投资页专用手机样式，虚拟宽度上限 430px；其他页面继续原样式。原 CSS 哈希锁定，避免人工复制大段样式。
- 投资页使用既有手机标签交互；pending 徽章恢复隐藏，预估/确认标记保留，API 状态不变。
- 正式 tests 504 项通过，定向 lint/type、JS 语法、镜像构建、nginx 检查通过；1039px/390px 页面、五项标签、非空图表和交易折叠已验证，浏览器无 error/warn。
- 只重建并部署 web；summary/holdings 响应与部署前逐字一致，API/scheduler 未重启。回退镜像 `pams-pams-web:before-mobile-restore-20261020` 已保留。
- 风险：本轮验证不核准历史资产数据；图表首次读取仍较慢，未在 UI 恢复任务中修改；尚未用户验收，PR 不自动合并。
- 下一轮：先由用户验收展示，再按回退盘点逐项决定是否恢复其他能力。

## 2026-10-20：持仓状态别名兼容（M 级）

- 页面复测发现旧 `daily_pnl_status` 仍先于新 `daily_return_status` 被读取，可能一边 pending、一边 confirmed。
- API 统一两套兼容字段及文案，消除前端继续显示历史确认的路径，不改变金额与任何资产事实。
- 新增旧字段覆盖回归；合并前两轮后正式 tests 499 项通过，lint/type、API 构建及 health 通过；正式页面已确认基金显示未刷新，市值和盈亏不变。

## 2026-10-20：运行调试第二轮（M 级）

### 修复与验证

- 统一质量面板原先仅展示记录覆盖，漏掉后端 `checks` 的账户缺失、估值过期等警告，误显示无阻塞动作。
- 现在直接展示非 ok 检查，标记对应数据集待核准，保留已有操作入口；不在前端推算数据日期或资产口径，不变更布局和样式。
- 增加可执行 JS 渲染回归，覆盖健康、警告、未知检查安全转义及缺数据；正式 tests 498 项通过。JS 语法、定向 Python lint/type、前端镜像和 nginx 检查通过。
- 已在 in-app browser 验证投资、交易展开/收起、风险/配置、收支分类切换/账户、更多/同步详情；移动端 390px 无页面横向溢出，质量面板显示两项后端警告。测试不等同真实写入链路验收。
- 18 项核心 API 读取返回 200。首次收益曲线约 15 秒、研究健康检查约 32 秒、穿透响应约 1.37 MB，列入性能待办，不以放宽超时冒充优化。

### 剩余风险

- NAV 当前读数为未发布草稿；旧发布值不再覆盖，不代表已完成真实对账。账户确认缺失、估值时点混用和归因差异仍存在。
- A股、基金、贵金属来源过期；调度历史补跑按设计阻断，不能使用当前价填过去日期，历史完整性保持未核验。
- 同步矩阵的银行/现金记录状态只表示存在导入，不表示当前余额已确认；需后续统一陈旧度披露。
- Firefly cron 容器健康检查报 unhealthy，主 Firefly 服务健康；本轮未更改外部记账服务和其数据。
- 没有尝试真实金融交易、资产录入确认或付费行情请求；后续需在隔离库验证写入，再经明确来源补齐历史。

## 2026-10-20：运行调试第一轮（M 级）

- 发现：旧基金 NAV 的来源状态会被当成今日已确认，即便净值日期早于展示日期。
- 修复：持仓状态只认可同一展示日期的来源，历史来源显示未刷新；保留来源日期，不改变金额、份额、成本和盈亏。
- 测试：增加旧基金 NAV、旧行情确认和当日基金确认回归；正式 `tests/` 497 项通过，定向 lint、mypy 和 API 镜像构建通过。
- 运行：Docker 已恢复；已构建并部署 API、前端和调度器，核准原项目挂载及既有数据库卷。
- 备份：资产相关 `public.pams_*` 表备份已完整读取校验。全库/全 public 备份含大量研究行情，因 Compose 重建数据库容器被中断，已标为 `.incomplete`，不能用作完整备份。
- 数据：部署前后 28 张既有 PAMS 表校验值一致，只新增 069 中三张空状态表。旧版本过期已发布 NAV 覆盖被移除，页面值因此变化，不代表发生交易，也不能宣称资产总值已核准。
- 同步：重启前后检查点文件校验一致；启动补跑明确阻断历史回填，未写入收益快照。历史完整性仍为未核验。
- 待办：统一质量面板未展示 API 已返回的警告；A股、基金、贵金属行情过期；账户账单缺口、研究任务异常、归因跨日期差异仍需单独处置。
- 边界：本轮未执行真实交易录入、导入确认、账户核准或付费行情请求。根目录无参数 pytest 会收集已退出正式验证范围的旧 backtest_V2 并报缺模块；正式验收命令为 CI 中的 `python -m pytest tests -q`。

## 2026-10-20：恢复主线与同步可靠性

### 发现的问题

- 21 条历史 PR 未收敛；开发分支、main 与本地历史修改不一致。
- 干净检出缺少导入模块和旧入口说明；镜像遗漏服务代码及显式运行依赖，并引用未跟踪的运行数据目录。
- 调度器把 HTTP 成功当成业务成功，部分更新、旧 Flex 和基金净值缺失仍可能继续发布收益快照。
- 停机后无持久化成功日期和历史缺口记录。

### 已修复

- 原工作区保留，Git bundle、修改补丁及未跟踪清单已保存到本地 recovery 目录。
- 历史 PR 按依赖合并或注明替代关系后关闭；#28–31 分别收敛打包、行情状态、持仓状态、依赖及镜像验证。
- 调度增加业务结果校验、失败阻断、三次以内网络重试和只读同步健康输出。
- 检查点原子持久化、成功窗口去重、失败补跑、历史缺口保留；禁止用当前价伪装历史数据。
- 详细处置、补跑边界及部署限制见 `docs/RELEASE_RECOVERY_20261020.md`。

### 验证与风险

- 干净基线 451 项测试通过；增加同步边界测试后，独立虚拟环境 494 项通过。
- API ASGI 路由 smoke、Compose 配置、Python 编译、定向 lint 和核心类型检查已执行。
- PR #31 云端 CI 已验证临时 Postgres、API/前端镜像；同步改动还增加调度镜像与纽约时区检查，最终结果以 PR #32 的最终提交检查为准。
- 本机 Docker Desktop 启动失败，自动审批拒绝清理遗留 socket，生产部署及真实数据验收尚未完成。
- 单项刷新已落库的数据不自动回滚；历史交易日完整回补、交易所特定节假日以及生产资产对账仍需后续验证。

### 下一轮

- 恢复 Docker 后保存数据库底稿，核对历史价格与交易缺口，再启用生产调度。
- 对资产、负债、内部划转和期初/期末 NAV 做独立对账，不把 CI 通过等同于真实资产已核准。

## 2026-06-28 追加：A股账户总盈亏绑定修复

### 本轮目标

- 修复持仓页 A 股账户顶部“总盈亏”显示退回持仓口径的问题。
- 保持后端 API、数据库、事实表和持仓行展示不变，只修正前端汇总字段绑定。

### 已修复问题

- `holdings_categories.js` 中 A 股账户汇总现在显式将 `accountTotalPnl` 映射为通用 `totalPnl`，避免顶部卡片继续使用持仓行汇总盈亏。
- A 股账户收益率优先使用 `accountReturnRate`，持仓收益率仅保留为行项目和持仓口径展示。
- 同步提升 `holdings_categories.js` 静态资源版本号，避免浏览器继续加载旧缓存。
- 新增前端静态契约测试，锁定 A 股账户卡必须使用账户级总盈亏。
- 追加修复 IBKR 半字段账户 performance：当接口没有 `total_pnl_cny` 或 `net_deposit_cny` 时，不再用账户净值反推总盈亏，避免把融资现金负债推成组合大额亏损。

### 验证记录

- `node --check frontend\public\assets\js\holdings_categories.js`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `docker compose --env-file C:\Users\hzfzzzi\openclaw\pams\.env up -d --build pams-web`
- HTTP smoke：`/investments.html` 已引用 `holdings_categories.js?v=20260628-account-total-pnl-v2`，线上脚本包含账户级总盈亏映射和 IBKR 半字段保护。

## 2026-06-28

### 本轮目标

- 修复持仓页移动端条目“持有金额”误显示为市值的问题。
- 保持总资产、市值、权重和收益口径不变，只调整单个展示字段。

### 已修复问题

- 移动端持仓条目的主金额由 `market_value` 改为 `cost`，文案同步为“持仓成本”。
- 详情页仍保留“市值”和“剩余成本”，用于区分市场估值与成本口径。
- 新增前端静态契约测试，防止该字段再次绑定回市值。

### 验证记录

- `node --check frontend\public\assets\js\holdings.js`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `git diff --check -- frontend/public/assets/js/holdings.js frontend/public/investments.html tests/test_frontend_static_contract.py`
- `docker compose --env-file C:\Users\hzfzzzi\openclaw\pams\.env up -d --build pams-web`
- 运行态 HTTP 校验：`/investments.html` 已引用 `holdings.js?v=20260628-holding-cost-display-v1`，新版 JS 包含“持仓成本”展示。

### 风险项

- 本轮没有修改后端资产事实表，也没有改变总资产、收益率、权重和 NAV 口径。
- in-app browser 可视化刷新检查超时，已用 HTTP 静态资源校验替代；若右侧仍显示旧文案，需要浏览器强刷。

## 2026-06-24

### 本轮目标

- 先保存当前资产与收益数据底稿，再做小范围后端口径修正。
- 修正 PortfolioState、NAV bridge、收益归因之间的净值口径漂移。
- 每完成一项改动后自检，禁止用前端临时计算掩盖后端数据问题。

### 已保存底稿

- 开工底稿：`.codex-artifacts/audits/baseline-20260624-163222`
- 改后快照：`.codex-artifacts/audits/after-20260624-165136`

### 已修复问题

- `PortfolioState` 的投资交易组件改为读取 `pams_investment_trade_fact`，不再依赖旧的空表口径；当前运行态读取到 331 笔投资交易。
- `PortfolioState` 的估值序列改为读取 `pams_portfolio_valuation_state`，并保留投资市值、总盈亏、收益率等统一字段。
- 数据质量接口新增结构化检查：组件覆盖、账户余额缺失、持仓日期缺失、估值序列滞后、外币汇率缺失。
- NAV bridge 新增可审计的逐步桥接：期初 NAV + 外部现金流 + 投资/估值影响 + 残差 = 期末 NAV。
- 收益归因的 `portfolio_nav_cny` 改为使用统一 NAV 账户估值口径，避免 IBKR 等杠杆账户用总资产或账户余额重复统计。
- NAV bridge 不再只看总残差为 0 就显示可信；若投资增值与收益归因仍有差额，会标记 `unreconciled`。

### 当前审计结论

- 运行态 `portfolio/state` 组件行数：账户余额 24、当前持仓 31、投资交易 331、现金流水 359、转账匹配 10、汇率 6、估值序列 1。
- 运行态 NAV bridge：期初 NAV 798,725.37，期末 NAV 1,123,955.32。
- 收益归因 `portfolio_nav_cny` 已对齐 NAV bridge 期末 NAV：1,123,955.32。
- 数据质量状态为 `warning`：缺少 `alipay`、`gf_7443`、`gf_9711` 账户余额；估值序列日期仍滞后于当前持仓日期。
- NAV bridge 状态仍为 `unreconciled`：投资增值与收益归因存在 63,366.72 元解释缺口。该缺口现在会被显式暴露，不再被隐藏为可信。

### 验证记录

- `python -m compileall -q backend\api\services`
- `python -m pytest tests/test_portfolio_state_contract.py tests/test_portfolio_valuation_state.py tests/test_valuation_nav_service.py tests/test_portfolio_equity_bridge.py tests/test_return_attribution.py tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py -q`
- 结果：98 passed。
- `docker compose up -d --build pams-api`
- 运行态 smoke：`/health` 返回 ok，`/api/portfolio/state`、`/api/portfolio/data-quality`、`/api/portfolio/equity-bridge`、`/api/analysis/return-attribution` 均可返回。

### 下一轮建议

1. 继续追 NAV 投资归因缺口 63,366.72 元，优先检查已清仓/已实现盈亏、历史估值快照和期初持仓回溯。
2. 补齐或显式排除 `alipay`、`gf_7443`、`gf_9711` 账户余额，降低数据质量 warning。
3. 补齐连续估值序列，让 NAV 曲线不再依赖单日估值状态。

### 追加轮次：NAV 质量状态分层

#### 发现的问题

- NAV bridge 的总账 roll-forward 已经闭合：期初 NAV + 外部现金流 + 期间投资/估值影响 = 期末 NAV，运行态 `unexplained_residual_cny = 0.00`。
- 原先 `quality.status = unreconciled` 的主因不是总账不平，而是把 `period_investment_effect_cny = 14,484.20` 与 `canonical_total_pnl_cny = 77,850.93` 直接比较。
- 这两个数不是同一层口径：前者是年初至今 NAV 期间贡献，后者是当前持仓/清仓累计成本口径盈亏，可能包含期初已持仓浮盈、现金/汇率/融资影响和估值时点差。

#### 已修复问题

- `_bridge_quality` 拆分为 blocking 与 advisory：真实 NAV 残差或入金闭环失败仍进入 `blocking_checks`，累计持仓盈亏与期间 NAV 贡献的口径差进入 `advisory_checks`。
- equity bridge API 新增 `quality.nav_reconciled`、`quality.advisory_checks`、`quality.attribution_comparison_basis`、`investment.period_attribution_available`、`investment.comparison_basis` 和 `investment.comparison_note`。
- 更多页的 NAV 增值解释读取 `bridgePayload.quality`，总账闭合但归因未期间化时显示“总账闭合 · 归因口径差”，并展示 advisory 原因。

#### 运行态结论

- 当前 API 返回：`quality.status = warning`，`nav_reconciled = true`，`blocking_checks = []`。
- 保留显式提醒：`advisory_checks[0].gap_cny = -63,366.72`，说明当前收益归因仍是累计口径，不等同于年初至今 NAV 期间贡献。
- 资产数据没有被修改；本轮只调整后端质量状态语义和前端解释状态。

#### 验证记录

- `python -m pytest tests/test_portfolio_equity_bridge.py tests/test_return_attribution.py tests/test_portfolio_state_contract.py tests/test_valuation_nav_service.py -q`
- `python -m pytest tests/test_frontend_static_contract.py -q`
- `node --check frontend\public\assets\js\nav_growth_explanation.js`
- `node --check frontend\public\assets\js\more.js`
- `python -m compileall -q backend\api\services`
- `docker compose up -d --build pams-api pams-web`
- 运行态 smoke：`/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY` 返回 `quality.status = warning`、`nav_reconciled = true`、`blocking_checks = []`。

#### 下一轮建议

1. 实现真正的期间化标的归因：按账户计算 `ending_account_nav - beginning_account_nav - net_funding`，并单列证券持仓、现金/汇率、融资成本和估值校准。
2. IBKR 优先拆分现金/融资/FX 对 NAV 的影响，避免用证券毛盈亏解释净清算价值。
3. 继续补齐 `alipay`、`gf_7443`、`gf_9711` 账户余额或显式排除规则，降低 PortfolioState 数据质量 warning。

## 2026-06-23

### 本轮目标

- 检查后端对账链路是否能从交易入账、收支入账开始自动闭环。
- 优先修复低风险、高收益的自动化断点，不改前端 UI 和资产事实数据。

### 已修复问题

- 银行流水导入完成后，自动执行转账匹配和 `pams_cashflow_fact` 回填，避免新导入数据停留在 staging 表。
- 转账匹配结果同步写入统一对账表 `pams_reconciliation_case`，减少旧 `pams_bank_transfer_matches` 与页面消费表之间的口径漂移。
- 双边匹配的转账 case 标记为 `auto`；只有单边来源的 source-only 匹配标记为 `open`，避免把未到账的投资入金误判为已确认。
- `cashflow_fact` 回填增加 `source_uid` 去重，避免重导入导致同一流水被反复扫描为待事实化。

### 待确认问题

- 同花顺、理财通等投资账户的入金仍需要继续补全银行侧现金流与投资交易 fact 的 `cashflow_id` 绑定。
- 旧表 `pams_bank_transfer_matches` 仍保留兼容；后续需要确认是否可以降级为只读兼容层。
- source-only 投资入金进入 `open` 后，账户页和流水页的提示文案是否需要进一步区分“在途”与“待确认”。

### 风险项

- 本轮没有直接修改生产资产数据；只有在后续触发银行导入或转账匹配接口时，新逻辑才会写入 facts/cases。
- 当前工作区存在大量历史脏状态，本轮只触碰对账链路相关 5 个文件。

### 验证记录

- `python -m pytest tests/test_cashflow_fact_service.py tests/test_investment_trade_fact_service.py tests/test_bank_reconcile_matching.py tests/test_ledger_view_service.py tests/test_accounts_view_service.py tests/test_portfolio_valuation_linkage.py -q`
- `python -m py_compile backend/api/endpoints/bank_reconcile.py backend/api/services/cashflow_fact.py`
- `docker compose up -d --build pams-api`
- Smoke：`/api/bank-reconcile/transfer-matches?limit=1` 返回 200，`/api/ledger/workbench` 返回 200。

### 美股夜盘收益刷新专项

发现的问题：

- 美股/海外价格刷新仍按北京时间自然日写入快照；凌晨 0:00-7:00 的 US 市场数据会和持仓页 daily PnL 窗口错位。
- 海外持仓刷新按账户 bucket 取最新快照时，6/23 只有港股一行会遮挡 6/22 的 USD 完整持仓，导致 `market=us` 刷新找到 0 个标的。
- 当前本机 IB Gateway 已启动，但 4000/4001/4002/7497/7496 与 Client Portal 5000 在容器内均为 `Connection refused`；未登录或 API socket 未监听时，美股夜盘无法取到 IBKR 高可信价格。

已修复问题：

- 美股/海外刷新写入日期改为 07:00 北京时间 daily PnL 窗口日期，和持仓页、分析页今日收益标签保持一致。
- 海外刷新取数改为按市场 bucket 选择各自最新快照，避免港股/韩股/美股互相遮挡。
- IBKR 估算价进入持仓快照前增加与上一快照价格的 sanity guard，防止明显固定点尺度漂移。

验证记录：

- `python -m pytest tests/test_prices_refresh_endpoint.py tests/test_frontend_static_contract.py -q`
- `python -m pytest tests/test_portfolio_summary_service.py tests/test_return_attribution.py -q`
- `python -m py_compile backend/api/endpoints/prices.py`
- `docker compose up -d --build pams-api`
- Smoke：`/api/prices/refresh/us-stock?market=us&priceBasis=estimate` 可找到 7 个 USD 持仓；在 IBKR socket 未监听时拒绝 Yahoo fallback 写入。

## 2026-06-22

### 本轮目标

- 按低风险、高收益、小 diff 原则执行首轮自主改进。
- 不改 UI 主结构，不改资产事实数据，不改变收益/风控口径。
- 先补稳定性契约，再记录后续需要拆分的小修方向。

### 发现的问题

- 基金预估净值表 `pams_fund_nav_estimates` 已被持仓 API 和同步中心依赖，但正式 schema 目录缺少建表迁移，fresh DB 可能在基金刷新前访问持仓页失败。
- 自动同步链路对失败、partial、过期数据的判断不足，失败刷新后仍可能继续重建收益快照，需要后续拆成单独 PR。
- 历史投资交易折汇存在使用最新汇率的路径，后续应统一为交易日当日或之前最近汇率，并暴露 fallback 状态。
- data-quality 接口实际加载了大量明细数据，后续可改为聚合查询，降低页面预热成本。
- 前端通用 empty/error/loading 状态缺少可访问性语义，后续可小范围补 `role` 和 `aria-live`，不改视觉风格。

### 已修复问题

- 新增正式 schema 迁移 `067_fund_nav_estimates.sql`，让基金预估收益状态表不再只依赖运行时 DDL。
- 新增 schema 契约测试，锁定基金预估表的核心字段、唯一键和常用查询索引。
- 修正 `/api/fx-rates/latest` 顶层 `as_of`：从“第一行日期”改为所有返回币种里的最新日期，避免不同币种日期不一致时页面误读。
- 给通用 loading/error/empty 数据状态补 `role` 与 `aria-live`，并把空状态 badge 从“空”改为“暂无”，保持原视觉但提升可理解性。

### 待确认问题

- 同步调度器是否应在任一关键市场数据失败时跳过收益快照重建，还是允许 partial 但在页面显式标注。
- IBKR Flex 旧 statement 是否允许对齐到最新组合日期，需要设定最大容忍天数。
- NAV、账户页、收益页的 FX fallback 字段是否统一通过一个服务输出。

### 风险项

- 本轮未改生产资产数据；运行态只重建 `pams-api` 和 `pams-web` 以验证 API 与静态资源改动。
- 工作区存在大量历史脏状态，本轮只应提交 schema、FX 服务、前端通用状态、版本号、测试和本报告相关文件。

### 验证记录

- `python -m pytest tests/test_fx_rates_service.py tests/test_fund_nav_estimates_schema_contract.py tests/test_holdings_view_service.py tests/test_frontend_static_contract.py -q`
- `python -m py_compile backend/api/services/fx_rates.py backend/api/services/fund_nav_estimates.py backend/api/services/holdings_view.py`
- `node --check` 已覆盖本轮触碰的前端模块。
- `docker compose config --quiet`
- `docker compose up -d --build pams-api`
- `docker compose up -d --build pams-web`
- Smoke：`investments.html`、`ledger.html`、`more.html`、`/health`、`/api/fx-rates/latest?base_currency=CNY` 均返回 200。

### 下一轮优化建议

1. 给基金 NAV 刷新和市场数据刷新返回 `ok / partial / error`，并让调度器基于结果决定是否重建收益快照。
2. 优先修复历史交易折汇：所有交易 fact 写入路径按 `fx.as_of <= trade_date` 取最近汇率。
3. 将 `portfolio/data-quality` 改为聚合查询，避免页面启动时拉取全量明细。
4. 给通用错误和空状态补可访问性语义，并补保存按钮防重复提交。
