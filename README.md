# PAMS - Personal Asset Management System

> **个人资产管理系统** - 多平台资产聚合、智能收支分析、A股量化策略、回测引擎、风险监控一体化解决方案

[![Version](https://img.shields.io/badge/version-1.7-blue.svg)](./VERSION.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)

---

## 核心功能

### 资产管理
- **多平台聚合**：支持A股、美股、加密货币、基金、贵金属、现金等6大类资产
- **实时汇率转换**：CNY/USD/MXN双向独立获取
- **智能分类**：基于资产类型和账户自动分类，配置驱动的持仓详情视图
- **风险监控**：集中度检查、回撤分析、数据完整性检测

### A股量化策略引擎（V1.6 新增）
- **多阶段选股流水线**：Universe 筛选 → 趋势判定 → 回踩区间 → 突破触发 → 量价确认
- **技术指标体系**：MA10/MA20/MA50 趋势门、ATR 止损/加仓、相对强度 RS、放量突破
- **量价确认 (Flow Confirm)**：成交量放大比、主力资金流向、大单净买入综合评分
- **风险仓位管理**：基于 ATR 的头寸计算、并发风险控制、板块集中度检查
- **订单生命周期**：候选 → 订单草稿 → 确认 → 执行 → 过期，全流程管理
- **盘中刷新**：15分钟级别实时扫描，Telegram 推送触发信号
- **黑名单 & 冷却期**：止损后 30 天冷却，ST/退市/停牌自动过滤
- **事件监控**：财报季、解禁日、重大事件自动标记
- **市场情绪**：涨停/跌停数、主力资金流、社交热度综合评分
- **观察池管理**：板块扫描入池、退出条件检查、入池/退出历史追踪

### HV Alpha 回测引擎（V1.7 新增）
- **V1 HV Alpha Model**：20日高点突破 + 量价确认 + ATR 止损 + 金字塔加仓
  - 动态股票池：定期筛选流动性充足的活跃标的
  - 宏观过滤：沪深300指数 MA200 趋势判定（RISK_ON / NEUTRAL）
  - 金字塔加仓：盈利 1R 后追加仓位，最多加仓 2 次
  - 跟踪止损：3x ATR 动态止损，锁定利润
  - 分析报告：7 大板块（概述/资金曲线/交易统计/每周回顾/月度归因/改进建议）
- **V2 Risk Protocol**：在 V1 基础上叠加三级风险状态机
  - **NORMAL** (R=2.5%) → **DEGRADED** (R=1.5%) → **CONSERVATIVE** (R=1.0%)
  - Trigger A：连续 12 笔亏损 → DEGRADED（持续 20 笔交易）
  - Trigger B：回撤 >= 25% → DEGRADED（至回撤恢复 < 15%）
  - Trigger C：连续 9 月负收益 + 指数 < MA200 → CONSERVATIVE
  - 资金锁：权益翻倍 → R 永久降 0.3%
  - CONSERVATIVE 信号过滤：综合评分（RS 40% + 放量比 30% + 突破幅度 30%），仅放行 top 20%
  - V1 引擎零修改，V2 通过组合/导入完全复用 V1 函数
- **交互式前端**：V1/V2 策略切换、参数面板、资金曲线图表、交易明细表、RP 事件卡片

### 交易识别（V1.5）
- **OCR 截图识别**：发送券商持仓截图，Tesseract OCR 自动识别交易数据
- **文字指令**：`买入 贵州茅台 100股 @1800` / `sell AAPL 50 @195.5`
- **确认入账**：识别结果展示 → 用户确认 → UPSERT 持仓快照
- **支持券商**：同花顺、广发证券、IBKR盈透、币安

### 收支分析
- **智能分类引擎**：基于关键词规则自动分类交易（18条预定义规则）
- **多维度统计**：按日/按月统计，支持收入、支出、结余分析
- **预算管理**：分类预算设置、执行跟踪、预警机制
- **交互式图表**：折线图/饼图联动，hover实时显示分类占比

### 每日推送
- **持仓日报**：每日 9:00 CST 自动推送资产总值、分类占比、盈亏变动
- **通知框架**：预留 Agent 消息推送接口，支持外部服务投递通知

---

## 技术栈

### 后端
- **框架**: FastAPI 0.115 + Uvicorn
- **语言**: Python 3.11
- **数据库**: PostgreSQL 16（原生SQL，无ORM）
- **行情数据**: AkShare（A股/指数 OHLCV）
- **OCR**: Tesseract 5.5 + pytesseract（中英文语言包）

### 前端
- **架构**: 多页面应用（MPA），ES Module
- **核心**: 原生JavaScript（ES6+）
- **图表**: Chart.js 4.4
- **样式**: 自定义CSS（Samurai Black 主题）

### 基础设施
- **容器化**: Docker & Docker Compose（9个服务）
- **反向代理**: Caddy 2
- **DNS**: AdGuard Home（局域网DNS）
- **定时调度**: APScheduler（Bot内）+ 独立Scheduler服务
- **Bot**: Telegram Bot（记账 + 交易识别 + 助手指令桥）

---

## 项目结构

```
pams/
├── backend/
│   ├── api/
│   │   ├── main.py                 # 核心API（汇率、NAV、持仓汇总等）
│   │   ├── endpoints/
│   │   │   ├── transactions.py     # 交易/预算API
│   │   │   ├── holdings_trade.py   # 持仓交易UPSERT API
│   │   │   ├── notifications.py    # 通知推送API
│   │   │   ├── prices.py           # 价格刷新API
│   │   │   └── tasks.py            # 任务队列API
│   │   ├── strategy/               # A股量化策略引擎
│   │   │   ├── router.py           # 策略API端点
│   │   │   ├── scanner.py          # EOD扫描主编排
│   │   │   ├── indicators.py       # 技术指标（MA/ATR/RS/突破）
│   │   │   ├── universe_filter.py  # 选股池过滤（流动性/ST/黑名单）
│   │   │   ├── flow_confirm.py     # 量价确认评分
│   │   │   ├── risk_sizing.py      # 风险仓位计算
│   │   │   ├── order_lifecycle.py  # 订单生命周期管理
│   │   │   ├── data_feed.py        # AkShare行情数据获取
│   │   │   ├── watchlist_manager.py # 观察池板块扫描
│   │   │   ├── sentiment.py        # 市场情绪评分
│   │   │   ├── event_monitor.py    # 事件监控（财报/解禁）
│   │   │   ├── macro_trend.py      # 宏观趋势状态
│   │   │   ├── cooldown.py         # 止损冷却期
│   │   │   ├── hard_veto.py        # 硬性否决条件
│   │   │   ├── fundamental.py      # 基本面快照
│   │   │   ├── intraday.py         # 盘中15分钟刷新
│   │   │   └── config.py           # 策略配置加载
│   │   └── backtest/               # HV Alpha 回测引擎
│   │       ├── router.py           # 回测API端点（V1 + V2）
│   │       ├── engine.py           # V1 HV Alpha Model（~1300行）
│   │       ├── engine_v2.py        # V2 Risk Protocol 引擎
│   │       └── risk_protocol.py    # 三级风险状态机
│   └── scheduler/
│       └── scheduler.py            # 定时价格刷新调度器
│
├── database/postgres/schema/
│   ├── 005_transactions.sql        # 交易/预算/分类规则表
│   ├── 006_update_accounts.sql     # 账户约束和视图
│   ├── 008_task_queue.sql          # 任务队列表
│   ├── 009_notifications.sql       # 通知推送表
│   ├── 010_strategy.sql            # 策略候选/订单/黑名单/风控表
│   └── 012_watchlist_management.sql # 观察池管理表
│
├── frontend/public/
│   ├── index.html                  # 仪表板首页
│   ├── cashflow.html               # 收支分析页
│   ├── holdings.html               # 持仓汇总页
│   ├── holdings_categories.html    # 持仓分类页
│   ├── holdings_detail.html        # 持仓详情页（配置驱动）
│   ├── strategy.html               # 投资策略页
│   ├── watchlist.html              # 观察池页
│   ├── backtest.html               # 回测页面（V1/V2切换）
│   └── assets/js/
│       ├── utils.js                # 共享工具模块
│       ├── dashboard.js            # 仪表板交互
│       ├── holdings.js             # 持仓汇总
│       ├── holdings_categories.js  # 持仓分类（价格走势）
│       ├── holdings_detail.js      # 持仓详情（A股/美股/加密/贵金属/其他）
│       ├── strategy.js             # 策略候选/订单/扫描交互
│       └── backtest.js             # 回测参数/图表/报告交互
│
├── telegram-bot/
│   ├── bot.py                      # Telegram Bot V3.0（记账+交易识别+推送）
│   ├── Dockerfile                  # 含Tesseract OCR
│   └── ocr/                        # OCR识别模块
│       ├── engine.py               # Tesseract封装
│       ├── broker_detector.py      # 券商识别（关键词评分）
│       └── parsers/                # 券商解析器
│           ├── tonghuashun.py      # 同花顺
│           ├── guangfa.py          # 广发证券
│           ├── ibkr.py             # IBKR盈透
│           └── binance_parser.py   # 币安
│
├── telegram-bot-assistant/
│   └── bot_assistant.py            # Telegram助手Bot（指令桥）
│
├── strategy/
│   └── backtest_pams.py            # 独立回测脚本
│
├── scripts/
│   └── import_holdings_from_excel.py  # Excel持仓导入
│
├── compose.yaml                    # Docker编排（9个服务）
├── VERSION.md                      # 版本历史
└── README.md
```

---

## 快速开始

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
   cp .env.example .env
   # 编辑 .env，设置：
   # - POSTGRES_PASSWORD
   # - TELEGRAM_BOT_TOKEN
   # - OWNER_CHAT_ID（用于每日推送）
   ```

3. **启动服务**
   ```bash
   docker compose up -d
   ```

4. **访问应用**
   - 主应用：`http://pams.local` 或 `http://localhost:80`
   - API文档：`http://localhost:8090/docs`
   - Firefly III：`http://localhost:8082`

### 端口映射

| 服务 | 端口 | 用途 |
|------|------|------|
| Caddy | 80 | 反向代理 |
| pams-web | 8080 | 前端 |
| pams-api | 8090 | FastAPI后端 |
| pams-firefly | 8082 | Firefly III |
| pams-adguard | 53/8081 | DNS |

---

## API文档

### 资产管理

| 端点 | 方法 | 功能 |
|------|------|------|
| `/holdings/latest` | GET | 最新持仓快照 |
| `/holdings/by-category/{cat}` | GET | 按分类持仓 |
| `/summary2/latest` | GET | 仪表板综合数据 |
| `/nav/series` | GET | NAV时间序列 |
| `/nav/drawdown` | GET | 回撤分析 |

### 策略引擎（V1.6）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/strategy/candidates` | GET | 候选标的列表 |
| `/api/strategy/orders` | GET | 订单草稿列表 |
| `/api/strategy/orders/{id}/action` | POST | 确认/取消订单 |
| `/api/strategy/orders/{id}/execute` | POST | 标记订单已执行 |
| `/api/strategy/scan/eod` | POST | 手动触发EOD扫描 |
| `/api/strategy/risk-snapshot` | GET | 风控快照 |
| `/api/strategy/blacklist` | GET/POST | 黑名单管理 |
| `/api/strategy/cooldowns` | GET | 冷却期查询 |
| `/api/strategy/sentiment` | GET | 市场情绪状态 |
| `/api/strategy/events` | GET | 事件监控列表 |
| `/api/strategy/watchlist/scan` | POST | 观察池板块扫描 |
| `/api/strategy/intraday/refresh` | POST | 盘中15分钟刷新 |

### 回测引擎（V1.7）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/backtest/run` | POST | V1 HV Alpha 回测 |
| `/api/backtest/run_v2` | POST | V2 Risk Protocol 回测 |
| `/api/backtest/watchlist` | GET | 默认股票池 |

### 交易识别（V1.5）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/holdings/trade` | POST | 持仓交易UPSERT |

### 通知推送（V1.5）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/notifications` | POST | 创建通知 |
| `/api/notifications/pending` | GET | 获取待发送通知 |
| `/api/notifications/{id}/sent` | PATCH | 标记已发送 |

### 收支管理

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/transactions` | POST | 创建交易 |
| `/api/transactions/recent` | GET | 最近交易 |
| `/api/transactions/monthly/{month}` | GET | 月度统计 |
| `/api/transactions/budgets/{month}` | GET | 预算执行 |

### 价格与汇率

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/fx-rates/latest` | GET | 最新汇率 |
| `/api/fx-rates/refresh` | POST | 刷新汇率 |
| `/api/prices/crypto` | POST | 刷新加密价格 |
| `/api/prices/us-stock` | POST | 刷新美股价格 |

**完整API文档**：`http://localhost:8090/docs`

---

## Telegram Bot 使用

### 快速记账
```
-50 餐饮          → Santander/MXN 支出
+5000 工资        → Santander/MXN 收入
```

### 交易识别
```
买入 贵州茅台 100股 @1800     → 同花顺账户
卖出 AAPL 50 @195.5          → IBKR账户
buy BTC 0.5 @97000           → 币安账户
```
或直接发送券商持仓截图（同花顺/广发/IBKR/币安）。

### 命令
- `/start` - 帮助信息
- `/record` - 详细记账（选账户/币种/分类）
- `/portfolio` - 持仓概览
- `/stats` - 本月统计
- `/list` - 最近记录

---

## 下一步计划

### V1.7 - 回测引擎 + Risk Protocol ✅
- [x] HV Alpha V1 回测引擎（20日突破 + ATR止损 + 金字塔加仓）
- [x] HV Alpha V2 Risk Protocol 三级风险状态机
- [x] 回测前端页面（策略切换、参数面板、图表、报告）

### V1.6 - A股策略引擎 ✅
- [x] 多阶段选股流水线（Universe → 趋势 → 回踩 → 触发 → 量价确认）
- [x] 订单生命周期管理 + 风险仓位计算
- [x] 盘中15分钟刷新 + Telegram推送
- [x] 观察池板块扫描 + 退出条件检查

### V1.5 - 交易Bot + 前端重构 ✅
- [x] Telegram交易识别Bot（OCR + 文字指令）
- [x] 每日持仓推送 + 通知框架
- [x] 持仓子页面合并（配置驱动）
- [x] 定时价格调度器

### V1.8 - 外部集成（计划中）
- [ ] Binance API 实时数据同步
- [ ] OCR 精度调优（真实截图测试）
- [ ] 回测参数优化（网格搜索 + 蒙特卡洛）

### V2.0 - 长期目标
- [ ] 移动端适配 + PWA
- [ ] 多用户支持
- [ ] 机器学习分类
- [ ] 数据导出 / PDF报表

---

## 许可证

MIT License - 详见 [LICENSE](LICENSE)

---

**Built with ❤️ by hzfzzzi**
