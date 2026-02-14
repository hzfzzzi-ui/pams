# PAMS 清理后架构总览

**清理完成时间**: 2026-02-15 03:00:33

---

## ✨ 清理成果

### 归档文件统计

| 类别 | 大小 | 说明 |
|-----|------|------|
| 旧版代码 | 195KB | web/, api/, src/ |
| 重复配置 | 1.2MB | ingest/, import/, inbox/ |
| 测试文件 | 8KB | *.json 测试文件 |
| **总计** | **1.4MB** | 已归档到 archive/ |

### 清理效果
- ✅ 项目根目录从 22个 减少到 14个目录
- ✅ 所有旧版文件安全归档到 `archive/`
- ✅ 目录结构清晰，活跃代码分离
- ✅ Docker服务全部正常运行

---

## 📂 清理后的目录结构

```
pams/                              # 项目根目录
├── frontend/                      # ✅ 前端（活跃）
│   ├── public/
│   │   ├── assets/
│   │   │   ├── css/style.css
│   │   │   └── js/              # ES6 Modules
│   │   │       ├── api.js
│   │   │       ├── ui.js
│   │   │       ├── dashboard.js
│   │   │       ├── cashflow.js  # ⭐ 新增
│   │   │       ├── holdings.js
│   │   │       ├── strategy.js
│   │   │       └── watchlist.js
│   │   ├── index.html           # 总览页
│   │   ├── cashflow.html        # ⭐ 收支页（新）
│   │   ├── holdings.html
│   │   ├── strategy.html
│   │   └── watchlist.html
│   ├── Dockerfile
│   └── nginx.conf
│
├── backend/                       # ✅ 后端（活跃）
│   ├── api/
│   │   ├── main.py              # FastAPI入口
│   │   ├── endpoints/
│   │   │   └── transactions.py  # ⭐ 收支API（新）
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── services/
│   │   └── data-import/
│   │       └── import_balance_sheet.js
│   └── shared/
│       └── db.js
│
├── gateway/                       # ✅ 网关
│   └── Caddyfile
│
├── database/                      # ✅ 数据库Schema
│   ├── postgres/
│   │   └── schema/
│   │       └── 005_transactions.sql  # ⭐ 收支表（新）
│   └── sqlite/
│       └── schema.sql
│
├── config/                        # ✅ 配置
│   ├── docker/
│   │   └── compose.yaml
│   └── .env.example
│
├── scripts/                       # ✅ 工具脚本
│   ├── ingest/                  # 数据摄入
│   │   ├── config.json
│   │   ├── inbox/
│   │   └── queue/
│   ├── import/                  # 数据导入
│   │   └── firefly_bootstrap.sql
│   └── maintenance/             # 运维工具
│
├── telegram-bot/                  # ⏳ Telegram Bot
│   ├── Dockerfile
│   └── README.md
│
├── frontend-vue/                  # 🔮 Vue3版本（未来）
│   └── src/
│
├── data/                          # 📁 运行时数据
│   └── pams.sqlite3
│
├── docs/                          # 📚 文档
│   ├── DASHBOARD_SPEC.md
│   └── VUE3_MIGRATION_GUIDE.md
│
├── strategy/                      # 📈 投资策略
│   └── ITERATION_LOG.md
│
├── archive/                       # 📦 归档（新增）
│   ├── v1.0-legacy/             # 旧版代码
│   │   ├── web/
│   │   ├── api/
│   │   └── src/
│   ├── redundant/               # 重复配置
│   │   ├── ingest/
│   │   ├── import/
│   │   ├── inbox/
│   │   ├── Caddyfile
│   │   ├── schema.sql
│   │   └── DASHBOARD_SPEC.md
│   ├── test-files/              # 测试文件
│   │   ├── add-income.json
│   │   ├── test-transaction.json
│   │   ├── test-transactions.json
│   │   └── tx1.json
│   ├── README.md                # 归档说明
│   └── CLEANUP_REPORT_*.txt     # 清理报告
│
├── secrets/                       # 🔒 密钥
├── .claude/                       # 🤖 Claude配置
├── node_modules/                  # 📦 依赖
│
├── README.md                      # 项目说明
├── CURRENT_ARCHITECTURE.md        # 📐 架构文档
├── CLEANUP_PLAN.md                # 🗑️ 清理计划
├── ARCHITECTURE_CLEAN.md          # ✨ 清理后架构（本文档）
├── ARCHITECTURE.md                # 旧架构文档
├── CHANGELOG.md                   # 变更日志
├── VERSION_GUIDE.md               # 版本指南
├── cleanup-archive.sh             # 清理脚本
└── compose.yaml                   # Docker编排（根目录）
```

---

## 🏗️ 系统架构（清理后）

```
┌──────────────────────────────────────────────┐
│         用户设备 (iOS/PC)                     │
│       http://pams.local                      │
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│        DNS解析 (AdGuard Home)                │
│   pams.local → 192.168.0.143:80             │
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│          Caddy 网关 (:80)                    │
│  ┌────────────────────────────────────────┐ │
│  │  /api/*  → pams-api:8000              │ │
│  │  /*      → pams-web:80                │ │
│  └────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
         ↓                     ↓
   ┌──────────┐         ┌──────────┐
   │ pams-api │         │ pams-web │
   │ FastAPI  │         │  Nginx   │
   │  :8000   │         │   :80    │
   └────┬─────┘         └──────────┘
        ↓
   ┌──────────┐
   │PostgreSQL│
   │  :5432   │
   └──────────┘
```

---

