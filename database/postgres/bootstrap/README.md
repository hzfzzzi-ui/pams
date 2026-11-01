# 空数据库初始化

`manifest.txt` 是空数据库的固定执行顺序，不是升级清单；具体 SQL 位于 `database/postgres/schema/`。历史回填、历史删表文件没有列入；不创建旧策略、回测或信号对象；共享行情、行业兜底及独立回测应用读取的情绪和交易日历对象保留。清单包含原有账户、分类和基准标的默认种子，不导入交易、持仓或历史账单。

空库不创建旧 Telegram 任务队列或通知表，应用也不注册其 API；已有库的同名表和记录不会由此变更删除。`026` 不迁移退役机器人、模拟账户和策略对象，已有库清理及外部调用者迁移需要单独核查和授权。

空库不创建旧收支交易表、预算表及其分类规则表，也不从旧交易表回填收支；账户主数据由 `014_accounts.sql` 初始化。当前收支使用 `pams_cashflow_fact`。此清单不会修改或删除已有库中的对象或事实。

`scripts/bootstrap_empty_database.py` 只接受 `PAMS_BOOTSTRAP_DSN` 显式指定的数据库及 `--apply`；目标必须已由数据库所有者标记为 `pams-empty-bootstrap`，无业务表、视图或序列，也没有其他客户端连接。执行在单一事务中完成，失败回滚；脚本对四个 schema 文件的显式事务包装做受限处理，遇到独立行形式的未知事务语句拒绝运行。不要对已有库运行此入口。

```powershell
# 先由授权人员创建空库并设置 COMMENT ON DATABASE 为 pams-empty-bootstrap。
# PAMS_BOOTSTRAP_DSN 使用私有环境设置；不要将连接串写入仓库、日志或 PR。
python scripts/bootstrap_empty_database.py --apply
```

数据库标记和 advisory lock 是防误操作机制，不能代替权限隔离或阻止具备管理权限的其他连接；初始化期间必须独占空库。连接在提交响应时中断时，应由授权人员检查实际数据库状态，不应假定回滚或盲目重试。

## 本机开发库

使用单独启动的本机 PostgreSQL 16 服务和全新空库，避免复用现有应用的数据库服务。库名为 `pams_dev_` 加 32 位小写十六进制字符；由该专用数据库所有者将数据库 COMMENT 设为 `pams-development-sandbox`。连接串须显式提供回环地址、端口、库名和用户，不能依赖默认数据库或 libpq 服务配置。

在独立进程环境中设置 `PAMS_SOURCE_MODE=development` 和 `PAMS_DEV_BOOTSTRAP_DSN`，执行 `python scripts/bootstrap_development_database.py --apply`。入口仅接受有标记的空库并在一个事务内初始化；不能用于已有库升级或恢复数据。导入预检另需单独设置 32 字节十六进制 `PAMS_SOURCE_DEV_TOKEN_KEY`，与隔离测试密钥分开；需要发布到开发库时为开发进程显式设置 `PAMS_IMPORT_PUBLISH_ENABLED=true`。凭据和密钥不写入仓库或日志。

开发进程不要继承线上券商、行情凭据，不使用线上环境文件；只启动所需 API，不启动调度器或自动刷新。开发数据来自合成资料或经用户明确提供的资料。

下面示例从仓库根目录运行，需要 Docker、Python 依赖和 PostgreSQL 16 镜像。它创建独立临时容器和随机数据库，不读取现有 `.env`。请在专门的 PowerShell 进程中执行；示例密码只用于这次本机合成数据测试。

