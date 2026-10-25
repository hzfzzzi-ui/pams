# 发布身份与状态记录

## 判定依据

`main` 是集成代码，不代表全部组件使用同一源码或全部能力已启用。
运行版本由当前容器镜像 ID、实际文件指纹和配置观察证明。
核准清单描述预期，不能从未知线上状态反写预期来使核验通过。
当前旧镜像没有 revision 标签时，使用已核准候选指纹及源码/CI 证据，不伪造标签。

`scripts/verify_release.py` 是宿主机只读工具，不挂 Docker socket 给应用。
只允许 Docker inspect、列明文件的 sha256sum、受限 schema SELECT 和 GET /health。
没有 build/up/restart、迁移、同步、回填、修账或自动回退命令。

## 输入与输出

输入是私有核准 JSON，不提交包含主机路径的实际生产清单：

- schema_version=1、release_id、integrated_commit。
- components：每项 container、image_id、source_commit、files（绝对容器路径到 SHA256）、runtime_policy_sha256。
- runtime_policy 包含挂载、端口、启动命令、用户、重启策略、privileged、CapAdd/CapDrop、SecurityOpt 和只读根文件系统；不包含环境变量值。
- database：显式 container/user/database、image_id 和 runtime_policy_sha256；capabilities 只允许 table/column，列明 schema/table/column、expected_exists、deferred 和暂缓理由。
- health_urls：仅本机或 pams.local 的 /health，无查询参数、凭据或刷新路径。

清单须来自经核准的候选产物/源码证据及既有配置门禁，不提供自动认可线上状态的命令。
每组件允许不同 source_commit；不能用当前 main SHA 强行要求无变化组件重新构建。
原始清单只有用户或获授权发布者可替换；SHA256 是一致性指纹，不是数字签名或防管理员篡改机制。

输出独立记录 observed_at、镜像、容器、源指纹核对和能力；状态如下：

| 类别 | 状态含义 |
| --- | --- |
| identity_status | matched / drift / unknown；异常不能沿用上一次成功 |
| 能力 | expected_exists 与实查一致才匹配；批准暂缓标 deferred_by_approval，不假称已启用 |
| 财务核准 | 始终 not_performed，不能借身份检查核准 NAV/收益 |
| 用户验收 | not_inferred，不自动建立 stable |
| record_status | pending / synced / failed，与是否部署成功分开 |

只检查清单列明文件，不声明未列文件或敏感环境值已核验；这些仍按原发布门禁单独检查。
容器在读取间更换或重启时返回 unknown。快照是观测区间证据，不是跨服务原子事务。
镜像、权限和挂载未匹配时不执行容器内命令；通过后使用 inspect 得到的容器 ID，防止名称被替换。
健康检查拒绝重定向，不能把其他主机或非 health 路径的响应当作就绪。

## 使用

```powershell
python scripts/verify_release.py --manifest <私有核准清单.json> --output-dir <私有记录目录>
python scripts/verify_release.py --manifest <私有核准清单.json> --check-report <私有记录目录>/latest.json
python scripts/verify_release.py --manifest <私有核准清单.json> --output-dir <私有记录目录> --record-pr <原功能PR号>
```

默认不写记录、不访问 GitHub；--output-dir 显式写独立历史及原子替换 latest.json。
--check-report 核对清单指纹及报告结构；超过十分钟、未来时间、归档失败、非 matched 或不完整证据返回非零。
Docker/查询失败会生成新 unknown，不保留假绿灯；较早观察不能覆盖较新 latest。
--record-pr 显式生成中文评论，同一批次只更新当前 GitHub 用户的同一标记，不覆盖别人的评论或其他批次。
GitHub 失败则 record_status=failed、非零退出；不因此重复部署，使用同一批次重新核对后重试归档。
同一宿主机的同批次归档使用互斥锁；拒绝覆盖较新评论。锁不是跨主机分布式锁，不支持多台发布机并发归档。
使用宿主机 OS 锁，进程异常终止时操作系统自动释放所有权；残留锁文件不代表锁仍被持有，不自动抢占存活发布进程。
报告不输出完整环境变量、数据库密码或命令错误正文。

部署前后必须新鲜核对，发现差异停止后续发布，不自动修改预期或恢复数据库。
平时可在获准的宿主机定时任务中每五分钟运行同一脚本，仅写本地报告；不每次发 GitHub 评论。
定时任务和工具部署独立记录，不新增常驻容器；脚本本身不安装定时任务。

## 文档职责

DEVELOPMENT_STATE 保留阶段、验收、最近核验快照时间与运行报告入口，不把 Git 文档写成实时控制面。
NIGHTLY_REPORT 记录结论、异常与待办；原功能 PR 评论由同一份观察结果生成。
部署记录不单开 PR。Git 文档尚未随下轮合入时明确待归入；不能把滞后快照称当前现场。
发布前 CI、配置/秘密值核对、资产服务发布后的九表九接口和 UI 验证仍独立执行，身份匹配不替代它们。
