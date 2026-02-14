# PAMS 项目全面分析报告

**分析日期**: 2026-02-15
**当前版本**: v1.1
**分析者**: Claude Sonnet 4.5

---

## 📋 目录

1. [项目概况](#项目概况)
2. [技术架构](#技术架构)
3. [功能模块分析](#功能模块分析)
4. [数据模型](#数据模型)
5. [代码结构](#代码结构)
6. [部署状态](#部署状态)
7. [数据状态](#数据状态)
8. [已知问题](#已知问题)
9. [技术债务](#技术债务)
10. [优化建议](#优化建议)
11. [路线图](#路线图)

---

## 📊 项目概况

### 基本信息

| 项目属性 | 值 |
|---------|---|
| **项目名称** | PAMS (Personal Asset Management System) |
| **当前版本** | v1.1 |
| **发布日期** | 2026-02-15 |
| **许可证** | MIT |
| **代码行数** | ~18,000行 |
| **提交数** | 8次 |
| **GitHub** | https://github.com/hzfzzzi-ui/pams |

### 项目定位

- **目标用户**: 个人投资者
- **部署方式**: LAN-only局域网部署
- **核心价值**: 隐私优先、多平台集成、策略化投资
- **技术特点**: Docker容器化、前后端分离、微服务架构

### 开发历程

- **2026-02-13**: 项目初始化，基础架构搭建
- **2026-02-14**: v1.0发布，收支分析模块完成
- **2026-02-15**: v1.1发布，持仓分类系统完成

---

## 🏗️ 技术架构

### 架构图

```
┌─────────────────────────────────────────────────┐
│              用户访问层                          │
│  http://pams.local (iOS/PC浏览器)               │
└──────────────────┬──────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────┐
│         Caddy 网关 (端口80)                      │
│  ┌──────────────┬──────────────────┐            │
│  │  /api/*  →   │  /*     →        │            │
│  │  pams-api    │  pams-web        │            │
│  └──────┬───────┴──────────┬───────┘            │
└─────────┼──────────────────┼────────────────────┘
          ↓                  ↓
┌──────────────────┐  ┌──────────────────┐
│   FastAPI后端    │  │   Nginx前端      │
│   (Python 3.11)  │  │   (静态文件)     │
│   端口: 8000     │  │   端口: 80       │
└────────┬─────────┘  └──────────────────┘
         ↓
┌──────────────────┐
│  PostgreSQL 16   │
│   主数据库       │
│   端口: 5432     │
└──────────────────┘

辅助服务:
├─ Firefly III (记账系统, 端口8080)
├─ AdGuard Home (DNS, 端口53)
└─ Telegram Bot (数据摄入)
```

### 技术栈

#### 后端 (Backend)
- **语言**: Python 3.11
- **框架**: FastAPI
- **数据库**: PostgreSQL 16 (主) + SQLite (遗留)
- **ORM**: psycopg2-binary
- **Web服务器**: Uvicorn

#### 前端 (Frontend)
- **语言**: HTML5 + ES6 JavaScript
- **UI框架**: 原生CSS (无框架)
- **图表库**: Chart.js 4.4.1
- **Web服务器**: Nginx 1.27-alpine
- **模块化**: ES6 Modules

#### 基础设施
- **容器编排**: Docker Compose
- **反向代理**: Caddy 2
- **DNS**: AdGuard Home
- **记账系统**: Firefly III

#### 开发工具
- **版本控制**: Git
- **CI/CD**: (未配置)
- **测试**: (未配置)

### 网络架构

- **局域网IP**: 192.168.0.143
- **域名**: pams.local (AdGuard DNS解析)
- **对外端口**: 仅80端口 (Caddy网关)
- **内部通信**: Docker网络

---

## 🎯 功能模块分析

### 1. 资产总览 (Dashboard)

**文件**: `frontend/public/index.html`, `assets/js/dashboard.js`

**核心功能**:
- 5个KPI卡片：总资产、总负债、净资产、最大回撤、持仓数
- 资产结构饼图（按asset_class分类）
- 负债结构饼图（按账户分组）
- 净资产曲线图（90天历史）
- 收支迷你图（30天）
- 风险告警面板
- 系统状态面板

**API依赖**:
- `GET /api/summary2/latest` - 资产汇总
- `GET /api/holdings/latest` - 持仓列表
- `GET /api/nav/series` - NAV时间序列
- `GET /api/cashflow/series` - 收支数据
- `GET /api/risk/latest` - 风险事件

**状态**: ✅ 完成

### 2. 持仓管理 (Holdings)

**文件**: `frontend/public/holdings.html`, `assets/js/holdings.js`, `assets/js/holdings_categories.js`

**核心功能** (v1.1更新):
- **全部持仓总览**: 显示所有持仓的汇总表格
- **快速导航**: 跳转到各分类
- **四大分类详情** (统一页面顺序展示):
  1. **A股持仓**: 市值走势图 + 4个KPI + 明细表
  2. **美股持仓**: 市值走势图 + 4个KPI + 明细表
  3. **加密持仓**: 市值走势图 + 4个KPI + 明细表
  4. **其他持仓**: 市值走势图 + 4个KPI + 明细表

**数据分类规则**:
| 大类 | 小类 | type字段 |
|------|------|---------|
| A股 | A股、A股基金 | stock, fund, stock_cn, equity_cn |
| 美股 | 美股 | stock_us, equity_us |
| 加密 | BTC, ETH, USDT | crypto |
| 其他 | 员工持股、贵金属、理财、现金等 | esop, gold, wealth, cash, cpf, loan |

**API依赖**:
- `GET /api/holdings/latest` - 全部持仓
- `GET /api/holdings/by-category/{category}` - 分类持仓
- `GET /api/holdings/history/{category}` - 分类历史数据

**状态**: ✅ v1.1完成（统一页面展示）

### 3. 收支分析 (Cashflow)

**文件**: `frontend/public/cashflow.html`, `assets/cashflow.js`

**核心功能**:
- 近90天收支曲线图（收入/支出/净额）
- 月度统计卡片
- 交易记录列表（预留）

**数据库表**:
- `pams_transactions` - 交易记录
- `pams_budgets` - 预算管理
- `pams_category_rules` - 分类规则

**API依赖**:
- `GET /api/cashflow/series` - 收支时间序列
- `GET /api/transactions/monthly/{month}` - 月度统计
- `GET /api/transactions/recent` - 最近交易

**状态**: 🟡 基础完成（缺少交易记录数据）

### 4. 投资策略 (Strategy)

**文件**: `frontend/public/strategy.html`, `assets/js/strategy.js`

**核心功能**:
- IPS v1策略参数展示
- NAV/回撤双轴图表
- 风险事件列表
- 调试面板（JSON原始数据）

**策略参数**:
```json
{
  "maxPositions": {"core": 10, "satellite": 5},
  "maxDrawdown": -0.25,
  "sectorLimit": 0.40,
  "rebalanceBand": 0.10,
  "alertMode": "A2"
}
```

**API依赖**:
- `GET /api/policy/ips` - IPS策略
- `GET /api/risk/latest` - 风险事件
- `GET /api/nav/drawdown` - 回撤曲线

**状态**: ✅ 完成

### 5. 观察池 (Watchlist)

**文件**: `frontend/public/watchlist.html`, `assets/js/watchlist.js`

**核心功能**:
- 观察标的列表（CN/US市场）
- 状态分类（candidate/active/archived）
- 投资论点记录

**API依赖**:
- `GET /api/watchlist` - 观察池列表
- `POST /api/watchlist` - 添加标的

**状态**: ✅ 完成

---

## 🗄️ 数据模型

### PostgreSQL主数据库 (pams)

#### 核心表

**1. pams_holdings_snapshot** (持仓快照)
```sql
CREATE TABLE pams_holdings_snapshot (
    venue VARCHAR(50),           -- 平台 (cn, binance, ibkr)
    account VARCHAR(100),         -- 账户名称
    type VARCHAR(50),             -- 类型 (stock, crypto, fund等)
    symbol VARCHAR(50),           -- 代码
    name VARCHAR(200),            -- 名称
    currency VARCHAR(10),         -- 币种
    market_value DECIMAL(15,2),   -- 市值
    holding_pnl DECIMAL(15,2),    -- 持仓盈亏
    daily_pnl DECIMAL(15,2),      -- 日盈亏
    as_of DATE,                   -- 快照日期
    created_at TIMESTAMP          -- 创建时间
);
```

**当前数据状态** (2026-02-15):
- 总记录数: 31条
- 快照数: 1个
- 最新日期: 2026-02-15

**分类分布**:
- A股: 14条 (45%)
- 美股: 0条 (0%)
- 加密: 1条 (3%)
- 其他: 16条 (52%)

**2. pams_nav_daily** (日净资产)
```sql
CREATE TABLE pams_nav_daily (
    as_of DATE PRIMARY KEY,
    total_market_value DECIMAL(15,2),
    holdings_count INTEGER,
    venues_count INTEGER,
    completeness VARCHAR(20),
    updated_at TIMESTAMP
);
```

**3. pams_transactions** (收支交易)
```sql
CREATE TABLE pams_transactions (
    id SERIAL PRIMARY KEY,
    date DATE,
    amount DECIMAL(15,2),
    category VARCHAR(50),
    description TEXT,
    account VARCHAR(50),
    currency VARCHAR(3),
    auto_classified BOOLEAN,
    created_at TIMESTAMP
);
```

**4. pams_risk_events** (风险事件)
```sql
CREATE TABLE pams_risk_events (
    as_of DATE,
    severity VARCHAR(20),        -- info/warn/critical
    code VARCHAR(50),             -- 事件代码
    title VARCHAR(200),           -- 标题
    details JSONB,                -- 详情
    created_at TIMESTAMP
);
```

**5. pams_watchlist** (观察池)
```sql
CREATE TABLE pams_watchlist (
    market VARCHAR(10),           -- CN/US
    symbol VARCHAR(50),
    name VARCHAR(200),
    thesis TEXT,
    status VARCHAR(20),           -- candidate/active/archived
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

### 映射规则表

**pams_asset_class_map** (资产分类映射)
- 用于将venue + type → asset_class的规则表

**pams_fx_rates_daily** (汇率)
- 存储历史汇率数据

---

## 📁 代码结构

### 目录树

```
pams/
├── backend/                    # 后端服务
│   ├── api/                   # FastAPI主服务
│   │   ├── main.py           # 主入口 (API端点定义)
│   │   ├── type_mapper.py    # 类型映射系统 ⭐ v1.1新增
│   │   ├── account_mapper.py # 账户标准化 ⭐ v1.1新增
│   │   ├── endpoints/        # 端点模块
│   │   │   └── transactions.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── services/             # 业务服务（预留）
│   └── shared/               # 共享代码（预留）
│
├── frontend/                  # 前端静态文件
│   └── public/
│       ├── index.html        # Dashboard总览
│       ├── holdings.html     # 持仓页面 ⭐ v1.1重大更新
│       ├── holdings_categories.html  # 独立分类页（备用）
│       ├── cashflow.html     # 收支分析
│       ├── strategy.html     # 投资策略
│       ├── watchlist.html    # 观察池
│       ├── test_api.html     # API调试工具 ⭐ v1.1新增
│       └── assets/
│           ├── css/
│           │   └── style.css # 全局样式
│           └── js/
│               ├── api.js            # API封装
│               ├── ui.js             # UI组件
│               ├── dashboard.js      # Dashboard逻辑
│               ├── holdings.js       # 全部持仓逻辑
│               ├── holdings_categories.js  # 分类逻辑 ⭐ v1.1
│               ├── cashflow.js       # 收支逻辑
│               ├── strategy.js       # 策略逻辑
│               └── watchlist.js      # 观察池逻辑
│
├── frontend-vue/              # Vue 3前端（开发中）
│   └── src/                  # 未来版本
│
├── gateway/                   # 网关配置
│   └── Caddyfile             # Caddy反向代理
│
├── database/                  # 数据库
│   ├── postgres/
│   │   └── schema/           # PostgreSQL Schema
│   │       ├── 001_*.sql
│   │       └── 006_update_accounts.sql ⭐ v1.1
│   └── sqlite/               # SQLite（遗留）
│       └── schema.sql
│
├── scripts/                   # 辅助脚本
│   ├── ingest/               # 数据摄入
│   ├── import/               # 数据导入
│   └── maintenance/          # 维护工具
│
├── telegram-bot/              # Telegram机器人
│   └── bot.py                # 截图摄入
│
├── config/                    # 配置文件
│   └── docker/
│       └── compose.yaml      # Docker编排
│
├── docs/                      # 文档
│   ├── PROJECT_ANALYSIS_2026-02-15.md  # 本文档
│   ├── VERSION_v1.1_SUMMARY.md
│   ├── HOLDINGS_CATEGORIES_UPDATE.md
│   ├── ACCOUNT_MAPPING.md
│   └── ...
│
├── CHANGELOG.md               # 变更日志
├── README.md                  # 项目说明
├── LICENSE                    # MIT许可证
├── .gitignore
│
├── quick-push.bat             # 快速提交脚本 ⭐ v1.1
├── quick-push.ps1
└── push-now.sh
```

### 文件统计

| 类别 | 数量 |
|------|------|
| HTML页面 | 11个 |
| JavaScript模块 | 9个 |
| Python模块 | 6个 |
| SQL脚本 | 2个 |
| Markdown文档 | 7个 |
| 配置文件 | 5个 |

---

## 🚀 部署状态

### 运行容器

| 容器名 | 状态 | 端口映射 | 作用 |
|--------|------|---------|------|
| **pams-web** | Up 3分钟 | 8080→80 | Nginx前端 |
| **pams-api** | Up 21分钟 | 8090→8000 | FastAPI后端 |
| **pams-postgres** | Up 1小时 | 5432 | PostgreSQL数据库 |
| **pams-caddy** | Up 6小时 | 80→80 | Caddy网关 |
| **pams-firefly** | Up 7小时 (healthy) | 8082→8080 | Firefly III记账 |
| **pams-firefly-cron** | Up 7小时 (unhealthy) | - | Firefly定时任务 |
| **pams-adguard** | Up 7小时 | 53, 3000, 8081 | AdGuard DNS |
| **pams-bot** | Up 3小时 | - | Telegram Bot |

### 访问地址

- **主入口**: http://pams.local 或 http://192.168.0.143
- **API文档**: http://pams.local/docs (FastAPI自动生成)
- **Firefly**: http://firefly.pams.local
- **AdGuard**: http://adguard.pams.local

### 健康状态

- ✅ pams-web: 正常
- ✅ pams-api: 正常
- ✅ pams-postgres: 正常
- ✅ pams-caddy: 正常
- ✅ pams-firefly: 健康
- ⚠️ pams-firefly-cron: 不健康（需检查）
- ✅ pams-adguard: 正常
- ✅ pams-bot: 正常

---

## 📊 数据状态

### 持仓数据

**总览**:
- 总记录: 31条
- 快照日期: 2026-02-15 (单日)
- 覆盖平台: 3个 (cn, binance, manual)

**分类统计**:
```
A股:    14条 (45%)  - 市值最高: 紫金矿业 ¥32,113
美股:    0条 (0%)   - 无数据
加密:    1条 (3%)   - ETH现金 $679.26
其他:   16条 (52%)  - 市值最高: 华为员工持股 ¥318,000
```

**数据质量**:
- ✅ 所有记录有市值数据
- ⚠️ 仅单日快照，无历史趋势
- ⚠️ 部分记录缺少holding_pnl和daily_pnl
- ⚠️ 美股类别完全为空

### NAV数据

- 记录数: 2条
- 日期范围: 2026-02-11 ~ 2026-02-13
- ⚠️ 数据点不足，无法计算准确的最大回撤

### 收支数据

- ⚠️ 交易记录为空
- 预算管理未配置

### 观察池数据

- 标的数量: (未统计)
- 市场分布: CN/US

---

## 🐛 已知问题

### 严重 (Critical)

1. **历史数据缺失**
   - 持仓快照仅1天数据
   - NAV时间序列仅2个点
   - 影响: 图表无法显示趋势，回撤计算不准确

2. **美股数据为空**
   - 美股分类完全没有持仓记录
   - 影响: 美股页面空白

### 中等 (Medium)

3. **收支模块数据缺失**
   - pams_transactions表为空
   - 影响: 收支图表无数据

4. **Firefly Cron不健康**
   - 定时任务容器状态unhealthy
   - 影响: 可能影响Firefly数据同步

5. **盈亏数据不完整**
   - 部分持仓缺少holding_pnl和daily_pnl
   - 影响: KPI卡片数据不完整

### 轻微 (Low)

6. **浏览器缓存问题**
   - 容器重建后需要强制刷新
   - 解决方案: Ctrl+Shift+R

7. **前端无框架依赖**
   - 原生HTML/JS，代码复用性低
   - 计划: Vue 3迁移中

---

## 💳 技术债务

### 架构层面

1. **双数据库系统**
   - PostgreSQL + SQLite并存
   - 建议: 统一到PostgreSQL，移除SQLite

2. **遗留代码**
   - archive/目录1.4MB旧代码
   - 建议: 删除或Git历史管理

3. **API端点混乱**
   - Express (18888端口) + FastAPI (8000端口)
   - 建议: 逐步迁移Express端点到FastAPI

### 代码层面

4. **main.py过大**
   - 897行代码在单文件
   - 建议: 拆分为模块化结构 (endpoints/, services/, models/)

5. **前端模块化不足**
   - 代码复用性低
   - 建议: 引入Vue 3组件化

6. **缺少测试**
   - 无单元测试、集成测试
   - 建议: 添加pytest测试框架

### 数据层面

7. **数据导入手动化**
   - 依赖Excel手动导入
   - 建议: 自动化数据采集 (Binance API, IBKR API)

8. **缺少数据校验**
   - 导入数据无验证机制
   - 建议: 添加Pydantic数据模型验证

---

## 💡 优化建议

### 短期 (1-2周)

#### 1. 补充历史数据 (优先级: ⭐⭐⭐⭐⭐)
```sql
-- 建议：每日生成持仓快照
-- 目标：至少30天历史数据
-- 方案：
1. 创建定时任务 (cron job)
2. 每日凌晨1点执行 INSERT INTO pams_holdings_snapshot
3. 或手动填充历史数据（如有Excel记录）
```

#### 2. 修复Firefly Cron (优先级: ⭐⭐⭐⭐)
```bash
docker logs pams-firefly-cron --tail 50
# 检查错误日志，修复配置
```

#### 3. 添加盈亏数据 (优先级: ⭐⭐⭐)
```python
# 计算公式
holding_pnl = market_value - cost_basis
daily_pnl = today_market_value - yesterday_market_value
```

### 中期 (1-2个月)

#### 4. API模块化重构 (优先级: ⭐⭐⭐⭐)
```
backend/api/
├── main.py                # 仅路由注册
├── endpoints/             # 端点层
│   ├── holdings.py
│   ├── summary.py
│   ├── nav.py
│   └── risk.py
├── services/              # 业务层
│   ├── asset_service.py
│   └── risk_service.py
└── models/                # 数据模型
    ├── holding.py
    └── nav.py
```

#### 5. 自动化数据采集 (优先级: ⭐⭐⭐⭐)
- Binance API集成
- IBKR Flex Query集成
- 定时任务调度 (APScheduler)

#### 6. Vue 3前端迁移 (优先级: ⭐⭐⭐)
- 完成frontend-vue开发
- 组件化、TypeScript、状态管理
- 替换原生HTML/JS

### 长期 (3-6个月)

#### 7. 实时数据推送 (优先级: ⭐⭐)
- WebSocket集成
- 实时持仓更新
- 风险告警推送

#### 8. 移动原生应用 (优先级: ⭐⭐)
- iOS原生应用
- 推送通知
- 离线支持

#### 9. 多用户支持 (优先级: ⭐)
- 用户认证
- 权限管理
- 多账户隔离

---

## 🗺️ 路线图

### V1.2.0 (2周内)

**主题**: 数据完整性

- [ ] 补充30天历史持仓快照
- [ ] 添加美股测试数据
- [ ] 修复Firefly Cron健康问题
- [ ] 完善盈亏数据计算
- [ ] 添加数据校验

### V1.3.0 (1个月内)

**主题**: API重构与自动化

- [ ] main.py模块化拆分
- [ ] Binance API集成
- [ ] IBKR数据采集
- [ ] 定时任务调度
- [ ] 单元测试覆盖率 >50%

### V1.5.0 (2个月内)

**主题**: Vue 3前端现代化

- [ ] Vue 3框架迁移完成
- [ ] Element Plus UI组件集成
- [ ] TypeScript类型安全
- [ ] Pinia状态管理
- [ ] 组件懒加载

### V2.0.0 (3-6个月)

**主题**: 实时化与移动化

- [ ] WebSocket实时推送
- [ ] PWA支持
- [ ] iOS原生应用
- [ ] Redis缓存层
- [ ] 多用户支持

---

## 📈 代码质量指标

### 当前状态

| 指标 | 值 | 目标 |
|-----|----|----|
| **代码行数** | ~18,000 | - |
| **测试覆盖率** | 0% | >80% |
| **API响应时间** | <100ms | <50ms |
| **前端加载时间** | ~2s | <1s |
| **Docker镜像大小** | ~500MB | <200MB |
| **数据库查询时间** | <50ms | <20ms |

### 代码复杂度

- **main.py**: 897行（需拆分）
- **dashboard.js**: ~300行（可接受）
- **holdings_categories.js**: ~130行（良好）

---

## 🔐 安全性分析

### 当前安全措施

✅ **已实施**:
- LAN-only部署，无公网暴露
- Docker容器隔离
- PostgreSQL密码保护
- AdGuard DNS过滤

⚠️ **缺失**:
- 无用户认证
- 无API密钥验证
- 无数据加密（传输/存储）
- 无审计日志

### 建议改进

1. 添加基础认证 (HTTP Basic Auth)
2. API密钥验证
3. HTTPS支持（自签名证书）
4. 敏感数据加密存储

---

## 📝 总结

### 优势

1. ✅ **架构清晰**: Docker容器化，前后端分离
2. ✅ **功能完整**: Dashboard、持仓、收支、策略、观察池
3. ✅ **隐私优先**: LAN-only部署，无云依赖
4. ✅ **持仓分类系统完善**: v1.1已实现A股/美股/加密/其他分类
5. ✅ **文档齐全**: README、CHANGELOG、各种技术文档

### 劣势

1. ⚠️ **历史数据不足**: 仅1天快照，无法展示趋势
2. ⚠️ **数据采集手动化**: 依赖Excel导入
3. ⚠️ **缺少测试**: 无单元测试、集成测试
4. ⚠️ **前端原生实现**: 代码复用性低
5. ⚠️ **技术债务**: main.py过大，双数据库系统

### 下一步行动

**立即执行** (本周):
1. 补充历史持仓快照数据（至少7天）
2. 修复Firefly Cron健康问题
3. 添加美股测试数据

**近期计划** (本月):
1. 完成API模块化重构
2. 集成Binance API自动采集
3. 添加基础测试覆盖

**中期目标** (3个月):
1. Vue 3前端迁移完成
2. 实现自动化数据采集
3. 添加实时数据推送

---

## 📊 附录：关键指标

### 性能指标

| API端点 | 平均响应时间 | QPS |
|---------|-------------|-----|
| /api/summary2/latest | ~80ms | - |
| /api/holdings/latest | ~50ms | - |
| /api/holdings/by-category/A股 | ~40ms | - |

### 数据规模

| 数据表 | 记录数 | 增长率 |
|--------|--------|--------|
| pams_holdings_snapshot | 31 | +31/天 (预计) |
| pams_nav_daily | 2 | +1/天 |
| pams_transactions | 0 | 未启用 |

---

**分析完成日期**: 2026-02-15
**下次复审**: 2026-03-01
**分析版本**: v1.0

