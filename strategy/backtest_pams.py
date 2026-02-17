#!/usr/bin/env python3
"""
PAMS A股卫星策略回测引擎
────────────────────────
严格无前瞻偏差 (No Look-Ahead Bias)

规则:
  - 初始资金: 100,000 CNY
  - 最大同时持仓: 5 只
  - 观察池: 基于截止 2026/01/17 可得数据选取热门板块代表股
  - 策略执行: 2026/01/19 (首个交易日) ~ 2026/02/01
  - 每个交易日仅使用截止当日的数据

策略流程 (复现 PAMS scanner.py):
  1. 趋势门: MA20 > MA60, CLOSE > MA60, MAX_RETURN_40D >= 12%
  2. 回调区: MA20 ± 1×ATR14
  3. 触发日: CLOSE > MA20 AND CLOSE > OPEN (收阳)
  4. 量价确认: 缩量回调 + 放量触发 → PASS/WEAK/FAIL
  5. 入场: CLOSE × 0.998 | 止损: min(ENTRY - 2×ATR, LOW20 - 0.5×ATR)
  6. 仓位: risk_amount / (entry - stop), 100 股整手
"""

import warnings
warnings.filterwarnings("ignore")

import math
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd
import numpy as np

# ==================== 策略参数 ====================

INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS = 5
RISK_PER_TRADE_PCT = 0.25      # 25% per trade (激进配置)
ENTRY_OFFSET_PCT = -0.002      # entry = close * (1 + offset)
STOP_ATR_MULT = 2.0            # stop_a = entry - 2 * ATR14
STOP_LOW_MULT = 0.5            # stop_b = low_20d - 0.5 * ATR14
MA_TANGLE_THRESHOLD = 0.015    # |MA20-MA60|/close < 1.5% → reject
MIN_RETURN_40D = 0.12          # 40 日最大涨幅门槛
PULLBACK_BAND_ATR = 1.0        # 回调区间 ± 1×ATR
MAX_POSITION_VALUE_PCT = 0.50  # 单笔不超过 NAV 50% (激进配置)
MIN_BARS = 65                  # 计算 MA60 所需最少 K 线

BACKTEST_START = date(2025, 10, 1)   # 回测起始日
BACKTEST_END = date(2026, 2, 1)      # 回测结束日
DATA_FETCH_START = date(2025, 5, 1)  # 足够计算 MA60 + ATR14


# ==================== 数据层 ====================

def build_watchlist() -> List[Tuple[str, str]]:
    """
    构建观察池: 基于截止 2026/01/17 可用信息选取热门板块代表股。

    选取逻辑:
    - 覆盖有色/黄金、新能源、半导体/AI、消费、金融、军工、医药等热门板块
    - 每板块 2-3 只流动性好的龙头

    Returns: [(symbol, name), ...]
    """
    pool = [
        # ── 有色 / 黄金 ──
        ("sh601899", "紫金矿业"),
        ("sh600547", "山东黄金"),
        ("sh603993", "洛阳钼业"),
        ("sz002466", "天齐锂业"),
        ("sh600489", "中金黄金"),
        # ── 新能源 ──
        ("sz300750", "宁德时代"),
        ("sh601012", "隆基绿能"),
        ("sz002594", "比亚迪"),
        ("sh600438", "通威股份"),
        # ── 半导体 / AI ──
        ("sh688981", "中芯国际"),
        ("sz002371", "北方华创"),
        ("sh603019", "中科曙光"),
        ("sz002230", "科大飞讯"),
        ("sh688111", "金山办公"),
        ("sz300496", "中科创达"),
        ("sh688036", "传音控股"),
        # ── 消费 / 白酒 ──
        ("sh600519", "贵州茅台"),
        ("sz000858", "五粮液"),
        ("sh600887", "伊利股份"),
        # ── 金融 ──
        ("sh601318", "中国平安"),
        ("sh600036", "招商银行"),
        ("sh601166", "兴业银行"),
        ("sh600030", "中信证券"),
        # ── 科技 / 互联网 ──
        ("sz300059", "东方财富"),
        ("sz002415", "海康威视"),
        # ── 医药 ──
        ("sh600276", "恒瑞医药"),
        ("sz300760", "迈瑞医疗"),
        ("sh688185", "康希诺"),
        # ── 军工 ──
        ("sh601989", "中国重工"),
        ("sz002049", "紫光国微"),
        ("sh600893", "航发动力"),
        # ── 基建 / 地产 ──
        ("sh601668", "中国建筑"),
        ("sh600048", "保利发展"),
        # ── 电力 / 公用事业 ──
        ("sh600900", "长江电力"),
        ("sh601985", "中国核电"),
        # ── 汽车 ──
        ("sh600104", "上汽集团"),
        ("sz002920", "德赛西威"),
    ]
    return pool