## 🎯 核心服务状态

| 服务 | 状态 | 端口 | 代码位置 |
|-----|------|------|----------|
| **pams-web** | ✅ 运行 12分钟 | 内部80 | `frontend/` |
| **pams-api** | ✅ 运行 27分钟 | 内部8000 | `backend/api/` |
| **postgres** | ✅ 运行 4小时 | 5432 | - |
| **caddy** | ✅ 运行 4小时 | 80 | `gateway/` |
| **firefly** | ✅ 健康 4小时 | 内部8080 | - |
| **adguard** | ✅ 运行 4小时 | 53 | - |

---

## 📊 已实现功能

### 前端页面 ✅

| 页面 | URL | 功能 | 状态 |
|-----|-----|------|------|
| 总览 | `/` | 5 KPI + 资产结构 + NAV曲线 | ✅ 运行中 |
| **收支** | `/cashflow.html` | 月度统计 + 自动分类 + 饼图 | ✅ **新增** |
| 持仓 | `/holdings.html` | 持仓明细表格 | ✅ 运行中 |
| 策略 | `/strategy.html` | IPS参数 + 风险事件 | ✅ 运行中 |
| 观察池 | `/watchlist.html` | 观察标的列表 | ✅ 运行中 |

### 后端API ✅

| 端点分类 | 端点数量 | 状态 |
|---------|---------|------|
| 持仓管理 | 2 | ✅ |
| 资产汇总 | 2 | ✅ |
| 净值序列 | 3 | ✅ |
| **收支分析** | **4** | ✅ **新增** |
| 风险策略 | 2 | ✅ |
| 观察池 | 2 | ✅ |
| 资产管理 | 3 | ✅ |
| 管理端点 | 2 | ✅ |

### 数据库表 ✅

| 表名 | 用途 | 状态 |
|-----|------|------|
| pams_holdings_snapshot | 持仓快照 | ✅ |
| **pams_transactions** | **收支交易** | ✅ **新增** |
| pams_asset_class_map | 资产分类 | ✅ |
| pams_fx_rates_daily | 汇率 | ✅ |
| pams_nav_daily | 净资产 | ✅ |
| pams_risk_events | 风险事件 | ✅ |
| pams_policy | 投资策略 | ✅ |
| pams_watchlist | 观察池 | ✅ |

---

## 🎨 收支模块详解（新增）

### 数据流

```
用户添加交易
    ↓
POST /api/transactions
    ↓
auto_classify() 自动分类
    ↓
INSERT pams_transactions
    ↓
GET /api/transactions/monthly/2026-02
    ↓
聚合统计 (收入/支出/净额/分类)
    ↓
cashflow.js 渲染
    ↓
Chart.js 饼图 + 表格
```

### 自动分类引擎

```sql
-- 分类规则表
pams_category_rules (
  keyword,      # 关键词 (如 "午餐", "咖啡")
  category,     # 目标分类 (如 "餐饮")
  subcategory,  # 子分类 (如 "午餐")
  priority      # 优先级
)

-- 内置规则示例
"午餐" → 餐饮 > 午餐
"打车" → 交通 > 打车
"工资" → 收入 > 工资
```

### 月度统计

```sql
SELECT
  SUM(CASE WHEN amount > 0 THEN amount END) AS total_income,
  SUM(CASE WHEN amount < 0 THEN ABS(amount) END) AS total_expense,
  SUM(amount) AS net,
  category,
  SUM(ABS(amount)) AS category_total
FROM pams_transactions
WHERE TO_CHAR(date, 'YYYY-MM') = '2026-02'
GROUP BY category;
```

---

## 🔧 快速访问

### 前端链接
- 总览: http://pams.local/ 或 http://localhost:8080/
- 收支: http://pams.local/cashflow.html
- 持仓: http://pams.local/holdings.html
- 策略: http://pams.local/strategy.html
- 观察池: http://pams.local/watchlist.html

### API测试
```bash
# 资产汇总
curl http://localhost:8090/summary2/latest?baseCurrency=CNY

# 月度收支
curl http://localhost:8090/api/transactions/monthly/2026-02

# 最近交易
curl http://localhost:8090/api/transactions/recent?limit=10

# 健康检查
curl http://localhost:8090/health
```

---

## 📦 归档管理

### 查看归档内容
```bash
# 查看归档目录
ls -R archive/

# 查看清理报告
cat archive/CLEANUP_REPORT_*.txt
```

### 恢复文件
```bash
# 恢复单个文件
cp archive/test-files/add-income.json ./

# 恢复整个目录
cp -r archive/v1.0-legacy/web ./
```

### 删除归档（谨慎）
```bash
# 确认不再需要后执行
rm -rf archive/
```

---

## 🚀 下一步计划

### 短期任务
- [ ] 测试 Telegram Bot 完整流程
- [ ] 收支预算管理功能
- [ ] Firefly III 交易同步

### 中期任务
- [ ] Vue3 前端迁移
- [ ] WebSocket 实时推送
- [ ] 移动端原生应用

### 长期任务
- [ ] AI 驱动选股引擎
- [ ] 自动化交易执行
- [ ] 社区开源版本

---

## 📚 相关文档

- [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) - 完整架构文档
- [CLEANUP_PLAN.md](CLEANUP_PLAN.md) - 清理计划
- [archive/README.md](archive/README.md) - 归档说明
- [README.md](README.md) - 项目概述

---

**清理版本**: v1.0-clean
**最后更新**: 2026-02-15 03:00:33
**状态**: ✅ 生产运行中
