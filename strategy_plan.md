# PAMS 策略系统实施路线图

> 基于 IPS v1.1 (`strategy-gpt/strategy_config.json`) 的完整能力规划
> 最后更新: 2026-02-16

---

## 系统全景

IPS v1.1 定义了一套 **三层配置 × 多市场 × 全链路风控** 的投资策略系统。下表列出所有能力模块及当前实现状态。

| # | 能力模块 | IPS 定义 | 当前状态 | 阶段 |
|---|---------|---------|---------|------|
| 1 | A股 EOD 扫描流水线 | satellite_cn.technical_engine | ✅ 已上线 | P0 |
| 2 | Universe 过滤 (ST/黑名单/流动性) | satellite_cn.universe | ✅ 已上线 | P0 |
| 3 | 趋势门 (MA20>MA60 + 回调区间 + 触发日) | satellite_cn.technical_engine.trend_gate | ✅ 已上线 | P0 |
| 4 | 量价确认 (PASS/WEAK/FAIL) | satellite_cn.flow_confirm | ✅ 已上线 | P0 |
| 5 | 仓位计算 (风险预算 + 100股整手) | portfolio.risk_budget | ✅ 已上线 | P0 |
| 6 | 订单草稿 + 前端确认/取消 | execution | ✅ 已上线 | P0 |
| 7 | Telegram 通知 | — | ✅ 已上线 | P0 |
| 8 | 月度熔断 (pause/reduce_only) | portfolio.monthly_circuit_breakers | ✅ 已上线 | P1 |
| 9 | Risk Snapshot 自动刷新 | — | ✅ 已上线 (risk_snapshot.py) | P1 |
| 10 | 并发风险上限 | portfolio.risk_budget.concurrent_risk_pct_cap | ✅ 已上线 (RC_R101) | P1 |
| 11 | 主题/行业敞口上限 | portfolio.risk_budget.theme_exposure_pct_cap | ✅ 已上线 (RC_R102) | P1 |
| 12 | Hard Veto (5 条规则) | satellite_cn.hard_veto | ✅ 已上线 (hard_veto.py) | P1 |
| 13 | 宏观趋势状态 (SPX 10月MA) | core_policy.trend_filter | ✅ 已上线 (macro_trend.py) | P1 |
| 14 | 订单生命周期 (confirmed→executed) | — | ✅ 已上线 (order_lifecycle.py) | P1 |
| 15 | 基本面质量评分 | satellite_cn.fundamental_gate | ❌ 配置完整，零实现 | P2 |
| 16 | 行业景气度检测 | satellite_cn.fundamental_gate.signals.industry_regime | ❌ 零实现 | P2 |
| 17 | 事件风险监控 | satellite_cn.fundamental_gate.signals.event_risks | ❌ 零实现 | P2 |
| 18 | Confirm Score 加权公式 | satellite_cn.confirm_score | ⚠️ 简化版 (50 + flow×0.5) | P2 |
| 19 | 停牌/异常停牌过滤 | universe.exclude.long_suspension | ❌ RC_U102 未实现 | P2 |
| 20 | 情绪分析 (HOT/NEUTRAL/PANIC) | satellite_cn.sentiment | ❌ 配置完整，零实现 | P3 |
| 21 | 市场宽度 (涨跌家数/涨停跌停) | sentiment.sources.market_breadth | ❌ 零实现 | P3 |
| 22 | 新闻情绪 (东财/金十/新浪) | sentiment.sources.news_providers | ❌ 零实现 | P3 |
| 23 | 搜索趋势 (百度/微信/Google) | sentiment.sources.search_trends | ❌ 零实现 | P3 |
| 24 | 社交热度 (X/微博) | sentiment.sources.social_heat | ❌ 零实现 | P3 |
| 25 | 盘中 15 分钟刷新 | technical_engine.scan_schedule.intraday_refresh | ❌ 仅 EOD | P3 |
| 26 | 冷却期 (止损后 5 日 / 双止损黑名单 20 日) | satellite_cn.positioning.cooldowns | ❌ 零实现 | P3 |
| 27 | 美股卫星策略 | satellite_overseas_stocks | ❌ 框架定义，零实现 | P4 |
| 28 | IBKR 数据接入 | data_sources.pricing_us | ❌ 零实现 | P4 |
| 29 | SEC EDGAR 事件 | data_sources.fundamentals_us | ❌ 零实现 | P4 |
| 30 | 跨市场主题敞口控制 | portfolio.risk_budget.theme_exposure_pct_cap | ❌ 零实现 | P4 |
| 31 | 加密货币 BTC/ETH | venture_crypto | ❌ 框架定义，零实现 | P5 |
| 32 | Binance API 自动执行 | execution.venues.CRYPTO | ❌ 零实现 | P5 |
| 33 | 资金费率 / OI 监控 | venture_crypto.flow_sentiment | ❌ 零实现 | P5 |
| 34 | Kill Switch | execution.venues.CRYPTO.kill_switch | ❌ 零实现 | P5 |
| 35 | 配置带宽再平衡 | core_policy.alloc_bands | ❌ 零实现 | P6 |
| 36 | 干火药部署 | core_policy.dry_powder | ❌ 零实现 | P6 |
| 37 | Attack Mode 切换 | satellite_cn.positioning.attack_switch | ❌ 零实现 | P6 |
| 38 | 交易历史 + P&L 归因 | — | ❌ 无表无逻辑 | P6 |
| 39 | 回测框架 | — | ❌ 零实现 | P6 |