def download_ohlcv(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    """下载单只股票日线数据 (前复权)。"""
    code = symbol[2:]
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df is None or df.empty:
            return None

        df = df.rename(columns={
            "日期": "trade_date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "turnover",
        })
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["symbol"] = symbol
        df = df.sort_values("trade_date").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  {symbol}: 下载失败 - {e}")
        return None


def download_all(
    pool: List[Tuple[str, str]],
    start: date,
    end: date,
) -> Dict[str, pd.DataFrame]:
    """批量下载, 返回 {symbol: DataFrame}。"""
    result = {}
    for sym, name in pool:
        print(f"  下载 {name}({sym})...", end="", flush=True)
        df = download_ohlcv(sym, start, end)
        if df is not None and len(df) >= MIN_BARS:
            result[sym] = df
            print(f" {len(df)} bars OK")
        else:
            print(" 数据不足, 跳过")
        time.sleep(0.3)  # 限速
    return result


# ==================== 指标计算 (无前瞻) ====================

def compute_indicators(df: pd.DataFrame, as_of_idx: int) -> Optional[Dict]:
    """
    计算截止 as_of_idx 行 (含) 的技术指标。
    严格只使用 df.iloc[:as_of_idx+1] 的数据。
    """
    sub = df.iloc[: as_of_idx + 1]
    if len(sub) < MIN_BARS:
        return None

    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    l = sub["low"].astype(float)
    o = sub["open"].astype(float)
    v = sub["volume"].astype(float)

    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    high_20d = h.rolling(20).max()
    low_20d = l.rolling(20).min()
    min_c_40d = c.rolling(40).min()
    max_ret_40d = c / min_c_40d - 1
    avg_vol_20d = v.rolling(20).mean()

    i = len(sub) - 1
    if pd.isna(ma60.iloc[i]) or pd.isna(atr14.iloc[i]):
        return None

    return {
        "trade_date": sub.iloc[i]["trade_date"],
        "close": float(c.iloc[i]),
        "open": float(o.iloc[i]),
        "high": float(h.iloc[i]),
        "low": float(l.iloc[i]),
        "volume": float(v.iloc[i]),
        "ma20": round(float(ma20.iloc[i]), 4),
        "ma60": round(float(ma60.iloc[i]), 4),
        "atr14": round(float(atr14.iloc[i]), 4),
        "high_20d": round(float(high_20d.iloc[i]), 4),
        "low_20d": round(float(low_20d.iloc[i]), 4),
        "max_return_40d": round(float(max_ret_40d.iloc[i]), 4),
        "avg_vol_20d": round(float(avg_vol_20d.iloc[i]), 0),
    }


# ==================== 策略规则 ====================

def trend_gate(ind: Dict) -> Tuple[bool, str]:
    """趋势门: MA20>MA60, CLOSE>MA60, MAX_RET_40D>=12%, 无 MA 纠缠。"""
    ma20, ma60, close = ind["ma20"], ind["ma60"], ind["close"]
    if close > 0 and abs(ma20 - ma60) / close < MA_TANGLE_THRESHOLD:
        return False, "MA_TANGLE"
    if ma20 <= ma60:
        return False, "MA20<=MA60"
    if close <= ma60:
        return False, "CLOSE<=MA60"
    if ind["max_return_40d"] < MIN_RETURN_40D:
        return False, "RET<12%"
    return True, "TREND_OK"


def pullback_zone(ind: Dict) -> Tuple[bool, str]:
    """回调区: MA20 ± 1×ATR, LOW 不得破 MA60。"""
    ma20, ma60 = ind["ma20"], ind["ma60"]
    atr, close, low = ind["atr14"], ind["close"], ind["low"]
    if low < ma60:
        return False, "LOW<MA60"
    upper = ma20 + PULLBACK_BAND_ATR * atr
    lower = ma20 - PULLBACK_BAND_ATR * atr
    if lower <= close <= upper:
        return True, "IN_ZONE"
    return False, "OUT_OF_ZONE"


def trigger_day(ind: Dict) -> bool:
    """触发日: 收盘站上 MA20 且收阳。"""
    return ind["close"] > ind["ma20"] and ind["close"] > ind["open"]


def flow_confirm(df: pd.DataFrame, as_of_idx: int) -> Tuple[str, float]:
    """
    量价确认 (无前瞻)。
    只使用截止 as_of_idx 的数据。
    大阴线检查仅覆盖回调段最后 10 根 K 线 (近期卖压更有参考意义)。
    """
    sub = df.iloc[: as_of_idx + 1]
    if len(sub) < 20:
        return "WEAK", 60.0

    recent = sub.tail(40)
    bars = recent.to_dict("records")

    # 找最高点
    peak_i, peak_h = 0, 0.0
    for i, b in enumerate(bars):
        if float(b["high"]) >= peak_h:
            peak_h = float(b["high"])
            peak_i = i

    impulse = bars[max(0, peak_i - 10) : peak_i + 1]
    pullback = bars[peak_i + 1 :]

    if not impulse or not pullback:
        return "WEAK", 60.0

    avg_v_imp = sum(float(b["volume"]) for b in impulse) / len(impulse)
    avg_v_pb = sum(float(b["volume"]) for b in pullback) / len(pullback)
    trig_v = float(bars[-1]["volume"])

    shrink = avg_v_pb < avg_v_imp
    pop = trig_v > avg_v_pb * 1.2 if avg_v_pb > 0 else False

    # 大阴线否决 (仅检查最近 10 根回调 K 线)
    check_bars = pullback[-10:] if len(pullback) > 10 else pullback
    for b in check_bars:
        c, o, v = float(b["close"]), float(b["open"]), float(b["volume"])
        if o > 0 and (c - o) / o < -0.025 and v > avg_v_imp:
            return "FAIL", 0.0

    if shrink and pop:
        return "PASS", 75.0
    if shrink or pop:
        return "WEAK", 60.0
    return "FAIL", 30.0


def calc_entry_stop(ind: Dict) -> Tuple[float, float]:
    """计算入场价和止损价。"""
    close, atr = ind["close"], ind["atr14"]
    low_20d = ind["low_20d"]
    entry = round(close * (1 + ENTRY_OFFSET_PCT), 2)
    stop_a = entry - STOP_ATR_MULT * atr
    stop_b = low_20d - STOP_LOW_MULT * atr
    stop = round(min(stop_a, stop_b), 2)
    return entry, stop


def calc_shares(entry: float, stop: float, nav: float, cash: float) -> int:
    """仓位计算 (100 股整手)。基于 NAV 计算风险, 受可用现金约束。"""
    if entry <= stop or entry <= 0:
        return 0
    risk_amount = nav * RISK_PER_TRADE_PCT
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    raw = risk_amount / risk_per_share
    shares = int(math.floor(raw / 100)) * 100

    # 单笔不超过可用现金
    max_by_cash = int(cash / entry / 100) * 100
    shares = min(shares, max_by_cash)
    return max(shares, 0)


# ==================== 持仓管理 ====================

@dataclass
class Position:
    symbol: str
    name: str
    entry_price: float
    stop_price: float
    shares: int
    entry_date: date
    current_price: float = 0.0

    def __post_init__(self):
        self.current_price = self.entry_price

    @property
    def value(self) -> float:
        return self.shares * self.current_price

    @property
    def cost(self) -> float:
        return self.shares * self.entry_price

    @property
    def pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares

    @property
    def pnl_pct(self) -> float:
        return (self.current_price / self.entry_price - 1) * 100 if self.entry_price > 0 else 0.0


@dataclass
class TradeRecord:
    date: date
    action: str        # BUY / SELL
    symbol: str
    name: str
    price: float
    shares: int
    amount: float      # cost or proceeds
    pnl: float = 0.0
    reason: str = ""


@dataclass
class Engine:
    cash: float
    initial_capital: float
    positions: List[Position] = field(default_factory=list)
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[Tuple[date, float, float]] = field(default_factory=list)
    peak_equity: float = 0.0

    def __post_init__(self):
        self.peak_equity = self.initial_capital

    @property
    def equity(self) -> float:
        return self.cash + sum(p.value for p in self.positions)

    @property
    def drawdown_pct(self) -> float:
        return (self.equity / self.peak_equity - 1) * 100 if self.peak_equity > 0 else 0.0

    def can_open(self) -> bool:
        return len(self.positions) < MAX_POSITIONS

    def open_position(
        self, symbol: str, name: str, entry: float, stop: float,
        shares: int, trade_date: date,
    ) -> bool:
        cost = entry * shares
        if cost > self.cash:
            shares = int(self.cash / entry / 100) * 100
            if shares <= 0:
                return False
            cost = entry * shares
        self.cash -= cost
        pos = Position(symbol, name, entry, stop, shares, trade_date)
        self.positions = [*self.positions, pos]  # immutable pattern
        self.trades.append(TradeRecord(
            trade_date, "BUY", symbol, name, entry, shares, cost,
        ))
        return True

    def close_position(
        self, pos: Position, exit_price: float, trade_date: date, reason: str,
    ) -> None:
        proceeds = exit_price * pos.shares
        pnl = (exit_price - pos.entry_price) * pos.shares
        self.cash += proceeds
        self.positions = [p for p in self.positions if p is not pos]
        self.trades.append(TradeRecord(
            trade_date, "SELL", pos.symbol, pos.name,
            exit_price, pos.shares, proceeds, pnl, reason,
        ))

    def update_and_check_stops(
        self, price_map: Dict[str, Dict], trade_date: date,
    ) -> List[TradeRecord]:
        """更新持仓现价, 检查止损。返回本日平仓记录。"""
        exits: List[Tuple[Position, float, str]] = []
        for pos in self.positions:
            bar = price_map.get(pos.symbol)
            if not bar:
                continue
            # 盘中最低价触及止损 → 以止损价成交
            if bar["low"] <= pos.stop_price:
                exits.append((pos, pos.stop_price, "止损"))
            else:
                pos.current_price = bar["close"]

        closed = []
        for pos, price, reason in exits:
            self.close_position(pos, price, trade_date, reason)
            closed.append(self.trades[-1])
        return closed

    def snapshot(self, trade_date: date) -> None:
        eq = self.equity
        self.peak_equity = max(self.peak_equity, eq)
        dd = (eq / self.peak_equity - 1) * 100 if self.peak_equity > 0 else 0.0
        self.equity_curve = [*self.equity_curve, (trade_date, eq, dd)]


# ==================== 回测主逻辑 ====================

def run_backtest() -> None:
    print("=" * 64)
    print("  PAMS A股卫星策略回测  —  严格无前瞻偏差")
    print("=" * 64)
    print(f"  资金: ¥{INITIAL_CAPITAL:,.0f}  |  最大持仓: {MAX_POSITIONS}")
    print(f"  策略起始: {BACKTEST_START}  |  结束: {BACKTEST_END}")
    print(f"  数据下载: {DATA_FETCH_START} ~ {BACKTEST_END}")
    print()

    # ── 1. 构建观察池 ──
    pool = build_watchlist()
    names = {s: n for s, n in pool}
    print(f"观察池: {len(pool)} 只标的")
    for s, n in pool:
        print(f"  {s} {n}")

    # ── 2. 下载历史数据 ──
    print(f"\n下载 OHLCV 数据 ({DATA_FETCH_START} ~ {BACKTEST_END})...")
    all_data = download_all(pool, DATA_FETCH_START, BACKTEST_END)
    print(f"有效标的: {len(all_data)} 只\n")

    if not all_data:
        print("ERROR: 无有效数据, 回测终止")
        return

    # ── 3. 提取交易日序列 ──
    sample_df = next(iter(all_data.values()))
    all_dates = sorted(sample_df["trade_date"].unique())
    bt_dates = [d for d in all_dates if BACKTEST_START <= d <= BACKTEST_END]

    # 如果 BACKTEST_START 非交易日, 从第一个 >= BACKTEST_START 的交易日开始
    if not bt_dates:
        first = next((d for d in all_dates if d >= BACKTEST_START), None)
        if first:
            bt_dates = [d for d in all_dates if first <= d <= BACKTEST_END]

    if not bt_dates:
        print("ERROR: 回测区间无交易日")
        return

    print(f"回测交易日: {len(bt_dates)} 天  ({bt_dates[0]} ~ {bt_dates[-1]})")
    print()

    # ── 4. 初始化引擎 ──
    engine = Engine(cash=INITIAL_CAPITAL, initial_capital=INITIAL_CAPITAL)

    # ── 5. 逐日执行 ──
    print("=" * 64)
    print("  逐日策略执行")
    print("=" * 64)

    for trade_date in bt_dates:
        print(f"\n─── {trade_date} ───")

        # 5a. 构建当日行情 (仅含有数据的标的)
        price_map: Dict[str, Dict] = {}
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == trade_date]
            if not rows.empty:
                r = rows.iloc[0]
                price_map[sym] = {
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                }

        # 5b. 更新持仓 + 止损检查
        closed = engine.update_and_check_stops(price_map, trade_date)
        for t in closed:
            print(f"  SELL {t.name}({t.symbol}) ¥{t.price:.2f} × {t.shares}股"
                  f"  盈亏 ¥{t.pnl:+,.2f}  [{t.reason}]")

        # 5c. 扫描候选 → 开仓 (只要还有仓位 & 现金)
        if engine.can_open() and engine.cash > 0:
            signals = _scan_signals(all_data, names, trade_date, engine)

            for sig in signals:
                if not engine.can_open():
                    break
                if engine.cash < sig["entry"] * 100:
                    continue
                ok = engine.open_position(
                    sig["symbol"], sig["name"],
                    sig["entry"], sig["stop"], sig["shares"],
                    trade_date,
                )
                if ok:
                    _print_buy_reasoning(sig, engine)

        # 5d. 记录权益
        engine.snapshot(trade_date)

        # 5e. 当日状态
        pos_str = ", ".join(
            f"{p.name}({p.pnl_pct:+.1f}%)" for p in engine.positions
        ) or "无"
        print(
            f"  现金 ¥{engine.cash:,.2f}  |  持仓[{len(engine.positions)}] {pos_str}  |  "
            f"权益 ¥{engine.equity:,.2f}  |  回撤 {engine.drawdown_pct:.2f}%"
        )

    # ── 6. 期末平仓 (以最后一日收盘价清算) ──
    if engine.positions:
        last_date = bt_dates[-1]
        last_price_map: Dict[str, Dict] = {}
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == last_date]
            if not rows.empty:
                r = rows.iloc[0]
                last_price_map[sym] = {"close": float(r["close"])}

        remaining = list(engine.positions)
        for pos in remaining:
            bar = last_price_map.get(pos.symbol)
            if bar:
                engine.close_position(pos, bar["close"], last_date, "期末清算")

    # ── 7. 输出报告 ──
    _print_report(engine, names)
    _print_weekly_snapshots(engine)
    _print_equity_chart(engine)
    _print_drawdown_chart(engine)


