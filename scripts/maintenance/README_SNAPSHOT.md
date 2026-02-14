# PAMS 持仓快照管理

## 📌 问题背景

PAMS Dashboard需要历史持仓数据来绘制趋势图（NAV曲线、回撤分析等），但目前仅有1天的快照数据。

**目标**: 至少保留30天的历史快照数据

---

## 🚀 快速开始

### 选项1：生成模拟历史数据（测试用）⚡**最快**

如果暂时没有真实历史数据，可以基于当前快照生成模拟数据：

```bash
# 在项目根目录执行
docker exec pams-postgres psql -U pams -d pams -f /scripts/maintenance/backfill_history.sql
```

**效果**: 生成过去30天的模拟数据，市值随机波动±5%

### 选项2：从历史Excel导入（推荐）

如果有历史的资产负债表Excel文件：

```bash
cd import/

# 1. 将历史Excel导出为CSV（按日期命名）
# 例如：balance_2026-02-14.csv, balance_2026-02-13.csv...

# 2. 批量导入
for file in balance_2026-*.csv; do
  DATE=$(echo $file | grep -oP '\d{4}-\d{2}-\d{2}')
  echo "📥 Importing $file..."
  node ../src/import_balance_sheet.js "$file" --date "$DATE"
done

# 3. 刷新衍生表
curl -X POST http://pams.local/api/admin/refresh
```

---

## ⏰ 设置每日自动快照

### 方法A：使用宿主机Cron（推荐Windows）

1. **赋予脚本执行权限**（Linux/WSL）:
   ```bash
   chmod +x scripts/maintenance/daily_snapshot.sh
   ```

2. **添加到Windows任务计划程序**:
   - 打开"任务计划程序"
   - 创建基本任务
   - 名称: "PAMS Daily Snapshot"
   - 触发器: 每天凌晨1:00
   - 操作: 启动程序
     - 程序: `bash`
     - 参数: `-c "C:\Users\hzfzzzi\openclaw\pams\scripts\maintenance\daily_snapshot.sh"`

3. **验证任务**:
   ```bash
   # 手动运行一次测试
   bash scripts/maintenance/daily_snapshot.sh

   # 查看日志
   tail -f /var/log/pams_snapshot.log
   ```

### 方法B：使用Docker容器Cron（推荐Linux）

1. **修改bot容器添加cron任务**:

   编辑 `config/docker/compose.yaml`:
   ```yaml
   pams-bot:
     build:
       context: ../../telegram-bot
     container_name: pams-bot
     restart: unless-stopped
     volumes:
       - ../../scripts/maintenance:/scripts
     environment:
       # ... 现有环境变量
     # 添加定时任务
     entrypoint: |
       sh -c '
         echo "0 1 * * * /scripts/daily_snapshot.sh" | crontab -
         crond -f
       '
   ```

2. **重启容器**:
   ```bash
   cd config/docker
   docker compose up -d pams-bot
   ```

### 方法C：使用GitHub Actions（云端备份）

如果项目托管在GitHub，可以使用Actions每日触发：

创建 `.github/workflows/daily-snapshot.yml`:
```yaml
name: Daily Snapshot Backup

on:
  schedule:
    - cron: '0 17 * * *'  # UTC 17:00 = 北京时间 1:00
  workflow_dispatch:  # 手动触发

jobs:
  snapshot:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger PAMS snapshot
        run: |
          curl -X POST https://your-pams-public-url/api/admin/refresh
          # 或通过SSH连接到服务器执行脚本
```

---

## 📊 验证历史数据

```sql
-- 检查有多少天的数据
SELECT COUNT(DISTINCT as_of) AS days FROM pams_holdings_snapshot;

-- 查看每天的记录数
SELECT
    as_of,
    COUNT(*) AS records,
    ROUND(SUM(market_value)::numeric, 2) AS total_market_value
FROM pams_holdings_snapshot
GROUP BY as_of
ORDER BY as_of DESC
LIMIT 30;

-- 查看汇率数据覆盖情况
SELECT
    as_of,
    COUNT(*) AS currency_pairs
FROM pams_fx_rates_daily
WHERE base_currency = 'CNY'
GROUP BY as_of
ORDER BY as_of DESC
LIMIT 10;
```

---

## 🔧 故障排查

### 问题1: Cron任务未执行

```bash
# 检查cron服务状态
systemctl status cron  # Linux
# 或查看Windows任务计划程序的历史记录

# 查看日志
tail -f /var/log/pams_snapshot.log
```

### 问题2: 汇率获取失败

```bash
# 手动测试Frankfurter API
curl "https://api.frankfurter.app/latest?from=CNY&to=USD"

# 如果API不可用，使用手动汇率
docker exec pams-postgres psql -U pams -d pams -c "
INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
VALUES
  (CURRENT_DATE, 'CNY', 'CNY', 1.0, 'manual'),
  (CURRENT_DATE, 'CNY', 'USD', 7.25, 'manual'),
  (CURRENT_DATE, 'CNY', 'MXN', 0.42, 'manual')
ON CONFLICT DO NOTHING;
"
```

### 问题3: 快照数据为空

```bash
# 检查数据源
docker exec pams-postgres psql -U pams -d pams -c "SELECT COUNT(*) FROM pams_holdings_snapshot WHERE as_of = CURRENT_DATE;"

# 如果为空，检查导入脚本是否正常运行
node src/import_balance_sheet.js import/balance_sheet_export.csv
```

---

## 📅 维护计划

**每日任务** (自动):
- ✅ 1:00 AM: 生成持仓快照
- ✅ 1:05 AM: 更新FX汇率
- ✅ 1:10 AM: 刷新衍生表 (`/admin/refresh`)

**每周任务** (手动):
- 🔍 检查快照数据完整性
- 🗂️ 备份数据库

**每月任务** (手动):
- 📈 审查历史趋势
- 🧹 清理过期日志
- 💾 导出月度报表

---

## 🎯 下一步优化

1. **实时价格更新** - 集成行情API（新浪财经/腾讯财经）
2. **智能数据填充** - 从券商/银行API自动同步
3. **异常检测** - 市值突变告警
4. **数据备份** - 自动备份到OSS/S3

---

**创建日期**: 2026-02-15
**维护者**: Claude Sonnet 4.5
**相关文档**: [PROJECT_ANALYSIS_2026-02-15.md](../../docs/PROJECT_ANALYSIS_2026-02-15.md)