---

## P0 · A股卫星策略 EOD 扫描 ✅ 已完成

> **里程碑**: 从零到可运行的 A 股策略扫描系统，支持手动和定时触发。

### 已完成清单

**后端模块** (`backend/api/strategy/`)

| 文件 | 行数 | 功能 |
|------|------|------|
| `config.py` | ~147 | 配置加载 + 69 Reason Codes + B_NAV/风险参数提取 |
| `models.py` | ~90 | Pydantic 模型: OHLCVBar, CandidateResult, OrderDraft 等 7 个 |
| `data_feed.py` | ~150 | AkShare OHLCV 获取 + 批量 UPSERT + DB 读取 |
| `indicators.py` | ~250 | MA20/MA60/ATR14 计算 + 趋势门 + 回调区间 + 触发日 + 入场止损 |
| `universe_filter.py` | ~120 | 从持仓+观察池收集 → ST过滤 → 黑名单 → 流动性筛选 |
| `flow_confirm.py` | ~108 | 冲击段/回调段量能分析 → PASS/WEAK/FAIL |
| `risk_sizing.py` | ~160 | 风控状态读取 + 仓位计算 (100股整手 + 市值上限) |
| `scanner.py` | ~400 | EOD 主编排: config → universe → OHLCV → 逐标的扫描 → 订单 → 通知 |
| `router.py` | ~350 | 12 个 API 端点 (候选/订单/黑名单/风控/扫描) |

**数据库** (`database/postgres/schema/010_strategy.sql`)

| 表名 | 用途 | 状态 |
|------|------|------|
| `pams_ohlcv_daily` | OHLCV 日线 | ✅ 有数据 |
| `pams_strategy_candidates` | 候选标的 | ✅ 有数据 (4 条 TRIGGERED) |
| `pams_order_drafts` | 订单草稿 | ✅ 有数据 (4 笔 pending) |
| `pams_strategy_blacklist` | 黑名单 | ✅ 可用 (空) |
| `pams_strategy_state` | 宏观趋势状态 | ⚠️ 已建表，未填充 |
| `pams_risk_snapshot` | 风控快照 | ⚠️ 已建表，未填充 |

**前端** (`frontend/public/`)
- `strategy.html` — 扫描 KPI + 风控卡片 + 候选表格 (11列) + 待确认订单区
- `strategy.js` — 数据拉取 + 渲染 + 订单确认/取消交互

**调度器**
- `scheduler.py` — 每日 15:30 CST 触发 `POST /api/strategy/scan/eod`

**依赖**
- `requirements.txt` — 新增 `akshare>=1.14.0` + `pandas>=2.2.0`
- `Dockerfile` — 新增 `COPY strategy/ ./strategy/`

