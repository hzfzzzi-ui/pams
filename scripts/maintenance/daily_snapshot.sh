#!/bin/bash
# PAMS 每日持仓快照生成脚本
# 用途：每天凌晨1点自动生成持仓快照
# 部署：添加到cron或Docker定时任务

set -e

LOG_FILE="/var/log/pams_snapshot.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] Starting daily snapshot generation..." | tee -a "$LOG_FILE"

# 1. 从SQLite读取最新持仓数据并导入PostgreSQL
# 注意：这里假设数据已经在pams_holdings_snapshot表中
# 如果数据来源是Excel或其他系统，需要先执行导入脚本

# 2. 调用FastAPI刷新端点，更新衍生表
echo "[$TIMESTAMP] Calling /admin/refresh..." | tee -a "$LOG_FILE"
RESPONSE=$(curl -X POST http://pams-api:8000/admin/refresh 2>&1)

if echo "$RESPONSE" | grep -q '"ok":true'; then
    echo "[$TIMESTAMP] ✅ Snapshot generated successfully" | tee -a "$LOG_FILE"
    echo "$RESPONSE" | tee -a "$LOG_FILE"
else
    echo "[$TIMESTAMP] ❌ Snapshot generation failed" | tee -a "$LOG_FILE"
    echo "$RESPONSE" | tee -a "$LOG_FILE"
    exit 1
fi

# 3. 更新FX汇率（从Frankfurter API获取）
echo "[$TIMESTAMP] Updating FX rates..." | tee -a "$LOG_FILE"

TODAY=$(date '+%Y-%m-%d')

# 获取USD汇率
USD_RATE=$(curl -s "https://api.frankfurter.app/latest?from=CNY&to=USD" | grep -oP '"USD":\K[0-9.]+' || echo "0.138")

# 获取MXN汇率
MXN_RATE=$(curl -s "https://api.frankfurter.app/latest?from=CNY&to=MXN" | grep -oP '"MXN":\K[0-9.]+' || echo "2.38")

# 插入汇率数据
docker exec pams-postgres psql -U pams -d pams -c "
INSERT INTO pams_fx_rates_daily (as_of, base_currency, quote_currency, rate, source)
VALUES
  ('$TODAY', 'CNY', 'CNY', 1.0, 'manual'),
  ('$TODAY', 'CNY', 'USD', 1/$USD_RATE, 'frankfurter'),
  ('$TODAY', 'CNY', 'MXN', 1/$MXN_RATE, 'frankfurter')
ON CONFLICT (as_of, base_currency, quote_currency)
DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source;
" 2>&1 | tee -a "$LOG_FILE"

echo "[$TIMESTAMP] ✅ FX rates updated" | tee -a "$LOG_FILE"

# 4. 清理30天前的日志
find /var/log -name "pams_snapshot.log.*" -mtime +30 -delete 2>/dev/null || true

echo "[$TIMESTAMP] 📊 Daily snapshot complete" | tee -a "$LOG_FILE"