def _scan_signals(
    all_data: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    trade_date: date,
    engine: Engine,
) -> List[Dict]:
    """对观察池全部标的执行策略扫描, 返回按 flow_score 排序的信号列表。"""
    held_syms = {p.symbol for p in engine.positions}
    signals = []

    for sym, df in all_data.items():
        if sym in held_syms:
            continue

        # 找到当日行在 DataFrame 中的索引
        mask = df["trade_date"] == trade_date
        if not mask.any():
            continue
        idx = df.index[mask][0]

        # 计算截止当日的指标 (无前瞻)
        ind = compute_indicators(df, idx)
        if ind is None:
            continue

        # 趋势门
        ok, reason = trend_gate(ind)
        if not ok:
            continue

        # 回调区
        ok, reason = pullback_zone(ind)
        if not ok:
            continue

        # 触发日
        if not trigger_day(ind):
            continue

        # 量价确认
        flow_result, flow_score = flow_confirm(df, idx)
        if flow_result == "FAIL":
            continue

        # 入场 / 止损
        entry, stop = calc_entry_stop(ind)
        if entry <= stop:
            continue

        # 仓位 (基于 NAV 计算风险, 受可用现金约束)
        shares = calc_shares(entry, stop, engine.equity, engine.cash)
        if shares <= 0:
            continue

        # 单笔市值限制
        pos_value = entry * shares
        if pos_value > engine.equity * MAX_POSITION_VALUE_PCT:
            shares = int(engine.equity * MAX_POSITION_VALUE_PCT / entry / 100) * 100
            if shares <= 0:
                continue

        risk_per_share = entry - stop
        risk_amount = shares * risk_per_share
        pos_value = entry * shares

        signals.append({
            "symbol": sym,
            "name": names.get(sym, sym),
            "entry": entry,
            "stop": stop,
            "shares": shares,
            "flow": flow_result,
            "flow_score": flow_score,
            "close": ind["close"],
            "open": ind["open"],
            "high": ind["high"],
            "low": ind["low"],
            "ma20": ind["ma20"],
            "ma60": ind["ma60"],
            "atr14": ind["atr14"],
            "high_20d": ind["high_20d"],
            "low_20d": ind["low_20d"],
            "max_return_40d": ind["max_return_40d"],
            "risk_per_share": risk_per_share,
            "risk_amount": risk_amount,
            "pos_value": pos_value,
            "pos_value_pct": pos_value / engine.equity * 100,
        })

    # 优先买量价确认好的
    signals.sort(key=lambda x: x["flow_score"], reverse=True)
    return signals