### P0 已知限制

1. **Risk Snapshot 空表** — `pams_risk_snapshot` 从未被写入，熔断检查始终返回默认值
2. **并发风险未校验** — `concurrent_risk_pct_cap` 配置已读取但 `get_current_risk_state()` 未做加仓检查
3. **Hard Veto 未实现** — `hard_veto_hit` 字段始终为 `false`
4. **Confirm Score 简化** — 当前公式 `50 + flow_score × 0.5`，未包含情绪和基本面权重
5. **无盘中刷新** — 仅 EOD 一次性扫描
6. **无冷却期** — 止损后无 5 日冷却、无双止损黑名单
7. **订单无 executed 状态** — confirmed 后无后续追踪

---

## P1 · 风控闭环 + 宏观趋势

> **里程碑**: 系统能自动计算实时风控状态，具备生产级安全护栏，订单可闭环追踪。

### P1.1 Risk Snapshot 自动刷新

**目标**: 每日从持仓数据自动计算并写入 `pams_risk_snapshot`。

**新增文件**: `backend/api/strategy/risk_snapshot.py` (~150行)

| 功能 | 说明 |
|------|------|
| `refresh_risk_snapshot(conn)` | 从 `pams_holdings_snapshot` 计算 B_NAV、持仓数、并发风险 |
| B_NAV 计算 | 总市值 - 非可交易资产 (华为股权、公积金、应收款) |
| 并发风险 | Σ(position_risk) / B_NAV，其中 position_risk = 持仓量 × (entry - stop) |
| 月度回撤 | 当月 NAV 最高点 vs 当前 NAV |
| 熔断状态 | drawdown ≥ 4% → pause, ≥ 7% → reduce_only |

**修改**: `scheduler.py` — 在每日价格刷新后触发 risk snapshot 刷新

### P1.2 并发风险 + 主题敞口上限

**修改文件**: `backend/api/strategy/risk_sizing.py`

| 检查 | 参数 | Reason Code |
|------|------|-------------|
| 并发风险上限 | concurrent_risk_pct_cap: defensive=2%, attack=3% | RC_R101_CONCURRENT_RISK_CAP |
| 主题敞口上限 | theme_exposure_pct_cap: defensive=25%, attack=35% | RC_R102_THEME_EXPOSURE_CAP |

**逻辑**: 新开仓前检查 current_concurrent + new_trade_risk ≤ cap

### P1.3 Hard Veto 实现

**新增文件**: `backend/api/strategy/hard_veto.py` (~120行)

6 条 Hard Veto 规则:

| 规则 | 条件 | Reason Code |
|------|------|-------------|
| V001 | LOW < MA60 AND 成交量放大 | RC_V001_BREAK_MA60_VOL_SPIKE |
| V002 | CLOSE < 回调区间下沿 | RC_V002_CLOSE_BELOW_PULLBACK_BAND |
| V003 | T+1 内重大负面事件 | RC_V003_MAJOR_NEG_EVENT_TPLUS1 |
| V004 | 连续跌停 / 流动性枯竭 | RC_V004_LIQUIDITY_CRASH |
| V005 | 恐慌情绪 + 指数破位 | RC_V005_PANIC_AND_INDEX_BREAK |
| V006 | 黑名单命中 | RC_V006_BLACKLIST |

**修改**: `scanner.py` — 在 TRIGGERED 阶段后插入 hard_veto 检查

### P1.4 宏观趋势状态 (SPX 10月MA)

**新增文件**: `backend/api/strategy/macro_trend.py` (~100行)

| 功能 | 说明 |
|------|------|
| `update_trend_state(conn)` | 获取 SPX 月线 → 计算 10月MA → 判定 RISK_ON/NEUTRAL/OFF |
| 数据源 | yfinance (`^GSPC`) 或 Stooq 回退 |
| 滞后 | 2 个月 hysteresis (防止频繁切换) |
| 写入 | `pams_strategy_state` 表 |

**影响**: 趋势状态影响配置带宽 (satellite_pct 范围) 和干火药比例

