# PAMS 当前架构文档

**Personal Asset Management System - 个人资产管理系统**

版本: v1.0 (2026-02-15)
状态: 生产运行中
部署方式: Docker Compose + LAN-only

---

## 🏗️ 系统架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    用户设备 (iOS/PC)                         │
│              http://pams.local (LAN访问)                    │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│               DNS解析层 (AdGuard Home)                       │
│          pams.local → 192.168.0.143:80                      │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│              网关层 (Caddy :80)                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  /api/*        → pams-api:8000  (FastAPI)          │  │
│  │  /*            → pams-web:80    (Nginx)            │  │
│  │  firefly.*     → firefly:8080   (Firefly III)     │  │
│  │  adguard.*     → adguard:80     (AdGuard)         │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
              ↓                    ↓
    ┌──────────────┐      ┌──────────────┐
    │  应用层      │      │  前端层      │
    │              │      │              │
    │  pams-api    │      │  pams-web    │
    │  (FastAPI)   │      │  (Nginx)     │
    │  Port: 8000  │      │  Port: 80    │
    └──────────────┘      └──────────────┘
              ↓                    ↓
    ┌──────────────┐      ┌──────────────┐
    │  数据层      │      │  前端资源    │
    │              │      │              │
    │  PostgreSQL  │      │  index.html  │
    │  Port: 5432  │      │  cashflow.   │
    │              │      │  holdings.   │
    │  Firefly DB  │      │  strategy.   │
    │  pams DB     │      │  watchlist.  │
    └──────────────┘      └──────────────┘
```

---

## 📦 容器服务清单

| 服务名 | 镜像 | 端口 | 功能 | 状态 |
|--------|------|------|------|------|
| **caddy** | caddy:2 | 80:80 | 反向代理、路由 | ✅ 运行中 |
| **pams-web** | nginx:1.27-alpine | 内部80 | 前端静态托管 | ✅ 运行中 |
| **pams-api** | python:3.11-slim | 内部8000 | FastAPI后端 | ✅ 运行中 |
| **postgres** | postgres:16 | 5432:5432 | 主数据库 | ✅ 运行中 |
| **firefly** | fireflyiii/core | 内部8080 | 记账系统 | ✅ 运行中 |
| **firefly-cron** | alpine | - | 定时任务 | ✅ 运行中 |
| **adguard** | adguard/adguardhome | 53:53 | DNS服务 | ✅ 运行中 |

---

## 🔄 数据流架构

### 1. 前端请求流

```
用户浏览器
  ↓ http://pams.local/
Caddy 网关
  ↓ 路由到 pams-web:80
Nginx 静态服务器
  ↓ 返回 index.html + assets/js/*.js
浏览器加载
  ↓ ES6 Module: import {jget} from './js/api.js'
API调用
  ↓ fetch('/summary2/latest')
Caddy 网关
  ↓ 路由到 pams-api:8000
FastAPI 后端
  ↓ 查询 PostgreSQL
返回JSON数据
  ↓
前端渲染 Chart.js 图表
```

### 2. 收支数据流

```
用户添加交易
  ↓ POST /api/transactions
FastAPI: transactions.py
  ↓ auto_classify() - 自动分类引擎
  ↓ INSERT INTO pams_transactions
PostgreSQL: pams_transactions表
  ↓ GET /api/transactions/monthly/2026-02
FastAPI: 聚合查询
  ↓ SUM(amount) GROUP BY category
返回月度统计
  ↓
前端: cashflow.js
  ↓ renderCategoryChart()
Chart.js饼图渲染
```

### 3. 资产数据流

```
手动导入/API同步
  ↓
pams_holdings_snapshot (PostgreSQL)
  ↓ 触发衍生表计算
pams_asset_class_map (分类映射)
  ↓ 汇率转换
pams_fx_rates_daily
  ↓ 汇总计算
pams_nav_daily (净资产)
  ↓ 风控检查
pams_risk_events
  ↓ API: /summary2/latest
Dashboard显示
```

---

## 🗃️ 数据库架构

### PostgreSQL Schema

#### 核心表

```sql
-- 持仓快照表
pams_holdings_snapshot (
  venue, account, type, symbol, name,
  currency, market_value, holding_pnl, daily_pnl,
  as_of, created_at
)

-- 收支交易表 (新增)
pams_transactions (
  id, date, amount, currency,
  category, subcategory, account,
  description, location,
  auto_classified, confidence,
  budget_id, source, created_at
)

-- 资产分类映射
pams_asset_class_map (
  venue, account, type, symbol,
  asset_class, priority, active
)

-- 汇率表
pams_fx_rates_daily (
  as_of, pair, rate, source
)

-- 净资产表
pams_nav_daily (
  as_of, total_market_value,
  holdings_count, venues_count,
  completeness, updated_at
)

-- 风险事件表
pams_risk_events (
  as_of, severity, code, title,
  details, created_at
)

-- 投资政策表
pams_policy (
  key, value JSONB, updated_at
)

-- 观察池
pams_watchlist (
  market, symbol, name, thesis,
  status, created_at, updated_at
)
```

#### 资产分类体系

```
Level 1: asset_class (资产大类)
  ├── CN_EQUITY       # A股权益
  ├── US_EQUITY       # 美股权益
  ├── FIXED_INCOME    # 固收理财
  ├── CRYPTO          # 加密货币
  ├── CASH            # 现金
  ├── ESOP            # 员工持股
  ├── GOLD_COMMODITIES # 黄金商品
  ├── LIABILITY       # 负债
  └── IGNORE          # 忽略项

Level 2: venue (交易所/平台)
  ├── guosen          # 国信证券
  ├── tencent_licaitong # 腾讯理财通
  ├── ibkr            # 盈透证券
  ├── binance         # 币安
  └── manual          # 手动录入

Level 3: account (具体账户)
  ├── cn_a_rmb        # A股人民币账户
  ├── us_main         # 美股主账户
  └── ...

Level 4: symbol (单一标的)
  ├── 601899          # 紫金矿业
  ├── BTC             # 比特币
  └── ...
```

---

## 📁 目录结构

### 活跃代码目录

```
pams/
├── frontend/                      # 前端应用 ✅
│   ├── public/
│   │   ├── assets/
│   │   │   ├── css/style.css
│   │   │   └── js/
│   │   │       ├── api.js         # API调用封装
│   │   │       ├── ui.js          # UI组件库
│   │   │       ├── dashboard.js   # 总览页逻辑
│   │   │       ├── cashflow.js    # 收支页逻辑
│   │   │       ├── holdings.js    # 持仓页逻辑
│   │   │       ├── strategy.js    # 策略页逻辑
│   │   │       └── watchlist.js   # 观察池逻辑
│   │   ├── index.html             # 总览页
│   │   ├── cashflow.html          # 收支页 (新)
│   │   ├── holdings.html          # 持仓页
│   │   ├── strategy.html          # 策略页
│   │   └── watchlist.html         # 观察池页
│   ├── Dockerfile
│   └── nginx.conf
│
├── backend/                       # 后端应用 ✅
│   ├── api/
│   │   ├── main.py                # FastAPI主入口
│   │   ├── endpoints/
│   │   │   └── transactions.py    # 收支API (新)
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── services/
│   │   └── data-import/
│   │       └── import_balance_sheet.js
│   └── shared/
│       └── db.js
│
├── gateway/                       # 网关配置 ✅
│   └── Caddyfile
│
├── database/                      # 数据库Schema ✅
│   ├── postgres/
│   │   └── schema/
│   │       └── 005_transactions.sql
│   └── sqlite/
│       └── schema.sql
│
├── config/                        # 配置文件 ✅
│   ├── docker/
│   │   └── compose.yaml           # Docker编排
│   └── .env.example
│
├── scripts/                       # 脚本工具 ✅
│   ├── ingest/                    # 数据摄入
│   ├── import/                    # 数据导入
│   └── maintenance/               # 运维工具
│
├── telegram-bot/                  # Telegram Bot ⏳
│   ├── Dockerfile
│   └── README.md
│
├── data/                          # 运行时数据 ✅
│   └── pams.sqlite3
│
├── docs/                          # 文档 ✅
│   ├── DASHBOARD_SPEC.md
│   └── VUE3_MIGRATION_GUIDE.md
│
├── strategy/                      # 策略文档 ✅
│   └── ITERATION_LOG.md
│
└── frontend-vue/                  # Vue3版本 (未来) 🔮
    └── src/
```

---

## 🌐 API端点清单

### FastAPI 端点 (pams-api:8000)

#### 持仓管理
```
GET  /holdings/latest              # 最新持仓快照
GET  /holdings/as-of?date={date}   # 指定日期持仓
```

#### 资产汇总
```
GET  /summary/latest               # 按type汇总 (旧)
GET  /summary2/latest              # 按asset_class汇总 (新)
     ?baseCurrency=CNY
     &venue={venue}
     &account={account}
```

#### 净值时间序列
```
GET  /nav/recent?limit={n}         # 最近N天NAV
GET  /nav/series?limit={n}         # NAV曲线数据
     &baseCurrency=CNY
GET  /nav/drawdown?limit={n}       # 回撤曲线
```

#### 收支分析 (新增)
```
POST /api/transactions             # 创建交易记录
     Body: {amount, category, date, description, ...}

GET  /api/transactions/recent      # 最近交易
     ?limit={n}

GET  /api/transactions/monthly/{month}  # 月度统计
     返回: {total_income, total_expense, net, by_category}

GET  /api/transactions/categories  # 所有分类列表
```

#### 风险与策略
```
GET  /risk/latest?limit={n}        # 风险事件
GET  /policy/ips                   # IPS策略参数
```

#### 观察池
```
GET  /watchlist?market={CN|US}     # 观察池列表
     &status={candidate|active|archived}
POST /watchlist                    # 添加观察项
```

#### 资产管理
```
GET  /assets/search?q={term}       # 资产搜索
GET  /assets/map                   # 资产映射表
POST /assets/map                   # 更新映射
```

#### 变动分析
```
GET  /delta/latest?mode={abs|up|down}  # 资产变动排序
```

#### 管理端点
```
POST /admin/refresh                # 刷新衍生表
GET  /health                       # 健康检查
```

---

## 🎨 前端页面功能

### 1. 总览页 (index.html)

**URL**: http://localhost:8080/

**功能模块**:
- 5个KPI卡片: 总资产、总负债、净资产、最大回撤、持仓数
- 资产结构饼图 + 明细列表
- 负债结构饼图
- 净资产曲线图 (90天)
- 近期收支柱状图 (30天)
- 风险告警面板
- 系统状态面板

**数据源**:
- `/summary2/latest?baseCurrency=CNY`
- `/holdings/latest`
- `/nav/series?limit=90`
- `/cashflow/series?limit=30`
- `/risk/latest?limit=10`

### 2. 收支页 (cashflow.html) ✨ 新增

**URL**: http://localhost:8080/cashflow.html

**功能模块**:
- 月份选择器 (上月/下月切换)
- 3个KPI卡片: 总收入、总支出、净额
- 支出分类饼图
- 最近30条交易记录表格

**数据源**:
- `/api/transactions/monthly/{month}`
- `/api/transactions/recent?limit=30`

**特色功能**:
- ✅ 自动分类引擎 (关键词匹配)
- ✅ 按月统计聚合
- ✅ 分类占比可视化
- ✅ 交易记录表格展示

### 3. 持仓页 (holdings.html)

**URL**: http://localhost:8080/holdings.html

**功能模块**:
- 完整持仓表格
- 按type筛选
- 显示: 平台、账户、类型、代码、名称、市值、盈亏

**数据源**:
- `/holdings/latest`

### 4. 策略页 (strategy.html)

**URL**: http://localhost:8080/strategy.html

**功能模块**:
- IPS策略参数卡片
- NAV/回撤双轴图
- 风险事件列表
- 调试JSON面板

**数据源**:
- `/policy/ips`
- `/nav/series`
- `/nav/drawdown`
- `/risk/latest`

### 5. 观察池 (watchlist.html)

**URL**: http://localhost:8080/watchlist.html

**功能模块**:
- 观察标的列表
- 市场筛选 (CN/US)
- 状态管理 (candidate/watch/buy)
- 投资论点展示

**数据源**:
- `/watchlist`

---

## 🔐 网络与安全

### LAN-only 部署

```
网络模式: bridge (Docker内部网络)
对外端口: 仅80 (Caddy)
内部通信: 服务名解析 (pams-api, postgres, etc.)
DNS解析: AdGuard Home (192.168.0.143:53)
域名: pams.local, firefly.pams.local, adguard.pams.local
```

### 数据隐私

- ✅ 完全本地部署，无云依赖
- ✅ 数据存储在本地PostgreSQL
- ✅ 仅局域网访问，无公网暴露
- ✅ .env文件包含敏感配置 (gitignore)

---

## 📊 关键技术栈

### 后端技术

| 组件 | 版本 | 用途 |
|-----|------|------|
| Python | 3.11 | 主开发语言 |
| FastAPI | 最新 | RESTful API框架 |
| Uvicorn | 最新 | ASGI服务器 |
| asyncpg | 最新 | PostgreSQL异步驱动 |
| psycopg2 | 2.9.9 | PostgreSQL同步驱动 |
| Pydantic | 最新 | 数据验证 |

### 前端技术

| 组件 | 版本 | 用途 |
|-----|------|------|
| HTML5 | - | 页面结构 |
| ES6 Modules | - | 模块化JS |
| Chart.js | 4.4.1 | 数据可视化 |
| CSS3 | - | 样式（暗色主题） |
| Nginx | 1.27-alpine | 静态托管 |

### 基础设施

| 组件 | 版本 | 用途 |
|-----|------|------|
| PostgreSQL | 16 | 主数据库 |
| Caddy | 2 | 反向代理 |
| Docker | - | 容器化 |
| Docker Compose | - | 编排 |
| AdGuard Home | 最新 | DNS服务 |
| Firefly III | 最新 | 记账系统 |

---

## 🚀 部署流程

### 1. 环境准备

```bash
# 1. 克隆项目
git clone <repo> pams
cd pams

# 2. 配置环境变量
cp config/.env.example .env
vim .env  # 填写数据库密码、API密钥等

# 3. 配置DNS
# 在AdGuard Home中添加:
# pams.local → 192.168.0.143
```

### 2. 启动服务

```bash
# 构建镜像
docker compose -f config/docker/compose.yaml build

# 启动服务
docker compose -f config/docker/compose.yaml up -d

# 查看日志
docker compose -f config/docker/compose.yaml logs -f
```

### 3. 数据初始化

```bash
# 初始化收支表
docker exec -i pams-postgres psql -U pams -d pams < database/postgres/schema/005_transactions.sql

# 导入资产数据（可选）
node backend/services/data-import/import_balance_sheet.js
```

### 4. 访问系统

- 总览: http://pams.local/
- 收支: http://pams.local/cashflow.html
- 持仓: http://pams.local/holdings.html
- 策略: http://pams.local/strategy.html
- 观察池: http://pams.local/watchlist.html

---

## 📈 数据流转

### 资产数据导入

```
1. 手动录入 → Excel → balance_sheet_export.csv
2. CSV → import_balance_sheet.js → PostgreSQL
3. PostgreSQL → /summary2/latest → Dashboard
```

### 收支数据流

```
1. 用户添加交易 → POST /api/transactions
2. 自动分类引擎 → pams_transactions表
3. 月度聚合查询 → GET /api/transactions/monthly/{month}
4. 前端渲染 → Chart.js饼图
```

### 实时更新

```
1. 用户刷新页面
2. fetch() API调用 (cache: 'no-store')
3. FastAPI查询最新数据
4. 返回JSON
5. JavaScript动态渲染
```

---

## 🔮 未来规划

### 短期 (1-3个月)
- [ ] Telegram Bot完整测试
- [ ] Binance/IBKR API自动同步
- [ ] Firefly III交易同步
- [ ] 收支预算管理功能

### 中期 (3-6个月)
- [ ] Vue3前端迁移 (frontend-vue/)
- [ ] WebSocket实时推送
- [ ] 移动端原生应用
- [ ] 收益归因分析

### 长期 (6-12个月)
- [ ] AI驱动选股引擎
- [ ] 自动化交易执行
- [ ] 多用户支持
- [ ] 社区开源版本

---

## 📝 维护指南

### 日常维护

```bash
# 查看服务状态
docker compose -f config/docker/compose.yaml ps

# 查看日志
docker compose -f config/docker/compose.yaml logs -f pams-api

# 重启服务
docker compose -f config/docker/compose.yaml restart pams-web

# 备份数据库
docker exec pams-postgres pg_dump -U pams pams > backup_$(date +%Y%m%d).sql
```

### 更新部署

```bash
# 1. 停止服务
docker compose -f config/docker/compose.yaml down

# 2. 拉取代码
git pull

# 3. 重新构建
docker compose -f config/docker/compose.yaml build --no-cache

# 4. 启动服务
docker compose -f config/docker/compose.yaml up -d
```

### 数据库维护

```bash
# 连接数据库
docker exec -it pams-postgres psql -U pams -d pams

# 查看表大小
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables WHERE schemaname = 'public' ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

# 清理旧数据（谨慎操作）
DELETE FROM pams_transactions WHERE created_at < NOW() - INTERVAL '1 year';
```

---

## 🐛 故障排查

### 前端无法访问

```bash
# 1. 检查Caddy
docker logs caddy | tail -20

# 2. 检查Nginx
docker logs pams-web | tail -20

# 3. 检查DNS解析
nslookup pams.local 192.168.0.143
```

### API 404错误

```bash
# 1. 检查API服务
docker logs pams-api | tail -20

# 2. 检查路由配置
cat gateway/Caddyfile

# 3. 手动测试API
curl http://localhost:8090/health
curl http://localhost:8090/summary2/latest?baseCurrency=CNY
```

### 数据库连接失败

```bash
# 1. 检查PostgreSQL
docker logs pams-postgres | tail -20

# 2. 检查网络
docker network inspect pams_default

# 3. 测试连接
docker exec pams-api python -c "import asyncpg; print('asyncpg OK')"
```

---

## 📚 相关文档

- [README.md](README.md) - 项目概述
- [CLEANUP_PLAN.md](CLEANUP_PLAN.md) - 清理计划
- [DASHBOARD_SPEC.md](docs/DASHBOARD_SPEC.md) - 仪表板规范
- [ITERATION_LOG.md](strategy/ITERATION_LOG.md) - 策略迭代日志
- [VUE3_MIGRATION_GUIDE.md](docs/VUE3_MIGRATION_GUIDE.md) - Vue3迁移指南

---

**最后更新**: 2026-02-15
**维护者**: hzfzzzi
**版本**: v1.0 (收支模块已完成)