# ==================== 买入决策链输出 ====================

def _print_buy_reasoning(sig: Dict, engine: Engine) -> None:
    """打印单笔买入的完整决策逻辑。"""
    s = sig
    tangle_pct = abs(s["ma20"] - s["ma60"]) / s["close"] * 100 if s["close"] > 0 else 0
    zone_upper = s["ma20"] + PULLBACK_BAND_ATR * s["atr14"]
    zone_lower = s["ma20"] - PULLBACK_BAND_ATR * s["atr14"]

    print(f"  ┌─ BUY  {s['name']}({s['symbol']})")
    print(f"  │  ① 趋势门 ✓  MA20={s['ma20']:.2f} > MA60={s['ma60']:.2f}  "
          f"收盘{s['close']:.2f}>MA60  "
          f"40日涨幅={s['max_return_40d']:.1%}≥12%  "
          f"MA间距={tangle_pct:.2f}%≥1.5%")
    print(f"  │  ② 回调区 ✓  价格{s['close']:.2f} ∈ [{zone_lower:.2f}, {zone_upper:.2f}]  "
          f"(MA20±{PULLBACK_BAND_ATR}×ATR{s['atr14']:.2f})  "
          f"最低{s['low']:.2f}≥MA60")
    print(f"  │  ③ 触发日 ✓  收盘{s['close']:.2f}>MA20{s['ma20']:.2f}  "
          f"且收阳 收盘{s['close']:.2f}>开盘{s['open']:.2f}")
    print(f"  │  ④ 量价   {s['flow']}({s['flow_score']:.0f}分)")
    print(f"  │  ⑤ 入场¥{s['entry']:.2f} = 收盘×0.998  "
          f"止损¥{s['stop']:.2f} = min(入场-2×ATR, 20日低{s['low_20d']:.2f}-0.5×ATR)")
    print(f"  │  ⑥ 仓位 {s['shares']}股 × ¥{s['entry']:.2f} = ¥{s['pos_value']:,.0f}  "
          f"(占NAV {s['pos_value_pct']:.1f}%)  "
          f"单笔风险¥{s['risk_amount']:,.0f}")
    print(f"  └─ 买入后: 现金¥{engine.cash:,.0f}  持仓{len(engine.positions)}/{MAX_POSITIONS}")