### P1.5 订单生命周期管理

**修改**: `router.py` + 新增 `order_lifecycle.py`

| 状态 | 转换 | 说明 |
|------|------|------|
| pending | → confirmed / cancelled | 用户前端操作 (已实现) |
| confirmed | → executed / expired | 用户手动标记已执行 / 到期自动过期 |
| executed | → (更新持仓) | 关联到 holdings_snapshot |
| expired | (终态) | valid_until 过期自动标记 |

**新增 API**:
- `POST /api/strategy/orders/{id}/execute` — 标记已执行 + 可选录入实际成交价
- 定时任务: 每日检查 valid_until 过期订单 → 自动标记 expired

### P1 验证标准

- [ ] `pams_risk_snapshot` 每日自动填充，前端风控卡片显示实时数据
- [ ] 并发风险超限时，scanner 拒绝生成新订单 (RC_R101)
- [ ] Hard Veto 命中时，candidate.hard_veto_hit = true，eligible = false
- [ ] `pams_strategy_state` 显示当前 SPX 趋势状态
- [ ] 订单可流转到 executed/expired 状态

---

## P2 · 基本面门控 + 事件驱动

> **里程碑**: 候选标的经过基本面质量筛查和事件风险过滤，Confirm Score 采用完整加权公式。

### P2.1 基本面质量评分

**新增文件**: `backend/api/strategy/fundamental_gate.py` (~200行)

| 信号 | 数据源 | 规则 | Reason Code |
|------|--------|------|-------------|
| 现金流趋势 | TusharePro / 东财 | 8 季度无持续恶化 | RC_F101_CASHFLOW_DETERIORATE |
| 行业景气度 | 行业指数趋势 | 不处于明确下行趋势 | RC_F102_INDUSTRY_DOWNTREND |
| 质量评分 | 综合 ROE/毛利率/负债率 | quality_score ∈ [0, 100] | RC_F001_QUALITY_SCORE_HIGH |

**配置引用**: `satellite_cn.fundamental_gate.signals`

### P2.2 事件风险监控

**新增文件**: `backend/api/strategy/event_monitor.py` (~150行)

| 事件类型 | 数据源 | Reason Code |
|---------|--------|-------------|
| 业绩预亏/预警 | CNINFO / 东财公告 | RC_F201_EVENT_PRELOSS |
| 立案调查 / 重大处罚 | CNINFO / 东财公告 | RC_F202_EVENT_INVESTIGATION |
| 异常停牌 | AkShare 停复牌数据 | RC_F203_EVENT_ABNORMAL_HALT |

**修改**: `universe_filter.py` — 新增 `filter_suspension()` (RC_U102_EXCLUDE_SUSP)

### P2.3 Confirm Score 增强

**修改**: `scanner.py` 中的评分逻辑

当前 (P0):
```
confirm_score = 50 + flow_score × 0.5
```

目标 (P2):
```
confirm_score = flow_score × 0.5 + sent_score × 0.3 + fundamental_score × 0.2
```

| 评分区间 | 仓位策略 |
|---------|---------|
| < 55 | REJECT (不建仓) |
| 55–70 | HALF_SIZE (半仓) |
| 70–85 | NORMAL_SIZE (标准仓位) |
| ≥ 85 | ATTACK_SIZE (进攻仓位) |

### P2 验证标准

- [ ] 候选标的展示 quality_score 和基本面 Reason Codes
- [ ] 业绩预亏标的被 RC_F201 过滤
- [ ] Confirm Score 公式包含三权重
- [ ] 停牌标的被 RC_U102 过滤

---

## P3 · 情绪分析 + 盘中刷新

> **里程碑**: 系统具备多源市场情绪判断能力，候选标的盘中实时跟踪。

### P3.1 市场情绪引擎

**新增文件**: `backend/api/strategy/sentiment.py` (~250行)

| 数据源 | 指标 | 说明 |
|--------|------|------|
| 市场宽度 | 涨跌家数、涨停跌停数 | AkShare 实时数据 |
| 新闻情绪 | 东财/金十/新浪财经 | 关键词 + NLP 分类 |
| 搜索趋势 | 百度指数 / 微信指数 | Top 10 热词 |
| 社交热度 | X / 微博 | 话题集中度 |

