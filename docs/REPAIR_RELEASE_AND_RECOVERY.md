# 修复、发布与恢复

本主题规定问题预检、隔离演练、历史修正、发布身份和分层恢复。能力存在、候选无阻断、审批包、PR 合并、镜像上线、财务核准和用户验收分别记录。当前授权以用户明确指令及可追溯授权记录为准，历史样本及旧批准不能复用为新执行许可。

## 操作权限

| 操作 | 允许范围与前提 |
| --- | --- |
| 只读预检 | 服务器强制只读，当前选源及全量证据，不写业务/审计表 |
| 来源关联隔离演练 | 显式测试环境、完整冻结底稿、审批和前态匹配；仅改外层 source_id |
| 历史完整持仓预检 | 默认 dry-run，完整官方账户/业务日集合、稳定身份、精度、源哈希及前态 |
| 历史删除 | historical_delete_only，只删被完整证据和精确审批证明的额外行，匹配行逐字节保留，不插缺失项或重写价格/成本/数量 |
| 资金补缺隔离演练 | 银行与券商双侧证据、独立历史 FX、明确测试库/随机 schema/标记/审批；无生产 apply |
| 代码/镜像发布 | 明确当前范围授权，最终 head CI、配置/schema 兼容、候选与实际镜像核验 |
| 生产迁移/修账/恢复 | 与以上各项分别审批，不借开发、部署或旧历史批准执行 |

## 数据库初始化与升级

现有 `database/postgres/schema/` 混有历史迁移和回填，不可将目录中全部脚本作为空库初始化或已有库升级流程执行。部分 `public` 对象是兼容视图，早期脚本可能按表创建索引或修改约束；个别脚本重复执行成功不能推广到整套 schema。