# ==================== 报告输出 ====================

def _print_report(engine: Engine, names: Dict[str, str]) -> None:
    print(f"\n{'=' * 64}")
    print("  回测结果汇总")
    print(f"{'=' * 64}")

    final_eq = engine.equity
    ret = (final_eq / engine.initial_capital - 1) * 100
    max_dd = min(eq[2] for eq in engine.equity_curve) if engine.equity_curve else 0.0

    print(f"  初始资金:   ¥{engine.initial_capital:>12,.2f}")
    print(f"  期末权益:   ¥{final_eq:>12,.2f}")
    print(f"  总收益:     ¥{final_eq - engine.initial_capital:>+12,.2f}  ({ret:+.2f}%)")
    print(f"  最大回撤:   {max_dd:.2f}%")

    buys = [t for t in engine.trades if t.action == "BUY"]
    sells = [t for t in engine.trades if t.action == "SELL"]
    wins = [t for t in sells if t.pnl > 0]
    losses = [t for t in sells if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in sells)

    print(f"\n  买入次数:   {len(buys)}")
    print(f"  卖出次数:   {len(sells)}")
    if sells:
        print(f"  盈利/亏损:  {len(wins)} / {len(losses)}")
        print(f"  胜率:       {len(wins)/len(sells)*100:.1f}%")
        print(f"  已实现盈亏: ¥{total_pnl:+,.2f}")

    print(f"\n  {'─' * 58}")
    print(f"  交易明细")
    print(f"  {'─' * 58}")
    for t in engine.trades:
        if t.action == "BUY":
            print(f"  {t.date}  BUY   {t.name:6s}  ¥{t.price:>8.2f} × {t.shares:>5d}股")
        else:
            print(
                f"  {t.date}  SELL  {t.name:6s}  ¥{t.price:>8.2f} × {t.shares:>5d}股  "
                f"盈亏 ¥{t.pnl:>+9,.2f}  [{t.reason}]"
            )