**输出**: `sent_state` (HOT/NEUTRAL/PANIC) + `sent_score` (55/70/50)

**规则**:
- HOT → position_cap_multiplier = 0.5× (仓位减半)
- PANIC → 必须 flow_confirm=PASS + 强制 defensive 模式

### P3.2 盘中 15 分钟刷新

**新增/修改**: `scheduler.py` + `scanner.py`

| 参数 | 值 |
|------|---|
| 刷新间隔 | 15 分钟 |
| 作用范围 | 仅 PULLBACK_ZONE 和 TRIGGERED 候选 |
| 交易时段 | 09:30-11:30, 13:00-15:00 CST |
| 功能 | 更新指标 → 重新检查触发条件 → 动态生成/撤销订单 |

### P3.3 冷却期系统

**新增文件**: `backend/api/strategy/cooldown.py` (~80行)

| 规则 | 参数 | 说明 |
|------|------|------|
| 止损后冷却 | 5 个交易日 | 同标的不可再触发 |
| 双止损黑名单 | 20 个交易日 | 同标的连续 2 次止损 → 自动加入黑名单 |

**依赖**: 需要 P1.5 的订单生命周期 (tracked stop-out events)

### P3 验证标准

- [ ] 前端策略页展示当前市场情绪状态 (HOT/NEUTRAL/PANIC)
- [ ] HOT 状态下仓位自动减半
- [ ] PULLBACK_ZONE 候选在盘中被实时检测到触发
- [ ] 止损 5 日内同标的不再出现在候选中

---

## P4 · 美股卫星策略

> **里程碑**: 将 P0 的回调框架复制到美股市场，支持 IBKR 数据和 SEC 事件。

### P4.1 IBKR 数据接入

**新增文件**: `backend/api/strategy/data_feed_us.py` (~150行)

| 功能 | 数据源 |
|------|--------|
| US OHLCV 日线 | IBKR Market Data / yfinance 回退 |
| 流动性指标 | ADV ≥ $20M USD (20日均成交额) |
| 基本面 | SEC EDGAR + 财报电话会议 |

### P4.2 美股策略引擎

**复用 P0 框架**: 趋势门 + 回调区间 + 触发日 + 量价确认 + 仓位计算

**差异参数** (来自 `satellite_overseas_stocks.differences`):

| 参数 | CN_A | US_STOCKS |
|------|------|-----------|
| 流动性门槛 | ¥200M 成交额 | $20M ADV |
| 情绪权重 | 多源均匀 | Bloomberg 40% + SEC 20% + 财报 20% + 趋势 20% |
| 执行 | 手动确认 (THS) | 手动确认 (IBKR) |

### P4.3 跨市场主题敞口

**修改**: `risk_sizing.py`

- 同一主题 (如"新能源") 在 CN + US 的总敞口不得超过 theme_exposure_pct_cap
- 需要行业/主题映射表

### P4 验证标准

- [ ] 美股 Universe 独立筛选并生成候选
- [ ] 美股候选使用 USD 计价，仓位按 B_NAV(CNY) 换算
- [ ] 跨市场同主题敞口受限

---

## P5 · 加密货币风控

> **里程碑**: BTC/ETH 纳入策略系统，具备资金费率 + OI 特有风控，支持自动执行。

### P5.1 Binance 数据 + 执行

**新增文件**: `backend/api/strategy/data_feed_crypto.py` + `crypto_executor.py`

| 功能 | 说明 |
|------|------|
| OHLCV | Binance K线 API |
| 资金费率 | ≥ 0.08% → block_or_half (RC_C001) |
| OI 监控 | OI 飙升 >15% + 价格停滞 → de_risk (RC_C002) |
| 自动执行 | manual_confirm=false，Binance 下单 |
| Kill Switch | 紧急全平功能 (RC_E101) |

### P5.2 加密货币风险参数

来自 `venture_crypto`:

