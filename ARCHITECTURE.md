# PAMS 系统架构文档

**版本**: V1.0.0
**最后更新**: 2026-02-14

---

## 目录

1. [系统概述](#系统概述)
2. [架构层次](#架构层次)
3. [技术栈](#技术栈)
4. [目录结构](#目录结构)
5. [数据流](#数据流)
6. [API设计](#api设计)
7. [前端架构](#前端架构)
8. [数据库设计](#数据库设计)
9. [部署架构](#部署架构)
10. [扩展性设计](#扩展性设计)

---

## 系统概述

PAMS (Personal Asset Management System) 是一个**隐私优先的局域网个人资产管理系统**,采用前后端分离架构,通过 Docker 容器化部署在本地网络。

### 核心特性

- ✅ **隐私优先**: 所有数据存储在本地,无云依赖
- ✅ **多数据源**: 支持券商、银行、加密交易所等10+数据源
- ✅ **多币种**: 支持 CNY/USD/MXN 等多币种自动汇率转换
- ✅ **实时监控**: 资产结构、风险告警、持仓分析
- ✅ **策略驱动**: IPS政策引擎,规则化投资决策

---

## 架构层次

```
┌─────────────────────────────────────────────────────────┐
│                    用户访问层                            │
│  http://pams.local (域名) / http://192.168.0.143 (IP)   │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                    网关层 (Caddy)                        │
│  - 反向代理 (唯一对外端口 :80)                          │
│  - 路由规则: /api/* → pams-api:8000                     │
│              /*     → pams-web:80                        │
└─────────────────────────────────────────────────────────┘
           ↓                              ↓
┌─────────────────────┐      ┌──────────────────────────┐
│   前端层 (Nginx)    │      │   后端层 (FastAPI)       │
│  - 静态文件托管     │      │  - RESTful API           │
│  - SPA路由          │      │  - 数据聚合              │
│  - 资源缓存         │      │  - 业务逻辑              │
└─────────────────────┘      └──────────────────────────┘
           ↓                              ↓
┌─────────────────────────────────────────────────────────┐
│                  数据层 (PostgreSQL)                     │
│  - pams_holdings_snapshot (持仓快照)                    │
│  - pams_nav_daily (净资产时间序列)                      │
│  - pams_risk_events (风险事件)                          │
│  - pams_policy (策略配置)                               │
└─────────────────────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────────────────────┐
│              外部集成层 (可选)                           │
│  - Firefly III (记账系统)                               │
│  - Binance API (加密货币行情)                           │
│  - IBKR API (美股数据)                                  │
│  - Frankfurter API (汇率服务)                           │
└─────────────────────────────────────────────────────────┘
```

---

## 技术栈

### 前端技术

| 技术 | 版本 | 用途 |
|------|------|------|
| **HTML5** | - | 页面结构 |
| **ES6 JavaScript** | - | 业务逻辑 (ES Module) |
| **CSS3** | - | 样式 (深色主题) |
| **Chart.js** | 4.4.1 | 数据可视化 |
| **Nginx** | 1.27-alpine | Web服务器 |

**设计原则**:
- ❌ 无框架依赖 (Vue/React)
- ✅ 原生ES6模块化
- ✅ 轻量级,快速加载

### 后端技术

| 技术 | 版本 | 用途 |
|------|------|------|
| **Python** | 3.11+ | 主语言 |
| **FastAPI** | 0.100+ | Web框架 |
| **Uvicorn** | - | ASGI服务器 |
| **psycopg2** | 2.9.9 | PostgreSQL驱动 |
| **httpx** | - | HTTP客户端 (汇率API) |

### 数据库

| 技术 | 版本 | 用途 |
|------|------|------|
| **PostgreSQL** | 16-alpine | 主数据库 |
| **SQLite** | 3 | 遗留数据/备份 |

### 基础设施

| 技术 | 版本 | 用途 |
|------|------|------|
| **Docker** | 24+ | 容器化 |
| **Docker Compose** | 2.x | 编排 |
| **Caddy** | 2 | 反向代理/网关 |
| **AdGuard Home** | latest | DNS服务 (可选) |

---

## 目录结构

```
pams/
├── frontend/                      # 前端应用
│   ├── public/                    # 静态资源根目录
│   │   ├── assets/
│   │   │   ├── css/
│   │   │   │   └── style.css      # 全局样式
│   │   │   └── js/
│   │   │       ├── api.js         # API调用库
│   │   │       ├── ui.js          # UI组件库
│   │   │       ├── dashboard.js   # Dashboard逻辑
│   │   │       ├── holdings.js    # 持仓页逻辑
│   │   │       ├── cashflow.js    # 收支页逻辑
│   │   │       ├── strategy.js    # 策略页逻辑
│   │   │       └── watchlist.js   # 观察池逻辑
│   │   ├── index.html             # Dashboard首页
│   │   ├── holdings.html          # 持仓明细页
│   │   ├── cashflow.html          # 收支分析页
│   │   ├── strategy.html          # 策略IPS页
│   │   └── watchlist.html         # 观察池页
│   ├── Dockerfile                 # 前端镜像构建
│   └── nginx.conf                 # Nginx配置
│
├── backend/                       # 后端应用
│   └── api/                       # FastAPI服务
│       ├── main.py                # 主入口 (897行)
│       ├── requirements.txt       # Python依赖
│       └── Dockerfile             # 后端镜像构建
│
├── gateway/                       # 网关配置
│   └── Caddyfile                  # Caddy路由规则
│
├── config/                        # 配置文件
│   ├── docker/
│   │   └── compose.yaml           # Docker编排配置
│   └── .env.example               # 环境变量模板
│
├── database/                      # 数据库资源
│   ├── postgres/
│   │   └── schema/                # SQL脚本
│   └── sqlite/                    # SQLite文件
│
├── scripts/                       # 辅助脚本
│   ├── import/                    # 数据导入
│   └── maintenance/               # 维护工具
│
├── docs/                          # 文档
│   ├── ARCHITECTURE.md            # 架构文档 (本文件)
│   ├── CHANGELOG.md               # 变更日志
│   ├── VERSION_GUIDE.md           # 版本管理指南
│   └── API.md                     # API文档 (待创建)
│
├── VERSION                        # 版本号
└── README.md                      # 项目说明
```

---

## 数据流

### 1. 用户访问流程

```
用户浏览器
    ↓ GET http://pams.local/
Caddy (:80)
    ↓ reverse_proxy
Nginx (pams-web:80)
    ↓ 返回 index.html + assets/js/dashboard.js
浏览器执行 JavaScript
    ↓ jget('/summary2/latest?baseCurrency=CNY')
Caddy (:80)
    ↓ handle_path /api/* → reverse_proxy pams-api:8000
FastAPI (pams-api:8000)
    ↓ 查询 PostgreSQL
    ↓ 聚合计算
    ↓ 返回 JSON
浏览器渲染图表
```

### 2. 数据导入流程

```
Excel资产负债表 (资产负债表.xlsx)
    ↓ 手动导出
balance_sheet_export.csv
    ↓ node import_balance_sheet.js
SQLite/PostgreSQL 插入
    ↓
FastAPI 读取
    ↓
前端展示
```

### 3. 汇率转换流程

```
FastAPI 收到请求 (baseCurrency=CNY)
    ↓ 检查汇率缓存
    ↓ 未命中
外部API请求 (Frankfurter)
    ↓ GET https://api.frankfurter.app/latest?from=USD&to=CNY
    ↓ 缓存到数据库 (pams_fx_rates_daily)
    ↓ 应用汇率转换
返回 CNY 口径数据
```

---

## API设计

### RESTful端点架构

**Base URL**: `http://pams.local/api`

#### 资产汇总

```
GET /summary2/latest?baseCurrency=CNY
```

**响应结构**:
```json
{
  "as_of": "2026-02-13",
  "base_currency": "CNY",
  "totals": {
    "market_value": "645127.91127984",
    "holding_pnl": "-617.20",
    "daily_pnl": "-700.08"
  },
  "by_asset_class": [
    {
      "asset_class": "CN_EQUITY",
      "market_value": "51451.00",
      "holding_pnl": "1208.53",
      "daily_pnl": "0.00"
    }
  ],
  "children": [...]
}
```

#### 持仓明细

```
GET /holdings/latest
GET /holdings/as-of?date=2026-02-13
```

**响应结构**:
```json
{
  "as_of": "2026-02-13",
  "rows": [
    {
      "venue": "guosen",
      "account": "cn_a_rmb",
      "type": "stock_cn",
      "symbol": "601899",
      "name": "紫金矿业",
      "currency": "CNY",
      "market_value": "32113.00",
      "holding_pnl": "4167.08",
      "daily_pnl": null
    }
  ]
}
```

#### NAV时间序列

```
GET /nav/series?limit=90&baseCurrency=CNY
GET /nav/recent?limit=30
```

#### 风险事件

```
GET /risk/latest?limit=10
```

**响应结构**:
```json
{
  "as_of": "2026-02-13",
  "events": [
    {
      "severity": "crit",
      "code": "CONCENTRATION_SINGLE",
      "title": "单一标的集中度过高",
      "details": {
        "name": "华为员工内部持股",
        "weight": 0.5294
      }
    }
  ]
}
```

#### 现金流

```
GET /cashflow/series?limit=30&currency=CNY
```

#### 观察池

```
GET /watchlist?market=CN&status=active
POST /watchlist
```

---

## 前端架构

### 模块化设计

#### api.js - API调用层
```javascript
// 统一API入口
export function apiBase() { return '/api'; }

// HTTP客户端
export async function jget(path) { ... }
export async function jpost(path, bodyObj) { ... }

// 工具函数
export function fmt2(x) { ... }  // 数字格式化
export function typeLabel(t) { ... }  // 类型标签
```

#### ui.js - UI组件层
```javascript
export function kpi(label, value, change, trend) { ... }
export function renderAlerts(container, alerts) { ... }
export function setActiveNav() { ... }
```

#### dashboard.js - 业务逻辑层
```javascript
async function main() {
  // 1. 数据获取
  const summary = await jget('/summary2/latest?baseCurrency=CNY');
  const holdings = await jget('/holdings/latest');

  // 2. 数据处理
  calculateTotals(summary);
  groupByAssetClass(summary.by_asset_class);

  // 3. 渲染UI
  renderKPIs();
  renderCharts();
  renderAlerts();
}
```

### 页面路由

| 路径 | 页面 | 主要功能 |
|------|------|---------|
| `/` | index.html | Dashboard总览 |
| `/holdings.html` | holdings.html | 持仓明细列表 |
| `/cashflow.html` | cashflow.html | 收支趋势分析 |
| `/strategy.html` | strategy.html | IPS策略配置 |
| `/watchlist.html` | watchlist.html | 投资观察池 |

---

## 数据库设计

### 核心表结构

#### pams_holdings_snapshot
```sql
CREATE TABLE pams_holdings_snapshot (
    venue VARCHAR(50),           -- 交易所/平台
    account VARCHAR(100),        -- 账户
    type VARCHAR(50),            -- 资产类型
    symbol VARCHAR(50),          -- 代码
    name VARCHAR(200),           -- 名称
    currency VARCHAR(3),         -- 币种
    market_value DECIMAL(20,4),  -- 市值
    holding_pnl DECIMAL(20,4),   -- 持仓盈亏
    daily_pnl DECIMAL(20,4),     -- 日盈亏
    as_of DATE,                  -- 快照日期
    created_at TIMESTAMP,
    PRIMARY KEY (venue, account, type, symbol, as_of)
);
```

#### pams_nav_daily
```sql
CREATE TABLE pams_nav_daily (
    as_of DATE PRIMARY KEY,
    total_market_value DECIMAL(20,4),
    holdings_count INTEGER,
    venues_count INTEGER,
    completeness VARCHAR(20),  -- 'ok' | 'incomplete'
    updated_at TIMESTAMP
);
```

#### pams_risk_events
```sql
CREATE TABLE pams_risk_events (
    as_of DATE,
    severity VARCHAR(10),       -- 'info' | 'warn' | 'crit'
    code VARCHAR(50),           -- 'CONCENTRATION_SINGLE'
    title TEXT,
    details JSONB,
    created_at TIMESTAMP,
    PRIMARY KEY (as_of, code)
);
```

### 数据关系

```
pams_holdings_snapshot
    ↓ (聚合)
pams_nav_daily
    ↓ (分析)
pams_risk_events
```

---

## 部署架构

### Docker容器组成

```yaml
services:
  caddy:           # 网关 (80:80)
  pams-web:        # 前端 (8080:80, 内部)
  pams-api:        # 后端 (8090:8000, 内部)
  postgres:        # 数据库 (5432, 内部)
  firefly:         # 记账系统 (8082:8080)
  firefly-cron:    # 定时任务
  adguard:         # DNS (53:53, 可选)
```

### 网络拓扑

```
192.168.0.143 (宿主机)
    ↓
┌─────────────────────────────────────┐
│  Docker Bridge Network              │
│  ┌─────────┐  ┌──────────┐         │
│  │ caddy   │→ │pams-web  │         │
│  │  :80    │  │  :80     │         │
│  └─────────┘  └──────────┘         │
│       ↓                             │
│  ┌──────────┐  ┌──────────┐        │
│  │pams-api  │→ │postgres  │        │
│  │ :8000    │  │ :5432    │        │
│  └──────────┘  └──────────┘        │
└─────────────────────────────────────┘
```

### 端口映射

| 容器 | 内部端口 | 外部端口 | 协议 | 用途 |
|------|---------|---------|------|------|
| caddy | 80 | 80 | HTTP | 统一入口 |
| pams-web | 80 | 8080 | HTTP | 直接访问(调试) |
| pams-api | 8000 | 8090 | HTTP | 直接访问(调试) |
| postgres | 5432 | - | TCP | 内部访问 |
| firefly | 8080 | 8082 | HTTP | 记账系统 |
| adguard | 53/80 | 53/3000 | UDP/HTTP | DNS服务 |

---

## 扩展性设计

### 水平扩展

#### 前端扩展
- ✅ 静态文件 CDN 缓存
- ✅ Nginx 负载均衡 (多实例)
- ⚠️ 无状态设计,支持容器扩展

#### 后端扩展
- ✅ FastAPI 多进程/多线程
- ✅ 无状态API,支持多实例
- ⚠️ 数据库连接池优化

#### 数据库扩展
- ✅ PostgreSQL 读写分离
- ✅ 时间序列数据分区
- ⚠️ 缓存层 (Redis)

### 垂直扩展

#### 性能优化
- [ ] API响应缓存 (Redis)
- [ ] 数据库索引优化
- [ ] 前端资源压缩 (gzip)
- [ ] Chart.js懒加载

#### 功能扩展
- [ ] WebSocket实时推送
- [ ] 移动端PWA支持
- [ ] 多用户/多账户支持
- [ ] AI驱动的选股引擎

---

## 安全设计

### 网络安全
- ✅ LAN-only部署,无公网暴露
- ✅ HTTP-only (内网环境)
- ⚠️ 可选: HTTPS证书 (自签名)

### 数据安全
- ✅ 本地存储,无云同步
- ✅ PostgreSQL访问控制
- ⚠️ 数据库加密 (可选)

### 代码安全
- ✅ 无SQL注入 (参数化查询)
- ✅ 输入验证
- ⚠️ XSS防护 (CSP策略)

---

## 技术债务与改进方向

### 已知限制

1. **前端无框架**: 随着功能增加,原生JS维护成本上升
   - **改进**: 考虑引入 Vue 3 或 Svelte

2. **单体后端**: 所有逻辑在 main.py (897行)
   - **改进**: 模块化拆分 (endpoints/, services/)

3. **双数据库**: SQLite + PostgreSQL 并存
   - **改进**: 统一迁移到 PostgreSQL

4. **手动数据导入**: 依赖CSV导入
   - **改进**: 自动化采集 (API集成)

### 优化计划

| 优先级 | 项目 | 预期收益 |
|--------|------|---------|
| P0 | FastAPI端点模块化 | 提升可维护性 |
| P0 | 自动化数据采集 | 减少手动操作 |
| P1 | 引入Vue框架 | 提升开发效率 |
| P1 | Redis缓存层 | 提升API性能 |
| P2 | WebSocket推送 | 实时数据更新 |
| P2 | 移动端优化 | 改善iOS体验 |

---

## 相关文档

- [CHANGELOG.md](CHANGELOG.md) - 版本变更记录
- [VERSION_GUIDE.md](VERSION_GUIDE.md) - 版本管理规范
- [README.md](README.md) - 项目概述
- [DASHBOARD_SPEC.md](DASHBOARD_SPEC.md) - Dashboard设计规范

---

**维护者**: Claude Sonnet 4.5
**联系方式**: 通过项目Issue反馈
**最后审核**: 2026-02-14