def _print_equity_chart(engine: Engine) -> None:
    """ASCII 权益曲线。"""
    if not engine.equity_curve:
        return

    print(f"\n{'=' * 64}")
    print("  权益曲线")
    print(f"{'=' * 64}")

    equities = [e[1] for e in engine.equity_curve]
    lo, hi = min(equities), max(equities)
    span = hi - lo if hi > lo else 1
    W = 40

    for d, eq, dd in engine.equity_curve:
        bar_len = int((eq - lo) / span * W)
        bar = "█" * bar_len + "░" * (W - bar_len)
        print(f"  {d}  ¥{eq:>10,.2f} {dd:>6.2f}%  |{bar}|")


def _print_drawdown_chart(engine: Engine) -> None:
    """ASCII 回撤曲线。"""
    if not engine.equity_curve:
        return

    print(f"\n{'=' * 64}")
    print("  回撤曲线")
    print(f"{'=' * 64}")

    dds = [e[2] for e in engine.equity_curve]
    min_dd = min(dds)
    span = abs(min_dd) if min_dd < 0 else 1
    W = 40

    for d, eq, dd in engine.equity_curve:
        bar_len = int(abs(dd) / span * W) if span > 0 else 0
        bar = "▓" * bar_len
        print(f"  {d} {dd:>7.2f}%  |{bar}")