| 参数 | 值 |
|------|---|
| 单笔风险 | 0.3% (低于股票的 0.5%) |
| 单标的上限 | 4% NAV |
| 总加密上限 | 10% NAV |
| 标的 | BTC, ETH + 最多 5 个 |
| 熔断 | 同股票 (4%/7%) |

### P5 验证标准

- [ ] BTC/ETH 出现在候选列表中
- [ ] 资金费率过热时自动阻断或减半
- [ ] 总加密敞口不超过 10% NAV
- [ ] Kill Switch 可一键全平

---

## P6 · 组合再平衡 + 绩效

> **里程碑**: 系统具备完整的组合管理能力，支持回测和绩效归因。

### P6.1 配置带宽再平衡

来自 `core_policy.alloc_bands`:

| 趋势状态 | Core | Satellite | Venture | Cash Min |
|---------|------|-----------|---------|----------|
| RISK_ON | 60-70% | 20-30% | 5-10% | 5% |
| RISK_NEUTRAL | 55-65% | 20-25% | 5-8% | 8% |
| RISK_OFF | 45-55% | 10-20% | 0-5% | 20% |

偏离 `rebalance_band` (10%) 时触发再平衡草案。

### P6.2 干火药部署

来自 `core_policy.dry_powder`:

| 指数回撤 | 部署比例 |
|---------|---------|
| ≥ 10% | 干火药的 25% |
| ≥ 20% | 干火药的 35% |
| ≥ 30% | 干火药的 40% |

### P6.3 Attack Mode 切换

来自 `satellite_cn.positioning.attack_switch`:

**切换条件** (全部满足):
1. CORE_STATE == RISK_ON
2. SENT_STATE != HOT
3. 最近 20 笔胜率 ≥ 50%
4. 最近 20 笔盈亏比 ≥ 1.2
5. 当前回撤 < 5%

最多 2 个 Attack 仓位，单笔风险 0.8%。

### P6.4 交易历史 + P&L 归因

**新增表**: `pams_trade_history`
- 记录每笔交易的入场/出场/持仓天数/P&L
- 计算胜率、盈亏比、最大回撤
- 供 Attack Mode 切换条件和前端绩效页使用

### P6.5 回测框架

- 基于历史 OHLCV 数据重放策略
- 输出: 总收益、最大回撤、Sharpe、胜率、盈亏比
- 用于参数优化和策略验证

### P6 验证标准

- [ ] 当前 Core/Satellite/Venture 配比可视化
- [ ] 偏离再平衡带宽时生成草案
- [ ] 指数大跌时自动触发干火药部署建议
- [ ] Attack Mode 满足条件时自动切换
- [ ] 交易历史页面展示完整 P&L

---

## 阶段依赖关系

```
P0 A股EOD扫描 ✅
 │
 ├─ P1 风控闭环 + 宏观趋势
 │   ├─ P1.1 Risk Snapshot (被 P1.2/P1.3 依赖)
 │   ├─ P1.2 并发风险/主题敞口
 │   ├─ P1.3 Hard Veto
 │   ├─ P1.4 SPX 宏观趋势 (被 P6.1 依赖)
 │   └─ P1.5 订单生命周期 (被 P3.3/P6.3 依赖)
 │
 ├─ P2 基本面 + 事件 (依赖 P1)
 │   ├─ P2.1 基本面评分
 │   ├─ P2.2 事件监控
 │   └─ P2.3 Confirm Score 增强 (依赖 P3.1 的 sent_score)
 │
 ├─ P3 情绪 + 盘中 (依赖 P1)
 │   ├─ P3.1 情绪引擎
 │   ├─ P3.2 盘中刷新
 │   └─ P3.3 冷却期 (依赖 P1.5)
 │
 ├─ P4 美股 (依赖 P1 + P2)
 │
 ├─ P5 加密 (依赖 P1)
 │
 └─ P6 组合再平衡 (依赖 P1.4 + P1.5 + P4 + P5)
```

---

## 数据源清单

