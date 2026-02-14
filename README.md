# PAMS - Personal Asset Management System

<div align="center">

![Version](https://img.shields.io/badge/version-1.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-green.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

**🏠 LAN-only 个人资产管理系统 · 隐私优先 · 多平台集成**

[功能特性](#功能特性) • [快速开始](#快速开始) • [架构设计](#架构设计) • [文档](#文档)

</div>

---

## 📖 项目简介

PAMS是一个专为个人投资者设计的**局域网资产管理系统**，通过Docker容器化部署，提供：

- 📊 **统一资产视图** - 多平台账户汇总（A股、美股、基金、理财、加密货币）
- 💰 **收支分析** - 自动分类、月度统计、预算管理
- 📈 **投资策略** - IPS策略管理、风险监控、回撤分析
- 🔒 **隐私优先** - 完全本地部署，无云依赖，数据自主可控
- 📱 **iOS友好** - AdGuard Home提供局域网域名解析

---

## ✨ 功能特性

### 核心功能

#### 1. 资产总览 (Dashboard)
- 💎 **5大KPI指标**：总资产、总负债、净资产、最大回撤、持仓数
- 📊 **多维度可视化**：
  - 资产结构饼图（按资产类别）
  - 负债结构分析
  - 净资产曲线（90天历史）
  - 近期收支趋势（30天）
- 🎯 **资产分类体系**：
  - CN_EQUITY (A股权益)
  - US_EQUITY (美股权益)
  - FIXED_INCOME (固收理财)
  - CRYPTO (加密货币)
  - CASH (现金)
  - ESOP (员工持股)
  - GOLD_COMMODITIES (黄金商品)
  - LIABILITY (负债)

#### 2. 收支分析 (Cashflow) ⭐ 新功能
- 📅 **月度统计**：收入、支出、净额自动汇总
- 🤖 **智能分类**：基于关键词的自动分类引擎（置信度评分）
- 📊 **分类占比**：支出分类饼图可视化
- 📝 **交易记录**：最近30条交易明细表格
- 💳 **账户管理**：支持多账户来源（微信、支付宝、信用卡等）

#### 3. 持仓管理 (Holdings)
- 📋 **完整持仓表格**：平台、账户、类型、代码、名称、市值、盈亏
- 🔍 **多维度筛选**：按资金类型、平台、账户筛选
- 💹 **实时盈亏**：持仓盈亏、日涨跌实时显示

#### 4. 投资策略 (Strategy)
- 📐 **IPS策略管理**：投资政策声明参数化配置
- ⚠️ **风险监控**：
  - 集中度检查（单一标的、单一类别）
  - 数据完整性检查
  - 回撤分析
- 📈 **NAV/回撤双轴图**：净资产价值与回撤百分比对比
- 🚨 **风险事件列表**：按严重程度排序（info/warn/critical）

#### 5. 观察池 (Watchlist)
- 👀 **候选标的管理**：CN/US市场标的追踪
- 📊 **状态分类**：candidate（候选）/ active（活跃）/ archived（已归档）
- 💡 **投资论点**：记录每个标的的投资理由

### 支持平台

| 平台 | 类型 | 支持情况 |
|------|------|----------|
| 🏦 **国信证券** | A股 | ✅ 完整支持 |
| 💼 **盈透证券 (IBKR)** | 美股 | ✅ 完整支持 |
| 💰 **腾讯理财通** | 基金/理财 | ✅ 完整支持 |
| 🏛️ **建设银行** | 银行账户 | ✅ 完整支持 |
| 🪙 **币安 (Binance)** | 加密货币 | ⏳ API集成中 |
| 🏢 **华为ESOP** | 员工持股 | ✅ 完整支持 |

---

## 🖼️ 项目截图

### 总览页面
```
┌─────────────────────────────────────────────────────────┐
│  PAMS 总览                              快照：2026-02-13 │
├─────────────────────────────────────────────────────────┤
│  总览 | 策略 | 观察池 | 持仓 | 收支                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  [总资产 ¥645K] [总负债 ¥32K] [净资产 ¥613K]            │
│  [最大回撤 -15%] [持仓数 29]                            │
│                                                          │
│  ┌──────────────────┐  ┌──────────────────┐            │
│  │  资产结构饼图    │  │  资产明细列表    │            │
│  │  ┌────────┐      │  │  CN_PRIVATE 31.8w│            │
│  │  │        │      │  │  CN_EQUITY  5.1w │            │
│  │  │  各类  │      │  │  CN_FUND    5.4w │            │
│  │  │  资产  │      │  │  ...            │            │
│  │  └────────┘      │  └──────────────────┘            │
│  └──────────────────┘                                   │
│                                                          │
│  ┌──────────────────────────────────────────┐          │
│  │  净资产曲线 (90天)                       │          │
│  │     📈 ¥613K                              │          │
│  └──────────────────────────────────────────┘          │
│                                                          │
│  ┌──────────────────────────────────────────┐          │
│  │  近期收支 (30天)                         │          │
│  │     📊 收支柱状图                         │          │
│  └──────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────┘
```

### 收支分析页面 ⭐
```
┌─────────────────────────────────────────────────────────┐
│  收支分析                         ◀ 2026-02 ▶           │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  [总收入 ¥5,000] [总支出 ¥347.50] [净额 ¥4,652.50]     │
│                                                          │
│  ┌──────────────────┐  ┌──────────────────┐            │
│  │ 支出分类占比     │  │ 最近交易记录     │            │
│  │  ┌────────┐      │  │ 2026-02-14 工资  │            │
│  │  │ 餐饮   │      │  │   +¥5,000       │            │
│  │  │ 92.7%  │      │  │ 2026-02-14 晚餐  │            │
│  │  │        │      │  │   -¥222 (自动)  │            │
│  │  │ 其他   │      │  │ 2026-02-14 午餐  │            │
│  │  │  7.3%  │      │  │   -¥50 (自动)   │            │
│  │  └────────┘      │  └──────────────────┘            │
│  └──────────────────┘                                   │
└─────────────────────────────────────────────────────────┘
```

> 💡 **提示**：实际页面使用Chart.js绘制动态图表，支持交互式操作

---

## 🚀 快速开始

### 前置要求

- **硬件**：一台局域网内的电脑（作为服务器）
- **软件**：
  - Docker & Docker Compose
  - Git
  - （可选）AdGuard Home（用于DNS解析）

### 安装步骤

#### 1. 克隆项目

```bash
git clone https://github.com/hzfzzzi-ui/pams.git
cd pams
```

#### 2. 配置环境变量

```bash
# 复制环境变量模板
cp config/.env.example .env

# 编辑.env文件，填写以下关键配置
vim .env
```

必需配置：
```env
# PostgreSQL数据库
POSTGRES_PASSWORD=your_secure_password
PGHOST=postgres
PGPORT=5432
PGDATABASE=pams
PGUSER=pams

# Firefly III (可选)
FIREFLY_APP_KEY=base64:your_32_char_key
FIREFLY_DB_PASSWORD=your_firefly_password
FIREFLY_TOKEN=your_api_token
```

#### 3. 初始化数据库

```bash
# 启动PostgreSQL容器
docker compose -f config/docker/compose.yaml up -d postgres

# 等待数据库启动（约10秒）
sleep 10

# 初始化收支表
docker exec -i pams-postgres psql -U pams -d pams < database/postgres/schema/005_transactions.sql
```

#### 4. 启动所有服务

```bash
# 构建镜像
docker compose -f config/docker/compose.yaml build

# 启动服务
docker compose -f config/docker/compose.yaml up -d

# 查看服务状态
docker compose -f config/docker/compose.yaml ps
```

#### 5. 访问系统

```bash
# 方式1：通过IP访问（任意设备）
http://192.168.0.143:8080/

# 方式2：通过域名访问（需配置DNS）
http://pams.local/
```

---

## 🌐 局域网域名配置（iOS友好）

由于iOS无法轻松编辑hosts文件，我们使用AdGuard Home提供局域网DNS解析。

### 配置步骤

#### 1. 固定服务器IP

在路由器管理页面（通常是`192.168.0.1`或`192.168.1.1`）：
- 进入DHCP设置
- 为服务器MAC地址预留IP：`192.168.0.143`

#### 2. 启动AdGuard Home

AdGuard Home已包含在docker-compose中，启动后访问：
```
http://192.168.0.143:3000
```

首次访问：
- 设置管理员密码
- 监听接口选择：`All interfaces`

#### 3. 添加DNS重写规则

在AdGuard Home管理页面：
- 进入 **Filters** → **DNS rewrites**
- 添加以下规则：

| 域名 | 目标IP |
|------|--------|
| `pams.local` | `192.168.0.143` |
| `firefly.pams.local` | `192.168.0.143` |
| `adguard.pams.local` | `192.168.0.143` |

#### 4. 配置iOS设备

在iPhone/iPad上：
1. 打开 **设置** → **Wi-Fi**
2. 点击已连接的网络旁的 **ⓘ**
3. 点击 **配置DNS** → **手动**
4. 删除现有DNS服务器
5. 添加DNS服务器：`192.168.0.143`
6. 保存

现在可以在Safari中访问：
- http://pams.local ✅
- http://firefly.pams.local ✅

---

## 🏗️ 架构设计

### 系统架构

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
│  │ /api/*  → pams-api:8000              │ │
│  │ /*      → pams-web:80                │ │
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

### 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端** | HTML5 + ES6 Modules + Chart.js | 原生JS，无框架依赖 |
| **Web服务器** | Nginx 1.27-alpine | 静态文件托管 |
| **后端** | Python 3.11 + FastAPI | RESTful API |
| **数据库** | PostgreSQL 16 | 主数据库 |
| **反向代理** | Caddy 2 | 自动HTTPS（LAN模式） |
| **DNS服务** | AdGuard Home | 局域网域名解析 |
| **记账系统** | Firefly III | 交易记录管理 |
| **容器编排** | Docker Compose | 服务编排 |

### 目录结构

```
pams/
├── frontend/              # 前端应用
│   ├── public/
│   │   ├── assets/
│   │   │   ├── css/style.css
│   │   │   └── js/
│   │   │       ├── api.js
│   │   │       ├── dashboard.js
│   │   │       ├── cashflow.js
│   │   │       ├── holdings.js
│   │   │       └── ...
│   │   ├── index.html
│   │   ├── cashflow.html
│   │   └── ...
│   ├── Dockerfile
│   └── nginx.conf
│
├── backend/               # 后端API
│   ├── api/
│   │   ├── main.py
│   │   ├── endpoints/
│   │   │   └── transactions.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── services/
│
├── gateway/               # 网关配置
│   └── Caddyfile
│
├── database/              # 数据库Schema
│   ├── postgres/
│   │   └── schema/
│   │       └── 005_transactions.sql
│   └── sqlite/
│       └── schema.sql
│
├── config/                # 配置文件
│   ├── docker/
│   │   └── compose.yaml
│   └── .env.example
│
├── scripts/               # 辅助脚本
│   ├── ingest/            # 数据摄入
│   ├── import/            # 数据导入
│   └── maintenance/       # 运维工具
│
├── telegram-bot/          # Telegram Bot
├── docs/                  # 文档
└── archive/               # 归档代码
```

---

## 📚 文档

- [完整架构文档](CURRENT_ARCHITECTURE.md) - 系统架构详解
- [清理计划](CLEANUP_PLAN.md) - 项目重构说明
- [清理后架构](ARCHITECTURE_CLEAN.md) - 当前架构总览
- [仪表板规范](docs/DASHBOARD_SPEC.md) - UI/UX规范
- [Vue3迁移指南](docs/VUE3_MIGRATION_GUIDE.md) - 前端框架迁移

---

## 🔌 API端点

### 资产管理

```bash
# 最新持仓
GET /holdings/latest

# 资产汇总（按资产类别）
GET /summary2/latest?baseCurrency=CNY

# 净资产时间序列
GET /nav/series?limit=90&baseCurrency=CNY
```

### 收支分析

```bash
# 创建交易记录
POST /api/transactions
{
  "amount": -50.0,
  "category": "餐饮",
  "date": "2026-02-14",
  "description": "午餐",
  "account": "wechat"
}

# 月度统计
GET /api/transactions/monthly/2026-02

# 最近交易
GET /api/transactions/recent?limit=30
```

### 风险与策略

```bash
# 风险事件
GET /risk/latest?limit=10

# IPS策略参数
GET /policy/ips
```

完整API文档请查看：[API端点总览](backend/api/endpoints_overview.py)

---

## 📊 数据导入

### 方式1：手动导入（Excel）

```bash
# 1. 准备Excel资产负债表
资产负债表.xlsx

# 2. 导出为CSV
scripts/import/balance_sheet_export.csv

# 3. 运行导入脚本
node backend/services/data-import/import_balance_sheet.js
```

### 方式2：API导入（推荐）

```bash
# 使用FastAPI直接导入
curl -X POST http://localhost:8090/api/transactions \
  -H "Content-Type: application/json" \
  -d '{"amount": 5000, "category": "工资", "description": "月薪"}'
```

### 方式3：Telegram Bot（开发中）

通过Telegram发送账户截图：
- 发送到 `@Pingihzf_bot`
- 自动OCR识别
- 分类存储到inbox/
- 等待人工审核后导入

---

## 🛠️ 开发指南

### 本地开发

```bash
# 1. 安装Python依赖
cd backend/api
pip install -r requirements.txt

# 2. 启动后端（开发模式）
uvicorn main:app --reload --port 8090

# 3. 启动前端（开发模式）
cd frontend/public
python -m http.server 8080

# 4. 访问
# 前端: http://localhost:8080
# API文档: http://localhost:8090/docs
```

### 添加新功能

1. **后端**：在 `backend/api/endpoints/` 创建新端点
2. **前端**：在 `frontend/public/assets/js/` 添加新模块
3. **数据库**：在 `database/postgres/schema/` 添加新表

### 代码规范

- **Python**：遵循PEP 8
- **JavaScript**：ES6+ 标准
- **Git Commit**：使用Conventional Commits格式
  - `feat:` 新功能
  - `fix:` Bug修复
  - `docs:` 文档更新
  - `chore:` 工具/配置更新

---

## 🐛 故障排查

### 常见问题

#### 1. 前端无法访问

```bash
# 检查Caddy状态
docker logs caddy

# 检查Nginx状态
docker logs pams-web

# 重启服务
docker compose -f config/docker/compose.yaml restart pams-web
```

#### 2. API 404错误

```bash
# 检查API服务
docker logs pams-api

# 手动测试API
curl http://localhost:8090/health
curl http://localhost:8090/summary2/latest?baseCurrency=CNY
```

#### 3. 数据库连接失败

```bash
# 检查PostgreSQL
docker logs pams-postgres

# 测试连接
docker exec -it pams-postgres psql -U pams -d pams
```

#### 4. DNS解析失败

```bash
# 测试DNS解析
nslookup pams.local 192.168.0.143

# 检查AdGuard Home
docker logs adguard
```

---

## 🤝 贡献指南

欢迎贡献！请遵循以下步骤：

1. Fork本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'feat: Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启Pull Request

---

## 📝 更新日志

### [v1.0] - 2026-02-15

#### 新增功能
- ✨ 收支分析模块（Cashflow Module）
  - 月度统计和自动分类
  - 支出分类饼图
  - 交易记录表格
- 📊 资产总览Dashboard
  - 5大KPI指标
  - 多维度可视化图表
- 💼 持仓管理模块
- 📈 投资策略和风险监控
- 👀 观察池管理

#### 项目重构
- 🗂️ 清理旧版代码，归档1.4MB
- 📁 优化目录结构（精简36%）
- 📚 完善文档（4个架构文档）

#### Bug修复
- 🐛 修复总览页API路径404错误
- 🐛 修复前端资源引用路径问题

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/) - 现代化的Python Web框架
- [Chart.js](https://www.chartjs.org/) - 简洁的JavaScript图表库
- [Firefly III](https://www.firefly-iii.org/) - 优秀的个人财务管理器
- [AdGuard Home](https://adguard.com/en/adguard-home/overview.html) - 网络级广告拦截器
- [Caddy](https://caddyserver.com/) - 现代化的Web服务器

---

## 📧 联系方式

- **GitHub**: [@hzfzzzi-ui](https://github.com/hzfzzzi-ui)
- **项目主页**: https://github.com/hzfzzzi-ui/pams
- **问题反馈**: [Issues](https://github.com/hzfzzzi-ui/pams/issues)

---

<div align="center">

**⭐ 如果这个项目对您有帮助，请给它一个Star！**

Made with ❤️ by hzfzzzi-ui

</div>
