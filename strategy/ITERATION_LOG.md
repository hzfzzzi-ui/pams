# Strategy Iteration Log

This file tracks 5-min internal reviews + 10-min shareholder updates during iterative strategy development.

## 2026-02-13
- (init) Start 4-hour iteration window: build A股/美股 watchlist+signals+daily strategy spec; converge to executable draft actions.

### 22:47 Internal 5-min review
**What’s weak / missing (strict shareholder lens):**
1) **No concrete universe definition** yet (CN/US candidate set). Need explicit: e.g., HS300+CSI500 for CN; S&P500 for US; plus exclusions.
2) **Signals not fully specified** with thresholds (e.g., MA periods, valuation percentile rules). Must set default parameters and document them.
3) **No daily data freshness SLA**: when prices/fx update, what if missing? Need fail-safe: use last available close + mark completeness.
4) **No position sizing rule** beyond caps. Need v1 sizing: starter tranche size, add-on tranche, max number of names, cash buffer.
5) **No conflict-resolution policy** when signals disagree (cheap but downtrend; breakout but expensive). Need tie-break rules.
6) **No mapping between balance-sheet categories and tradable tickers**: bottom-table has funds/positions without tickers; strategy execution requires symbol mapping table.
7) **No explicit risk budget per market** (A/US) inside overall allocation target; need drift-band and rebalancing cadence.

**Immediate fixes to implement next:**
- Write v1 parameter sheet (thresholds) into DB/rules JSON.
- Add `universe` config per market + a deterministic shortlist pipeline.
- Add `position_sizing` v1: starter=1/10 of target position, add on pullbacks, keep cash buffer.
- Add `completeness` flags in strategy output.

### 22:52 10-min update (iteration)
**Progress**
- 将“策略/观察池 v1.0”从概念推进到可落地的参数清单（待写入 rules.json）：
  - 趋势：MA20/MA60；20>60 且价>60 为趋势OK
  - 回撤买点：从60日高点回撤 8%~15% 进入观察买入区（价值风格，避免追高）
  - 风控强制：单标成本 -30% → 减仓到原数量70%
  - 仓位上限：US 30% / CN 50%（单标），不使用杠杆/期权

**Current executable strategy draft (today, v1 placeholder)**
- 由于目前库内只有“资产负债表快照”，缺少可交易标的 ticker 映射与最新行情，今天策略只能输出“风控/结构动作草案”，无法给出具体买卖标的：
  1) 先补齐 A股/美股持仓的 ticker 映射表（symbol_map），以及 watchlist 的 candidate universe。
  2) 在映射完成前：只执行“风险检查 + 资产配置偏离提示”，不生成买入单。

**Open issues / risks**
- 无 ticker 映射 → 无法对接行情 → 无法产生可执行的入池10只/每日信号。
- watchlist 的候选宇宙尚未落地（HS300/CSI500 vs 自选；S&P500 vs 自选）。

**Next 10-min plan**
- 设计并落地 `symbol_map`（底表标的名称→market+symbol）结构 + 录入最小集合（同花顺 A股4只先对齐）。
- 落地 universe 配置（CN/US）为 rules.json，并写入 iteration log。

### 22:57 Internal 5-min review
**Shareholder critique:**
- 10分钟汇报已发出，但“可执行”仍停留在风控框架层；股东可能认为没有产出。
- 必须在接下来 1-2 个 tick 内交付：
  1) 至少把“同花顺4只A股持仓”映射成标准代码（000xxx.SZ / 60xxxx.SH）
  2) 给出今日基于成本/市值的硬风控检查结果（是否触发-30%、是否超单标上限）
  3) 定义 watchlist 宇宙的最小可运行版本（先用手工白名单10只/市场，后续再爬取）
- 同时要写清“为什么现在不能自动选10只”（数据源/无key/无行情表），并给出可行的技术路径与时间。