| 阶段 | 新增数据源 | 用途 | 是否免费 |
|------|-----------|------|---------|
| P0 | AkShare | A股 OHLCV (前复权) | ✅ 免费 |
| P1 | yfinance / Stooq | SPX 月线 | ✅ 免费 |
| P1 | 持仓数据 (内部) | B_NAV / 并发风险计算 | ✅ 内部 |
| P2 | TusharePro | A股基本面 (财务报表) | ⚠️ 需 Token |
| P2 | 东财公告 / CNINFO | 事件风险 | ✅ 免费 |
| P3 | AkShare 市场宽度 | 涨跌家数/涨停跌停 | ✅ 免费 |
| P3 | 百度指数 / 微信指数 | 搜索趋势 | ⚠️ 需爬虫/API |
| P3 | 东财/新浪舆情 | 新闻情绪 | ⚠️ 需爬虫 |
| P4 | IBKR API | 美股行情 + 执行 | ⚠️ 需账户 |
| P4 | SEC EDGAR | 美股公告 | ✅ 免费 |
| P5 | Binance API | 加密行情 + 执行 | ✅ 免费 |
| P5 | Coinglass | 资金费率 / OI | ⚠️ 需 API Key |

---

## Reason Codes 覆盖矩阵

| 类别 | Code 范围 | 总数 | P0 已用 | P1 新增 | P2+ 新增 |
|------|----------|------|---------|---------|---------|
| Universe (U) | RC_U001–U201 | 5 | 4 | 0 | 1 (U102) |
| Fundamental (F) | RC_F001–F203 | 6 | 0 | 0 | 6 |
| Technical (T) | RC_T001–T402 | 8 | 7 | 0 | 1 (T402) |
| Flow (FL) | RC_FL001–FL203 | 6 | 6 | 0 | 0 |
| Sentiment (S) | RC_S001–S201 | 4 | 0 | 0 | 4 |
| Hard Veto (V) | RC_V001–V006 | 6 | 0 | 6 | 0 |
| Risk (R) | RC_R001–R202 | 7 | 4 | 2 (R101/R102) | 1 |
| Crypto (C) | RC_C001–C102 | 4 | 0 | 0 | 4 |
| Execution (E) | RC_E001–E101 | 4 | 2 | 1 (E003) | 1 |
| **合计** | | **50** | **23** | **9** | **18** |

---

## 当前系统架构

```
┌─────────────────────────────────────────────────┐
│  Frontend (nginx:8080)                          │
│  strategy.html → 候选表格 + 订单卡片 + 风控KPI  │
└──────────────────────┬──────────────────────────┘
                       │ HTTP
┌──────────────────────▼──────────────────────────┐
│  Caddy (:80) — 反向代理                          │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│  FastAPI (pams-api:8090)                        │
│                                                 │
│  /api/strategy/candidates    GET                │
│  /api/strategy/orders        GET/POST           │
│  /api/strategy/blacklist     GET/POST/DELETE     │
│  /api/strategy/risk-snapshot GET                │
│  /api/strategy/state         GET                │
│  /api/strategy/scan/eod      POST               │
│  /api/strategy/scan/latest   GET                │
│                                                 │
│  strategy/                                      │
│  ├── config.py        配置 + Reason Codes       │
│  ├── data_feed.py     AkShare OHLCV             │
│  ├── indicators.py    MA/ATR/趋势门/回调/触发    │
│  ├── universe_filter.py  ST/黑名单/流动性        │
│  ├── flow_confirm.py  量价确认                   │
│  ├── risk_sizing.py   仓位计算                   │
│  ├── scanner.py       EOD 编排                   │
│  ├── router.py        API 端点                   │
│  └── models.py        Pydantic                  │
└──────────────────────┬──────────────────────────┘
                       │ SQL
┌──────────────────────▼──────────────────────────┐
│  PostgreSQL 16                                  │
│  pams_ohlcv_daily          ✅                   │
│  pams_strategy_candidates  ✅                   │
│  pams_order_drafts         ✅                   │
│  pams_strategy_blacklist   ✅                   │
│  pams_strategy_state       ⚠️ 空               │
│  pams_risk_snapshot        ⚠️ 空               │
└─────────────────────────────────────────────────┘
```