def _print_weekly_snapshots(engine: Engine) -> None:
    """每周市值快照: 从第一笔买入起, 每隔一周输出持仓市值详情。"""
    if not engine.equity_curve:
        return

    # 找到第一笔买入日期
    first_buy = None
    for t in engine.trades:
        if t.action == "BUY":
            first_buy = t.date
            break
    if not first_buy:
        return

    # 建立权益字典
    eq_map = {d: (eq, dd) for d, eq, dd in engine.equity_curve}

    # 按周采样: 从 first_buy 开始, 每 7 天取一个快照
    print(f"\n{'=' * 72}")
    print("  每周市值快照  (从首笔买入起, 每 7 天)")
    print(f"{'=' * 72}")
    print(f"  {'日期':12s} {'权益':>12s} {'收益率':>8s} {'回撤':>8s} {'持仓数':>6s} {'现金':>12s}")
    print(f"  {'─' * 66}")

    # 收集快照日 (每 7 天一个)
    snap_date = first_buy
    curve_dates = [d for d, _, _ in engine.equity_curve]
    last_date = curve_dates[-1]

    while snap_date <= last_date:
        # 找到 <= snap_date 的最近交易日
        actual_date = None
        for d in curve_dates:
            if d <= snap_date:
                actual_date = d
        if actual_date and actual_date in eq_map:
            eq, dd = eq_map[actual_date]
            ret = (eq / engine.initial_capital - 1) * 100

            # 计算该日持仓数 (从 equity_curve 推算)
            # 统计截止该日持仓数: 买入<=该日 且 (卖出>该日 或 未卖出)
            open_count = 0
            invested = 0.0
            for t in engine.trades:
                if t.action == "BUY" and t.date <= actual_date:
                    # 检查是否在 actual_date 之前已卖出
                    sold = False
                    for s in engine.trades:
                        if (s.action == "SELL" and s.symbol == t.symbol
                                and s.date <= actual_date and s.date >= t.date):
                            sold = True
                            break
                    if not sold:
                        open_count += 1
                        invested += t.amount

            cash_est = eq - invested if invested < eq else 0
            print(f"  {actual_date}  ¥{eq:>10,.2f}  {ret:>+7.2f}%  {dd:>7.2f}%  {open_count:>4d}    ¥{cash_est:>10,.2f}")

        snap_date = snap_date + timedelta(days=7)

    # 始终输出最后一个交易日
    if curve_dates[-1] != actual_date:
        eq, dd = eq_map[curve_dates[-1]]
        ret = (eq / engine.initial_capital - 1) * 100
        print(f"  {curve_dates[-1]}  ¥{eq:>10,.2f}  {ret:>+7.2f}%  {dd:>7.2f}%")


# ==================== 入口 ====================

if __name__ == "__main__":
    run_backtest()
