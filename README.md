# PAMS - Personal Asset Management System

> **个人资产管理系统** - 多平台资产聚合、智能收支分析、风险监控一体化解决方案

[![Version](https://img.shields.io/badge/version-1.3-blue.svg)](./VERSION.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)

---

## 📋 目录

- [核心功能](#-核心功能)
- [技术栈](#-技术栈)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
- [API文档](#-api文档)
- [功能特性](#-功能特性)
- [下一步计划](#-下一步计划)
- [贡献指南](#-贡献指南)

---

## 🎯 核心功能

### 资产管理
- **多平台聚合**：支持A股、美股、加密货币、基金、现金、员工持股等6大类资产
- **实时汇率转换**：CNY/USD/MXN自动换算，统一币种视图
- **智能分类**：基于资产类型和账户自动分类，支持4个主要持仓视图
- **风险监控**：集中度检查、回撤分析、数据完整性检测

### 收支分析
- **智能分类引擎**：基于关键词规则自动分类交易（18条预定义规则）
- **多维度统计**：按日/按月统计，支持收入、支出、结余分析
- **预算管理**：分类预算设置、执行跟踪、预警机制（默认80%阈值）
- **交互式图表**：折线图/饼图联动，hover实时显示分类占比

### 投资决策
- **NAV追踪**：日度净资产值曲线，自动回撤计算
- **观察池**：CN/US市场标的追踪，状态分类管理
- **策略配置**：IPS策略可参数化存储
- **持仓详情**：完整持仓表格，多维度筛选（venue/account/type）

---

## 🛠 技术栈

### 后端
- **框架**: FastAPI 0.115.8 + Uvicorn 0.30.6
- **语言**: Python 3.11
- **数据库**: PostgreSQL 16（主） + SQLite（备）
- **数据库驱动**: asyncpg 0.29.0（异步） + psycopg2 2.9.9（同步）
- **特点**: 原生SQL查询，无ORM

### 前端
- **架构**: 多页面应用（MPA）
- **核心**: 原生JavaScript（ES6+）
- **图表**: Chart.js 4.4.1
- **样式**: 自定义CSS（grid2/grid5布局系统）

### 基础设施
- **容器化**: Docker & Docker Compose
- **反向代理**: Caddy 2
- **DNS**: AdGuard Home（局域网DNS）
- **集成**: Firefly III（开源记账系统）

---

## 📁 项目结构

```
pams/
├── backend/
│   └── api/
│       ├── main.py                 # 核心API端点（1071行）
│       ├── endpoints/
│       │   └── transactions.py     # 交易API（618行）
│       ├── account_mapper.py       # 账户标准化
│       ├── type_mapper.py          # 资产分类映射
│       └── requirements.txt
│
├── database/
│   └── postgres/schema/
│       ├── 005_transactions.sql    # 交易/预算/分类规则表
│       └── 006_update_accounts.sql # 账户约束和视图
│
├── frontend/public/
│   ├── index.html                  # 仪表板首页
│   ├── cashflow.html               # 收支分析页
│   ├── holdings.html               # 持仓汇总页
│   ├── strategy.html               # 投资策略页
│   ├── watchlist.html              # 观察池页
│   └── assets/
│       ├── css/style.css           # 全局样式
│       └── js/                     # 交互逻辑
│
├── gateway/
│   └── Caddyfile                   # 反向代理配置
│
├── telegram-bot/
│   ├── bot.py                      # Telegram机器人
│   └── requirements.txt
│
├── compose.yaml                    # Docker编排
├── VERSION.md                      # 版本历史
└── README.md
```

### 核心文件说明

| 文件 | 行数 | 功能 |
|------|------|------|
| `backend/api/main.py` | 1071 | 资产聚合、NAV计算、观察池等核心端点 |
| `backend/api/endpoints/transactions.py` | 618 | 交易记录、月度统计、预算管理 |
| `database/postgres/schema/005_transactions.sql` | 126 | 交易表、预算表、分类规则表定义 |
| `frontend/public/cashflow.html` | 9563 | 收支分析页（含Chart.js配置） |
| `frontend/public/holdings.html` | 10793 | 持仓管理页（完整表格） |

---

## 🚀 快速开始

### 前置要求
- Docker & Docker Compose
- Git

### 安装步骤

1. **克隆仓库**
   ```bash
   git clone https://github.com/hzfzzzi-ui/pams.git
   cd pams
   ```

2. **配置环境变量**
   ```bash
   cp config/.env.example .env
   # 编辑.env文件，设置PostgreSQL密码等
   ```

3. **启动服务**
   ```bash
   docker compose up -d
   ```

4. **访问应用**
   - 主应用：`http://pams.local` 或 `http://localhost:80`
   - API文档：`http://localhost:8090/docs`（FastAPI自动生成）
   - Firefly III：`http://localhost:8082`

5. **初始化数据库**
   ```bash
   docker exec -it pams-postgres psql -U pams -d pams -f /path/to/schema/005_transactions.sql
   ```

### 端口映射

| 服务 | 对外端口 | 内部端口 | 用途 |
|------|---------|---------|------|
| Caddy | 80 | 80 | 反向代理 |
| pams-web | 8080 | 80 | 前端Nginx |
| pams-api | 8090 | 8000 | FastAPI后端 |
| pams-firefly | 8082 | 8080 | Firefly III记账系统 |
| pams-postgres | 5432 | 5432 | PostgreSQL数据库 |
| pams-adguard | 53/8081/3000 | 53/80/3000 | DNS/广告屏蔽 |

---

## 📡 API文档

### 资产管理端点

| 端点 | 方法 | 功能 | 参数 |
|------|------|------|------|
| `/holdings/latest` | GET | 获取最新持仓快照 | `venue`, `account`, `limit=500` |
| `/holdings/as-of` | GET | 获取特定日期持仓 | `date=YYYY-MM-DD`, `venue`, `account` |
| `/holdings/by-category/{category}` | GET | 按分类获取持仓 | `limit=500` |
| `/summary/latest` | GET | 汇总统计（按type） | `venue`, `account` |
| `/summary2/latest` | GET | 新汇总统计（按asset_class） | `venue`, `account` |
| `/overview` | GET | 仪表板综合数据 | `baseCurrency=CNY` |
| `/nav/recent` | GET | 最近NAV数据 | `limit=60` |
| `/nav/drawdown` | GET | 回撤分析 | `limit=365` |

### 交易管理端点

| 端点 | 方法 | 功能 | 参数 |
|------|------|------|------|
| `/api/transactions` | POST | 创建交易记录 | JSON body |
| `/api/transactions/recent` | GET | 获取最近交易 | `limit=50` |
| `/api/transactions/monthly/{month}` | GET | 月度统计 | `month=YYYY-MM` |
| `/api/transactions/daily/{month}` | GET | 每日统计 | `month=YYYY-MM` |
| `/api/transactions/categories-by-range` | GET | 时间段分类占比 | `start=YYYY-MM-DD`, `end=YYYY-MM-DD` |
| `/api/transactions/budgets/{month}` | GET | 预算执行情况 | `month=YYYY-MM` |
| `/api/transactions/budgets` | POST | 创建预算 | `category`, `month`, `amount` |

**完整API文档**：访问 `http://localhost:8090/docs` 查看Swagger UI

---

## ✨ 功能特性

### 1. 多平台资产聚合

**支持的平台：**
- ✅ 国信证券/同花顺（A股）
- ✅ Interactive Brokers（美股）
- ✅ 腾讯理财通（基金/理财）
- ✅ 建设银行（银行账户）
- ⏳ 币安 Binance（加密货币）
- ✅ 华为ESOP（员工持股）
- ✅ 微信/财付通（现金）

### 2. 智能收支分析

**自动分类引擎**：
- 基于关键词匹配（支持优先级和置信度）
- 18条预定义规则（午餐/晚餐/打车/房租等）
- 自学习机制（match_count统计）

**可视化图表**：
- 📊 按日/按月收支折线图
- 🥧 支出分类饼图（hover联动）
- 📈 累计vs单日分类对比

### 3. 风险监控

**集中度检查**：
- 单一标的占比 >30% → warn，>50% → crit
- 单一类别占比 >40% → warn，>60% → crit

**回撤分析**：
- 基于NAV日度数据计算历史最大回撤
- 实时回撤曲线展示

**数据完整性**：
- 自动检测数据跨度异常（>30%判定为incomplete）

### 4. 数据库设计

**核心表结构**：
```sql
pams_holdings_snapshot      -- 持仓快照（支持as_of查询）
pams_transactions           -- 交易记录（多币种+自动分类）
pams_budgets                -- 预算管理
pams_category_rules         -- 自动分类规则
pams_nav_daily              -- 日度NAV
pams_fx_rates_daily         -- 汇率表
pams_watchlist              -- 观察池
pams_asset_class_map        -- 资产类别映射
```

**关键约束**：
- 账户白名单：11个标准账户（腾讯理财通/建行/币安/IBKR等）
- 币种支持：CNY/USD/MXN
- 视图：`v_transactions_cny`（自动汇率换算）

---

## 🔮 下一步计划

### V1.4 - 外部集成增强（2周）
- [ ] **Binance API集成** - 加密货币实时数据同步
- [ ] **Firefly III API对接** - cashflow端点完整集成
- [ ] **Telegram机器人完善** - 交易记录快捷输入

### V1.5 - 前端优化（3周）
- [ ] **Vue 3重构** - 替代原生JS，提升可维护性
- [ ] **移动端适配** - 响应式设计，支持iOS/Android
- [ ] **PWA支持** - 离线访问能力

### V1.6 - 智能分析（4周）
- [ ] **机器学习分类** - 改进auto_classify算法
- [ ] **趋势预测** - 基于历史数据的支出预测
- [ ] **异常检测** - 自动识别异常交易

### V2.0 - 企业级特性（长期）
- [ ] **多用户支持** - 认证/授权机制
- [ ] **API限流** - 防止滥用
- [ ] **数据导出** - PDF报表生成
- [ ] **单元测试** - 覆盖率>80%

---

## 🤝 贡献指南

### 开发环境设置

1. **安装依赖**
   ```bash
   cd backend/api
   pip install -r requirements.txt
   ```

2. **运行开发服务器**
   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

3. **数据库迁移**
   ```bash
   psql -U pams -d pams -f database/postgres/schema/005_transactions.sql
   ```

### 代码规范

- **Python**: PEP 8
- **JavaScript**: ES6+ 标准
- **SQL**: PostgreSQL标准语法
- **提交信息**: 使用 Conventional Commits 格式

### 提交流程

1. Fork本仓库
2. 创建特性分支（`git checkout -b feature/AmazingFeature`）
3. 提交更改（`git commit -m 'feat: Add AmazingFeature'`）
4. 推送到分支（`git push origin feature/AmazingFeature`）
5. 开启Pull Request

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/) - 现代化Python Web框架
- [Chart.js](https://www.chartjs.org/) - 简单灵活的JavaScript图表库
- [Firefly III](https://www.firefly-iii.org/) - 开源个人财务管理器
- [Caddy](https://caddyserver.com/) - 自动HTTPS的Web服务器
- [AdGuard Home](https://adguard.com/en/adguard-home/overview.html) - 网络广告和跟踪器拦截器

---

## 📞 联系方式

- 问题反馈：[GitHub Issues](https://github.com/hzfzzzi-ui/pams/issues)
- 版本历史：[VERSION.md](VERSION.md)

---

**Built with ❤️ by hzfzzzi**
