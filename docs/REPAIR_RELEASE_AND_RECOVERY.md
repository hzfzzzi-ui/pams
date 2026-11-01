# 修复、发布与恢复

本主题规定问题预检、隔离演练、历史修正、发布身份和分层恢复。能力存在、候选无阻断、审批包、PR 合并、镜像上线、财务核准和用户验收分别记录。当前授权以用户明确指令及可追溯授权记录为准，历史样本及旧批准不能复用为新执行许可。

## 操作权限

| 操作 | 允许范围与前提 |
| --- | --- |
| 只读预检 | 服务器强制只读，当前选源及全量证据，不写业务/审计表 |
| 资金补缺隔离演练 | 银行与券商双侧证据、独立历史 FX、明确测试库/随机 schema/标记/审批；无生产 apply |
| 代码/镜像发布 | 明确当前范围授权，最终 head CI、配置/schema 兼容、候选与实际镜像核验 |
| 生产迁移/修账/恢复 | 与以上各项分别审批，不借开发、部署或旧历史批准执行 |

## 数据库初始化与升级

现有 `database/postgres/schema/` 混有历史迁移和回填，不可将目录中全部脚本作为空库初始化或已有库升级流程执行。部分 `public` 对象是兼容视图，早期脚本可能按表创建索引或修改约束；个别脚本重复执行成功不能推广到整套 schema。

[空库初始化 PR #90](https://github.com/hzfzzzi-ui/pams/pull/90) 提出显式空库清单及带标记和独占保护的初始化入口；以当前检出实际文件为准。已有库升级须针对现场结构设计独立迁移，在隔离环境验证后另按生产操作授权执行；部署镜像不自动授权迁移或删表。

## 策略与模拟对象的边界

策略和模拟账户不属于 PAMS 目标能力。独立回测应用及其数据由该应用管理；源代码、已部署镜像和数据库各自可能处于不同阶段，不能依据代码删除推断线上消费者已切换或生产表已删除。

资产风控的行业兜底读取 `pams_fundamental_snapshot`。当前源码的基准币种优先来自 `application.yaml`，旧配置仍可从 `risk_limits.yaml` 回退；旧运行版本可能仍引用 `pams_policy`，以实际镜像核对为准。独立回测市场快照读取 `pams_sentiment_state`，并引用 `pit_trade_cal`、`pit_st_history`、`pit_industry_history`、`pit_security_master`、`pams_ohlcv_daily` 和 `pit_index_daily`；共享部署须保留这些依赖。旧检出可能仍引用 `pams_transaction_sync_log`；当前主线已移除 Telegram 专属同步代码。独立回测使用的交易生命周期对象须按该应用实际运行状态核对，不因 PAMS 清理而删除。

对生产库删表或迁移需要单独授权，并在所有实际消费者切换后重新核查动态 SQL、跨应用依赖和现场结构；需要执行时须重新设计隔离验证和回退方案。

兼容视图上的普通读写、UPSERT 与建立索引是不同能力：隔离 PostgreSQL 16 对直连行情视图的插入和冲突更新可用，视图本身不能建索引；具体权限、视图定义和实际消费者仍须按现场核对，不能将此结果推广到任意视图。

## 历史持仓与资金补缺

正常同步只写来源业务日，过去的异日快照更正必须绑定账户、业务日、稳定源身份、原 sync 证据、精确 XML hash、新鲜前态和具体审批。先恢复实际 schema/FK/生成列到隔离库验证回滚/幂等，锁定重核前态后原子应用，仅改批准输入；生成 holding_pnl 和更正 updated_at 是预期派生/审计差异。漂移或意外差异整批中止，保留原行、写意图、审批、来源和提交收据。限定日期的批准不推广到其他快照、资金、费用、关联或 FX。



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