```powershell
# 删除本进程继承的数据库和券商配置；不更改系统环境。
Get-ChildItem Env: | Where-Object { $_.Name -match '^(PG|IBKR_|PAMS_|TUSHARE_|DATABENTO_)' } | ForEach-Object { Remove-Item -LiteralPath ("Env:" + $_.Name) }
$devId = [guid]::NewGuid().ToString('N')
$devContainer = "pams-dev-$devId"
$devPassword = [guid]::NewGuid().ToString('N')
docker run -d --name $devContainer --label pams-purpose=development --cpus 0.75 --memory 512m -e POSTGRES_USER=pams_dev_admin -e POSTGRES_PASSWORD=$devPassword -p 127.0.0.1::5432 postgres:16-alpine
if ($LASTEXITCODE -ne 0) { throw '开发数据库容器启动失败' }
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    docker exec $devContainer pg_isready -U pams_dev_admin -d postgres *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw '开发数据库未就绪' }
$portLine = docker port $devContainer 5432/tcp
if ($portLine -notmatch '^127\.0\.0\.1:(\d+)$') { throw '开发数据库端口不可用' }
$env:PGHOST = '127.0.0.1'
$env:PGPORT = $Matches[1]
$env:PGUSER = 'pams_dev_admin'
$env:PGPASSWORD = $devPassword
$env:PGDATABASE = "pams_dev_$devId"
docker exec $devContainer psql -U pams_dev_admin -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $($env:PGDATABASE)"
if ($LASTEXITCODE -ne 0) { throw '开发数据库创建失败' }
docker exec $devContainer psql -U pams_dev_admin -d postgres -v ON_ERROR_STOP=1 -c "COMMENT ON DATABASE $($env:PGDATABASE) IS 'pams-development-sandbox'"
if ($LASTEXITCODE -ne 0) { throw '开发数据库标记失败' }
$env:PAMS_SOURCE_MODE = 'development'
$env:PAMS_DEV_BOOTSTRAP_DSN = "host=$($env:PGHOST) port=$($env:PGPORT) dbname=$($env:PGDATABASE) user=$($env:PGUSER)"
python scripts/bootstrap_development_database.py --apply
if ($LASTEXITCODE -ne 0) { throw '开发数据库初始化失败' }
$env:PAMS_SOURCE_DEV_TOKEN_KEY = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
$env:PAMS_IMPORT_PUBLISH_ENABLED = 'true'
Push-Location backend/api
python -m uvicorn main:app --host 127.0.0.1 --port 8000
Pop-Location
```

API 使用完毕后按 Ctrl+C 退出；确认变量 `$devContainer` 仍指向本次创建的容器后，可运行 `docker rm -f $devContainer` 清理该容器及其中的开发数据库。容器停止后数据不会保留。示例只提供本地 API，不启动定时任务；开发模式拒绝外部券商及行情抓取。若已有本机服务占用 8000，调整示例中的 API 端口。

如需测试 IBKR 现金事件，必须另建专用开发证据目录，目录及其父路径、标记、清单和 XML 均不能是链接或 Windows junction。以当前随机开发库名作为标记文件 `.pams-development-sandbox` 的内容，并将合成日报清单与对应 XML 放在同一个目录：

```powershell
$evidence = Join-Path (Get-Location) "tmp/pams-dev-evidence-$devId"
New-Item -ItemType Directory -Path $evidence -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText((Join-Path $evidence '.pams-development-sandbox'), $env:PGDATABASE, [Text.UTF8Encoding]::new($false))
$env:PAMS_DEV_EVIDENCE_DIR = $evidence
# 将合成清单和对应 XML 放入 $evidence 后，在启动 API 前设置：
$env:PAMS_IBKR_DAILY_CLOSE_MANIFEST = Join-Path $evidence 'daily-close.json'
```

未配置目录、标记不匹配或路径越界时不读取证据；现金事件还需设置合成报告对应的 `PAMS_IBKR_CASH_EVENTS_AFTER`。目录访问检查不提供抵御恶意进程并发替换文件的原子保证，开发目录应由当前用户独占。

共享 `pams_ohlcv_daily` 原则上只有一个物理表。新空库保留 public 实表及其物理身份，以支持独立消费者的索引与冲突更新。独立行情脚本仍识别旧有 `pams_external_market` 实表及兼容视图，并拒绝两个物理表并存；如果已有视图未暴露共享字段，扩列脚本拒绝继续，需单独审查视图迁移。`026` 不迁移独立回测应用拥有的行情或 transaction lifecycle/sync log 表；数据版本表保留在 public。已有视图需单独核查，空库初始化不作为升级或修复入口。策略和模拟账户对象不属于空库初始化清单；已有生产对象的处理须单独审查并授权。

对生产库的迁移、删表或回填需要独立授权；本变更不执行这些操作。
