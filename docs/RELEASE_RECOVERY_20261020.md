# PAMS 恢复基线与同步发布规则

## 代码恢复

- 原工作区保留历史修改、账单、报表及回测实验，不批量暂存或覆盖。
- Git 历史、修改补丁和未跟踪文件清单在原工作区 `.codex-artifacts/recovery-20261020/`；清单不是未跟踪文件内容备份，原文件仍留在原目录。
- 稳定源码工作区：`C:/Users/hzfzzzi/.codex/worktrees/release-recovery/pams`。
- `recovery-20261020-baseline` 标签记录 PR #28–30 收敛后的源码基点；运行依赖及镜像验证见 PR #31。
- 原 21 条开放 PR 全部处理：#4–15、#17、#19、#20、#22、#27 按依赖合并。
- #16 与 #17 提交相同，关闭重复 #16。
- #23 的状态写入移植为 #29；#25 的 Databento 主功能已在 #20，其旧行情文件不覆盖后续修复。
- #26 的打包修复和旧入口清理由 #28 收敛，状态读取由 #30 收敛，保留当前前端交互。
- 干净检出的基线全量测试 451 项通过；后续同步可靠性测试另计。

## 刷新发布

1. 亚太北京时间 15:05，北美纽约时间 16:30，保留原时区与夏令时规则。
2. 网络及服务端临时错误最多请求三次；鉴权等非限流 4xx 不重试。
3. 汇率未写入、Flex 无报告日期、行情覆盖不全、基金缺净值或需人工核验时，停止该窗口的收益快照发布。
4. 亚太允许上一交易日 Flex；北美要求报告日期达到目标交易日。旧 Flex 不再强行对齐最新日期。
5. 美股收盘后的基金确认按实际 NAV 日期检查，不因北京时间跨日误判。
6. 分类快照失败或写入零行，不继续发布组合快照。已完成的单项刷新不整体回滚，此处不提供跨接口数据库事务。

## 重启与缺口

- `scheduler_state` 独立 Docker volume 保存窗口、目标日期、最近成功日期、失败步骤和最近 20 次记录。
- API 只读挂载并在同步中心显示状态；检查点不作为资产或收益事实来源。
- 成功窗口跨重启去重，失败窗口每 30 分钟重新检查。
- 启动检查最近到期任务，仅在当前价格 API 写入日期与目标日期兼容时执行补跑。
- 当前行情 API 不支持任意历史日期。过期窗口标记 `historical_backfill_required`，禁止拿当前价填历史价。
- 最新窗口成功不清除已发现的历史缺口；首次检查点的历史覆盖为 `unverified`，独立核验前不能标记完整。
- 周末按上一工作日处理。交易所特定休市日未引入新日历引擎，源日期不满足要求时保守阻断。
- 单个 scheduler 服务写检查点，不支持多个副本同时写同一文件。

## 部署限制

早期 Docker Desktop 因遗留 `dockerInference` socket 启动失败，后由用户重启恢复。2026-10-20 已完成本机部署和只读验收，保留原 `pams_pg_data` 卷、config/data/账单挂载，不曾删除数据卷。云端 CI 使用临时 PostgreSQL，不接触用户资产库。

生产仅执行 `069_daily_return_state.sql` 的三张空状态表建表；未批量执行历史账务 SQL。28 张既有 PAMS 表校验值保持一致。资产表备份在原工作区 recovery 目录，并已完整读取校验；全库及全 public 的两份备份因数据库容器重建而中断，文件名标记 `.incomplete`，不得作为可恢复备份使用。后续部署应用服务使用 `--no-deps`，避免不必要重建数据库及中断备份。

旧发布 NAV 已过期，新版读取当前计算草稿并保留旧发布来源信息，因此前后页面数值变化，不能承诺数值完全一致。此项是过期覆盖逻辑的既有修复生效，不是本轮改写资产数据。运行态补跑已阻断历史回填，检查点跨重启持久化；数据历史缺口尚未核准。完整测试与风险见 `NIGHTLY_REPORT.md`。

部署前先恢复 Docker，从稳定工作区构建。`.env` 通过 `--env-file` 指向原项目；密钥不进入 Git。

```powershell
docker compose --env-file C:/Users/hzfzzzi/openclaw/pams/.env config --quiet
docker build -t pams-pams-api -f backend/api/Dockerfile .
docker build -t pams-pams-web frontend
docker build -t pams-pams-scheduler backend/scheduler
```

镜像从稳定工作区构建。完成数据库备份与核验后，运行部署使用下面命令，让相对挂载路径仍指向原项目 config/data/账单，项目名 `pams` 及既有数据库卷保持一致：

```powershell
docker compose --project-directory C:/Users/hzfzzzi/openclaw/pams --env-file C:/Users/hzfzzzi/openclaw/pams/.env -f C:/Users/hzfzzzi/.codex/worktrees/release-recovery/pams/compose.yaml up -d --no-build pams-api pams-web pams-scheduler
```

不要使用 `--remove-orphans` 或删除原数据卷。配置与事实表迁移不在上述命令中自动执行。

启用生产调度前：保存数据库底稿、核对迁移版本、验证 `/health` 和正式页面、检查行情授权及历史缺口。本轮不自动执行历史账务迁移。