[空库初始化 PR #90](https://github.com/hzfzzzi-ui/pams/pull/90) 提出显式空库清单及带标记和独占保护的初始化入口；以当前检出实际文件为准。已有库升级须针对现场结构设计独立迁移，在隔离环境验证后另按生产操作授权执行；部署镜像不自动授权迁移或删表。

## 策略与模拟对象的边界

策略和模拟账户不属于 PAMS 目标能力，独立回测应用也正在移除模拟账户。源代码、已部署镜像和数据库各自可能处于不同阶段；不能依据代码删除推断线上消费者已切换或生产表已删除。

资产风控的行业兜底读取 `pams_fundamental_snapshot`。当前源码的基准币种来自 `risk_limits.yaml`，旧运行版本可能仍引用 `pams_policy`，以实际镜像核对为准。独立回测市场快照读取 `pams_sentiment_state`，并引用 `pit_trade_cal`、`pit_st_history`、`pit_industry_history`、`pit_security_master`、`pams_ohlcv_daily` 和 `pit_index_daily`；共享部署须保留这些依赖。PAMS 兼容交易代码仍引用 `pams_transaction_sync_log`，独立回测还使用 `pams_transaction_lifecycle`，均不属于模拟账户删除范围。

对生产库删表或迁移需要单独授权，并在所有实际消费者切换后重新核查动态 SQL、跨应用依赖和现场结构；需要执行时须重新设计隔离验证和回退方案。

兼容视图上的普通读写、UPSERT 与建立索引是不同能力：隔离 PostgreSQL 16 对直连行情视图的插入和冲突更新可用，视图本身不能建索引；具体权限、视图定义和实际消费者仍须按现场核对，不能将此结果推广到任意视图。

## 来源关联预检

GET /api/ibkr/lineage-preflight 及 ibkr_lineage_preflight 只读比较已发布 Flex 原文/哈希/质量、原解析、完整来源与该来源 fact（含 raw_json.source_row）。不按 mtime 另选报告、不读当前价格或自行换汇。

预检按以下顺序处理：稳定源键、官方账户/执行编号及唯一性；账户/日期/标的/币种/方向、原币数量/价格/成交额/netCash；同币费用；内嵌旧 ID、历史数值及 make_source_hash；目标 ID 的占用者；发布质量。缺稳定身份、重复/缺失、非有限数、过期/legacy/沿用旧值、跨账户/币种和哈希变化均阻断。原币比较复用 1e-8，delta=actual−expected，不转换费用或重写旧金额、源哈希和 raw JSON。

目标已被其他 fact 占用时，即使对方在候选批次中，逐条预检仍阻断；不能自动分配临时 ID、松约束或安排置换。proposal 只列 source_id before/after，未来执行仍须重新取证和批准。

offset>=0，limit 为 1..500、默认 100；按稳定键/fact ID 排序。total、状态/原因数基于全量；link_mismatches 与阻断行分开，同一行多原因不能相加为错单数。逐行保留 fact/稳定键、旧/目标 ID、账户/执行/日期/币种、历史嵌入证据、不变量及原值/差额、旧 ID 指向和目标占用者。

candidate 仅当前无已知阻断，blocked 为有阻断，unchanged 为关联相同且无阻断；全局/逐条 executable=false、repair_implemented=false。缺表/缺报告/读失败返回 unavailable、counts=null，不伪装零差异。

连接首 SQL 前设置 REPEATABLE READ READ ONLY，SQL 超时 5 秒，结束再次核原文哈希并关闭。不 ensure/DDL、refresh、backfill、修复、插审计或改文件，异常只安全类型；GET 候选不能直接当过期执行计划。

## 隔离映射与历史证据

ibkr_lineage_rehearsal.plan_rehearsal 在同一发布原文及完整来源/fact 上复用既有预检与费用取证。原文 SHA、实际/应有日期须一致；过期/legacy、缺失、重复、数值/历史冲突或不完整范围拒绝。上限 500 行，分页截断不是完整批次。

规划包含逐条 fact/稳定键/旧新 ID/占用者、环/链/空闲目标组件，以及来源、事实、非 source_id 保护字段和投影整行确定指纹。保留重复，不作浮点近似或金额换算；占用者须在本批且目标唯一，批次外不能覆盖。费用证据不足不能预演；证据充分也不解除其他费用模型/结算警告。

始终 simulation_only=true、executable=false、production_write_supported=false、production_repair_ready=false。ready_for_isolated_rehearsal 只允许 scratch 试验。哈希只证明一致性，不是签名或授权。

写入辅助 tests/support/ibkr_rehearsal_support.py 不打入 API 镜像、不自行连库；连接必须匹配显式 PAMS_TEST_POSTGRES_DSN 的服务器/用户，随机 disposable 库名及隔离标记。普通应用库拒绝；这类标记防误用，不能抵御恶意管理员伪造。

1. 锁定 fact/来源表，重读全数据，核对规划、完整保护字段、源键对应目标和原文。旧缓存规划不能覆盖并发变化。
2. 同事务先赋唯一临时 source_id，再统一最终 ID，处理环/链且保持唯一约束；不删建 fact 或修改其他列。
3. 提交前核完整投影指纹。中断、锁冲突、附带字段变化、约束或提交失败均回滚，不留临时 ID。
4. 完整最终状态及来源/保护字段一致时重复执行返回 0；混合、部分完成或输入变化拒绝，不猜恢复进度。

内嵌 raw_json.source_row.source_id 保留入账时历史，外层关联变化后旧预检仍可能报 embedded_evidence_mismatch。不得改历史 JSON、源哈希或删检查来消除提示。

audit_evidence 仅解释确定投影，不改 projected_after_counts/reasons 或执行标志：complete_scope 要求原文、来源、fact 的稳定键多重集合完整且唯一；仅映射内 field=source_id、内嵌等于底稿旧值、外层等于对应目标且其他证据不变时，可解释 historical_reference_preserved。账户、执行或其他历史冲突不适用。

费用只有 unit_review.status=source_unit_verified 才区分来源单位证据，旧警告和 individual_cash_settlement_unverified 仍保留；可空 fee_currency 是否存在另按 [费用证据](BROKER_SYNC_AND_RECONCILIATION.md#费用的三个证据层次) 判定，不能把旧模型措辞推广到所有 schema。

review_rehearsal_evidence(baseline, plan, projected_facts) 从完整原底稿重建规划并核整批投影。缺底稿、部分完成、原始 JSON/时间戳/金额变化、调用者重签哈希的篡改规划均拒绝；返回副本，不改输入、不连库。即使 unresolved_evidence=0，仍 review 且 production_repair_ready=false。无可证明底稿/映射时不能仅凭字段相似认可历史 ID；真实错配、占用、费用、历史和全账核准问题继续独立存在。

## 来源修复工具与审批包

scripts/ibkr_source_link_repair.py 位于 API 镜像外，默认只读；只有 --rehearse 隔离模式，没有 --apply、--force、HTTP 写接口或调度注册。复用原选源/预检/费用证据/规划，不另建金融算法。070_ibkr_source_link_repair_audit.sql 仅供明确审批的隔离演练，CLI 不建表，生产只读预检不依赖该表。071 费用元数据迁移与它独立。

PAMS_REPAIR_DSN 必须显式设置，不回退应用 PG*，建议只读角色，不将含密码 DSN 放命令行或日志。只读输出 request 是审批包，绑定服务器/库/角色、报告 SHA/日期/发布 run、全来源/fact/保护列/投影指纹、全部映射、占用组件和风险。

```powershell
python scripts/ibkr_source_link_repair.py --raw-dir <有效Flex原文目录>
```

隔离执行先从冻结底稿恢复独立 PostgreSQL，名称 pams_ibkr_tx_test_<32位随机hex>；PAMS_TEST_POSTGRES_DSN 的 host/port/user 完全匹配该服务器；经人工核对设置数据库 comment pams-isolated-lineage-rehearsal，应用 070，并在该库生成审批包。审批文件只允许以下字段，所有 accepted_risks 原样接受：

```json
{
  "purpose": "isolated_source_link_rehearsal",
  "request_sha256": "<完整审批包哈希>",
  "approved_by": "<明确审批人>",
  "reference": "<仅隔离演练的依据>",
  "issued_at": "<带时区ISO时间>",
  "expires_at": "<最长24小时的带时区ISO时间>",
  "accepted_risks": ["<审批包原风险列表，不删减>"]
}
```

```powershell
python scripts/ibkr_source_link_repair.py --raw-dir <有效Flex原文目录> --rehearse --request <审批包.json> --approval <审批.json>
```

不能跨库、删阻断重签、仅批一部分或原文变化后继续。新鲜底稿、审批包和收据私有保管；声明不是签名、认证或防管理员篡改机制。

新鲜非 autocommit 连接先只读核隔离标记，再开写事务，不提交调用者旧事务。锁 fact/来源/报告发布记录/审计表，完整重推计划；在校验后、更新前、写审计前核有效期，保护列和投影整行严格一致。成功才写 pams_ibkr_source_link_repair_audit，存完整请求/批准、哈希、映射、数量、角色及时间；CLI 不改删审计，表所有者仍有权限，不能声称不可篡改。

中断、唯一约束、其他列副作用、审计/提交失败全回滚，不留假成功。异常仅安全类型；提交响应不明保持未核准，只在原审批有效且全部状态/原审计一致时确认重试。失败回滚不等于已提交批次的反向修复；后者须重新取证、核后续变化并另批，不能回灌生产凑相等。

## 历史持仓与资金补缺

ibkr_snapshot_alignment 用完整官方账户/业务日 XML 的稳定身份集合同时比较双表 extra/missing/matched，不能按名称/账户猜独立手工行归属。historical_delete_only 保留全部匹配行，仅审批删除被证明的额外项；source_sync 是另一个受信任完整来源入口，HTTP 参数或报表本身不是审批。其接入范围按代码、对应功能 PR 的发布记录及机器观察核对，不能从模式存在推定 collector 已替换。

正常同步只写来源业务日，过去的异日快照更正必须绑定账户、业务日、稳定源身份、原 sync 证据、精确 XML hash、新鲜前态和具体审批。先恢复实际 schema/FK/生成列到隔离库验证回滚/幂等，锁定重核前态后原子应用，仅改批准输入；生成 holding_pnl 和更正 updated_at 是预期派生/审计差异。漂移或意外差异整批中止，保留原行、写意图、审批、来源和提交收据。限定日期的批准不推广到其他快照、资金、费用、关联或 FX。

ibkr_funding_repair 的 PREVIEW 强制服务器只读；APPLY_ISOLATED 仅显式测试 DSN、随机 schema/标记、原请求、审批和前态均过才写，未提供生产 apply。输入含银行回单字段/原文 SHA、明确账户映射、Flex 原币到账与业务/报告日、稳定身份、独立历史 FX。遮罩账号/银行参考号不能补齐或等同券商账号/交易号；多候选、币种冲突、不同来源身份、越界日期和未知 FX 均阻断。

补缺银行 investment_funding 负额与券商 deposit 正额，复用两侧 fact 服务，保留备注与来源、绑定 cashflow_id，审计留 raw_json，不建表。它是内部资金链，不是收入/消费或全局 NAV 外部增值；不改证券数量/成本、不重复加已含入金现金、不以过期银行余额推当前余额。晚到报表/流水重新核候选、身份和前态，不能按旧审批盲加。

## 发布身份

scripts/verify_release.py 是宿主机只读工具，不向应用挂 Docker socket，仅 Docker inspect、列明文件 sha256sum、受限 schema SELECT 与 GET /health，无 build/up/restart、迁移、刷新、回填、修账或自动回退。

main 是集成代码，各组件可有不同来源 commit。核准清单描述预期，不能从未知现场自动反写；缺 revision 标签时用核准候选指纹与源码/CI 证据，不伪造标签。来源 commit 相同不证明镜像相同，候选/实际 ID、配置/挂载/schema 和健康分别核验。

| 清单字段 | 约束 |
| --- | --- |
| schema_version=1、release_id、integrated_commit | 私有核准清单身份 |
| components | container、image_id、source_commit、绝对容器路径 files→SHA256、runtime_policy_sha256 |
| runtime_policy | 挂载、端口、启动命令、用户、重启、privileged、CapAdd/Drop、SecurityOpt、只读根；不含环境值 |
| database | 显式 container/user/database、image_id、策略指纹；capabilities 仅 table/column，记录 schema/table/column、expected_exists、deferred 及理由 |
| health_urls | 仅本机或 pams.local 的 /health，无查询、凭据；拒绝重定向 |

清单来自核准产物和配置门禁，仅用户/授权发布者替换。SHA256 不是签名或防管理员篡改。只检查列明文件，敏感环境及未列范围另核；正常变化的 daily-close.json 不固定发布时 hash，其动态账户/日期/NAV/期间与原文由日结读取器校验，不可变历史 XML 保留。

输出独立 observed_at、镜像/容器/指纹/能力。identity_status 为 matched/drift/unknown；expected_exists 匹配才通过，批准延期为 deferred_by_approval。financial_verification=not_performed、user_acceptance=not_inferred；record_status=pending/synced/failed 与部署成败分开。

读取间容器更换/重启为 unknown，跨组件不是原子事务；镜像/权限/挂载未匹配不运行容器内命令，通过后按 inspect 的容器 ID 操作。失败生成新 unknown，不沿用旧绿灯，旧观察不能覆盖新记录。

```powershell
python scripts/verify_release.py --manifest <私有核准清单.json> --output-dir <私有记录目录>
python scripts/verify_release.py --manifest <私有核准清单.json> --check-report <私有记录目录>/latest.json
python scripts/verify_release.py --manifest <私有核准清单.json> --output-dir <私有记录目录> --record-pr <原功能PR号>
```

默认不写记录、不访问 GitHub；--output-dir 写历史并原子替换 latest。--check-report 检查清单指纹/结构，超过十分钟、未来时间、归档失败、非 matched 或不完整返回非零。--record-pr 只按同一批次标记更新当前用户中文评论，不覆盖他人或其他批次；GitHub 失败记 failed，复用同一批次核对归档，不重复部署。

同主机同批次 OS 互斥锁，退出自动释放，残留锁文件不等于锁仍存活；不抢存活锁，不支持跨主机分布式归档。报告不输出完整环境、密码或原始命令错误。获准的五分钟定时任务仅写本地报告，不每次发评论，工具不自行安装定时任务或常驻容器。

## 恢复边界

代码回退只涉及本轮提交，保留他人修改；镜像回退匹配 API/web/scheduler、配置、schema/API 兼容和缓存；数据恢复独立审批，不覆盖后续交易。备份记录时间、范围、校验和及隔离恢复结果，清单、视图定义、部分表或 .incomplete 归档不能冒充完整可恢复备份。副本需含相关底表、FK 与金融计算所需行情依赖，失败导出不登记成功。

## 验证与记录

只读预检、隔离恢复、事务回滚/幂等/并发、全字段投影、不变量及生产新鲜对照分别验证。新进程固定行情回放与生产九接口/九表不互相替代，不靠缓存凑相等，前后仅允许逐字段说明的差异。金融原文、逐笔映射和实际金额保留私有，测试用合成同类输入，样本数和特定 ID 不成为规则。

最终 head CI、环境全键值及核准变化白名单、实际镜像、健康、受影响页面和财务对照仍按 [工作流程](DEVELOPMENT_WORKFLOW.md) 执行。失败停止后续发布，不自动回灌；部署评论归原功能 PR，归档失败为“已部署、记录待同步”，不能重复部署。开发进度由 GitHub 维护，运行信息归对应部署评论及机器报告；既往过程和验收记录不作为实时控制面或 stable 证明。
