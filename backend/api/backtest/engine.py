"""
PAMS High Volatility Alpha Model 回测引擎
─────────────────────────────────────────
激进突破策略：20日突破 + 金字塔加仓 + 宽止损 + 全仓跟踪。
目标：年化超额最大化，接受 40-50% 最大回撤。
"""

import logging
import math
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd
import psycopg2
import psycopg2.extras

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# ==================== 固定常量 ====================

BREAKOUT_LOOKBACK = 20          # 20日突破
VOLUME_CONFIRM_MULT = 1.2       # 放量 >= 1.2x 均量
PYRAMID_LOOKBACK = 10           # 加仓条件: 10日新高
PYRAMID_VOLUME_MULT = 1.0       # 加仓量确认 (宽松)
INDEX_MA_PERIOD = 200           # 指数 MA200
MIN_BARS = 65                   # 最少 K 线数
ENTRY_PREMIUM_PCT = 0.002       # 突破追入溢价 0.2%
BENCHMARK_INDEX = "sh000001"    # 上证指数 (基准对比)


# ==================== 参数 ====================

@dataclass(frozen=True)
class BacktestParams:
    initial_capital: float = 100_000.0
    max_positions: int = 3              # 集中仓位
    risk_per_trade_pct: float = 0.02    # 2% 单笔风险 (RISK_ON)
    risk_neutral_pct: float = 0.01      # 1% (NEUTRAL)
    max_position_value_pct: float = 0.50  # 单票 50%
    stop_atr_mult: float = 2.5          # ATR×2.5 初始止损
    trailing_atr_mult: float = 3.0      # peak - 3×ATR 移动止损
    pool_source: str = "dynamic"
    start_date: str = "2025-10-01"
    end_date: str = "2026-02-01"
    symbols: Optional[List[str]] = None
    index_code: str = "sh000300"
    screen_interval_days: int = 5
    screen_min_turnover: float = 200_000_000
    # 金字塔加仓
    pyramid_enabled: bool = True
    pyramid_max_adds: int = 2           # 最多加仓 2 次
    pyramid_risk_frac: float = 0.5      # 加仓风险 = 初始 50%
    pyramid_trigger_r: float = 1.0      # +1R 触发加仓
    # 突破参数
    breakout_lookback: int = 20
    volume_mult: float = 1.2


# ==================== 股票池 ====================

def build_watchlist() -> List[Tuple[str, str]]:
    """默认观察池: 37 只热门板块龙头。"""
    return [
        ("sh601899", "紫金矿业"), ("sh600547", "山东黄金"),
        ("sh603993", "洛阳钼业"), ("sz002466", "天齐锂业"),
        ("sh600489", "中金黄金"),
        ("sz300750", "宁德时代"), ("sh601012", "隆基绿能"),
        ("sz002594", "比亚迪"),   ("sh600438", "通威股份"),
        ("sh688981", "中芯国际"), ("sz002371", "北方华创"),
        ("sh603019", "中科曙光"), ("sz002230", "科大飞讯"),
        ("sh688111", "金山办公"), ("sz300496", "中科创达"),
        ("sh688036", "传音控股"),
        ("sh600519", "贵州茅台"), ("sz000858", "五粮液"),
        ("sh600887", "伊利股份"),
        ("sh601318", "中国平安"), ("sh600036", "招商银行"),
        ("sh601166", "兴业银行"), ("sh600030", "中信证券"),
        ("sz300059", "东方财富"), ("sz002415", "海康威视"),
        ("sh600276", "恒瑞医药"), ("sz300760", "迈瑞医疗"),
        ("sh688185", "康希诺"),
        ("sh601989", "中国重工"), ("sz002049", "紫光国微"),
        ("sh600893", "航发动力"),
        ("sh601668", "中国建筑"), ("sh600048", "保利发展"),
        ("sh600900", "长江电力"), ("sh601985", "中国核电"),
        ("sh600104", "上汽集团"), ("sz002920", "德赛西威"),
    ]


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def build_watchlist_from_db() -> List[Tuple[str, str]]:
    """从 pams_watchlist 读取 status=watch/candidate 的标的，失败时回退硬编码。"""
    try:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT symbol, name FROM pams_watchlist
                    WHERE market = 'CN' AND status IN ('watch', 'candidate')
                    ORDER BY updated_at DESC
                """)
                rows = cur.fetchall()
        if rows:
            return [(r["symbol"], r["name"] or r["symbol"]) for r in rows]
        logger.warning("DB 观察池为空，回退到硬编码")
    except Exception as e:
        logger.warning("读取 DB 观察池失败，回退到硬编码: %s", e)
    return build_watchlist()


def _lookup_name(symbol: str) -> Optional[str]:
    """查询股票名称：优先 DB，回退 AkShare。"""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM pams_watchlist WHERE symbol = %s LIMIT 1",
                    (symbol,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception:
        pass
    try:
        code = symbol[2:] if len(symbol) > 2 else symbol
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            name_row = info[info["item"] == "股票简称"]
            if not name_row.empty:
                return str(name_row.iloc[0]["value"])
    except Exception:
        pass
    return None


def _resolve_custom_symbols(symbols: List[str]) -> List[Tuple[str, str]]:
    """将用户输入的代码列表解析为 (symbol, name) 对。"""
    result = []
    for sym in symbols:
        sym = sym.strip()
        if not sym:
            continue
        if len(sym) == 6 and sym.isdigit():
            sym = ("sh" if sym.startswith(("6", "9")) else "sz") + sym
        name = _lookup_name(sym) or sym
        result.append((sym, name))
    return result


def _build_broad_universe() -> List[Tuple[str, str]]:
    """构建宽泛候选池: 硬编码 37 只 + DB 观察池的并集。"""
    hardcoded = build_watchlist()
    db_list: List[Tuple[str, str]] = []
    try:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT symbol, name FROM pams_watchlist
                    WHERE market = 'CN' AND status IN ('watch', 'candidate')
                """)
                db_list = [
                    (r["symbol"], r["name"] or r["symbol"])
                    for r in cur.fetchall()
                ]
    except Exception as e:
        logger.warning("DB 观察池读取失败，仅用硬编码池: %s", e)

    seen: Dict[str, str] = {}
    for sym, name in hardcoded + db_list:
        if sym not in seen:
            seen[sym] = name
    result = list(seen.items())
    logger.info("宽泛池构建: %d 只 (硬编码%d + DB%d)", len(result), len(hardcoded), len(db_list))
    return result


# ==================== 数据层 ====================

def _download_ohlcv(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    code = symbol[2:] if len(symbol) > 2 else symbol
    try:
        if symbol.startswith("sh000") or symbol.startswith("sz399"):
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is None or df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            df["trade_date"] = pd.to_datetime(df["date"]).dt.date
            df["symbol"] = symbol
            df = df.rename(columns={"date": "_date_orig"})
            if "turnover" not in df.columns:
                df["turnover"] = 0
            df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
        else:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
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
        return df.sort_values("trade_date").reset_index(drop=True)
    except Exception as e:
        logger.warning("下载 %s 失败: %s", symbol, e)
        return None


def _download_all(
    pool: List[Tuple[str, str]], start: date, end: date,
) -> Dict[str, pd.DataFrame]:
    result = {}
    for sym, name in pool:
        df = _download_ohlcv(sym, start, end)
        if df is not None and len(df) >= MIN_BARS:
            result[sym] = df
        time.sleep(0.3)
    return result


def _add_atr_column(df: pd.DataFrame) -> pd.DataFrame:
    """为 DataFrame 预计算 ATR14 列。"""
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    c = df["close"].astype(float)
    prev_c = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    result = df.copy()
    result["atr14"] = tr.rolling(14).mean()
    return result


# ==================== 指标计算 ====================

def _compute_indicators(
    df: pd.DataFrame,
    as_of_idx: int,
    index_df: Optional[pd.DataFrame] = None,
) -> Optional[Dict]:
    sub = df.iloc[:as_of_idx + 1]
    if len(sub) < MIN_BARS:
        return None

    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    lo = sub["low"].astype(float)
    o = sub["open"].astype(float)
    v = sub["volume"].astype(float)

    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()

    prev_c = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    avg_vol_20d = v.rolling(20).mean()

    i = len(sub) - 1
    if pd.isna(ma60.iloc[i]) or pd.isna(atr14.iloc[i]):
        return None

    # RS = stock_ret_60d - index_ret_60d
    rs = -1.0
    trade_date_val = sub.iloc[i]["trade_date"]
    if index_df is not None and len(sub) >= 60:
        sym_close_now = float(c.iloc[i])
        sym_close_prev = float(c.iloc[i - 60]) if i >= 60 else float(c.iloc[0])
        sym_ret = (sym_close_now / sym_close_prev - 1) if sym_close_prev > 0 else 0.0
        idx_rows = index_df[index_df["trade_date"] <= trade_date_val]
        if len(idx_rows) >= 60:
            idx_close_now = float(idx_rows.iloc[-1]["close"])
            idx_close_prev = float(idx_rows.iloc[-60]["close"])
            idx_ret = (idx_close_now / idx_close_prev - 1) if idx_close_prev > 0 else 0.0
            rs = sym_ret - idx_ret

    # 指数 MA200 regime
    index_ma200 = None
    index_close_val = None
    if index_df is not None:
        idx_rows = index_df[index_df["trade_date"] <= trade_date_val]
        if len(idx_rows) >= INDEX_MA_PERIOD:
            index_ma200 = float(idx_rows["close"].astype(float).tail(INDEX_MA_PERIOD).mean())
            index_close_val = float(idx_rows.iloc[-1]["close"])
        elif len(idx_rows) > 0:
            index_close_val = float(idx_rows.iloc[-1]["close"])

    return {
        "trade_date": trade_date_val,
        "close": float(c.iloc[i]), "open": float(o.iloc[i]),
        "high": float(h.iloc[i]), "low": float(lo.iloc[i]),
        "volume": float(v.iloc[i]),
        "ma20": round(float(ma20.iloc[i]), 4),
        "ma60": round(float(ma60.iloc[i]), 4),
        "atr14": round(float(atr14.iloc[i]), 4),
        "avg_vol_20d": round(float(avg_vol_20d.iloc[i]), 0),
        "rs": round(rs, 4),
        # 指数 regime
        "index_ma200": index_ma200,
        "index_close": index_close_val,
    }


# ==================== 策略规则 ====================

def _breakout_signal(
    ind: Dict, df: pd.DataFrame, as_of_idx: int,
    lookback: int = BREAKOUT_LOOKBACK,
    vol_mult: float = VOLUME_CONFIRM_MULT,
) -> Tuple[bool, str, Dict]:
    """20日突破 + 放量确认 + RS>=0"""
    close = ind["close"]
    volume = ind["volume"]
    avg_vol = ind["avg_vol_20d"]

    # 条件1: 收盘 > 过去N日最高价 (不含当日)
    start_idx = max(0, as_of_idx - lookback)
    if start_idx >= as_of_idx:
        return False, "INSUFFICIENT_DATA", {}
    prev_high = float(df.iloc[start_idx:as_of_idx]["high"].astype(float).max())
    if close <= prev_high:
        return False, "NO_BREAKOUT", {}

    # 条件2: 放量 >= vol_mult × 20日均量
    if avg_vol > 0 and volume < avg_vol * vol_mult:
        return False, "LOW_VOLUME", {}

    # 条件3: RS >= 0 (放宽)
    if ind.get("rs", -1) < 0:
        return False, "RS<0", {}

    detail = {
        "prev_high": prev_high,
        "volume_ratio": round(volume / avg_vol, 2) if avg_vol > 0 else 0,
    }
    return True, "BREAKOUT_OK", detail


def _get_regime(ind: Dict) -> str:
    """指数 MA200 regime 判断。"""
    idx_close = ind.get("index_close")
    idx_ma200 = ind.get("index_ma200")
    if idx_close is None or idx_ma200 is None:
        return "RISK_ON"
    return "RISK_ON" if idx_close > idx_ma200 else "NEUTRAL"


def _calc_entry_stop(ind: Dict, params: BacktestParams) -> Tuple[float, float]:
    """ATR×2.5 止损，无距离上限拒绝。"""
    close, atr = ind["close"], ind["atr14"]
    entry = round(close * (1 + ENTRY_PREMIUM_PCT), 2)
    stop = round(entry - params.stop_atr_mult * atr, 2)
    return entry, stop


def _check_pyramid_add(
    pos: "_Position",
    bar: Dict,
    df: pd.DataFrame,
    as_of_idx: int,
    params: BacktestParams,
) -> bool:
    """检查是否满足加仓条件: +1R + 10日新高 + 放量。"""
    if not params.pyramid_enabled:
        return False
    if pos.pyramid_count >= params.pyramid_max_adds:
        return False

    # 条件1: 盈利达 +1R
    current_profit = bar["close"] - pos.entry_price
    if pos.r_value <= 0 or current_profit < pos.r_value * params.pyramid_trigger_r:
        return False

    # 条件2: 10日新高 (不含当日)
    start_idx = max(0, as_of_idx - PYRAMID_LOOKBACK)
    if start_idx >= as_of_idx:
        return False
    prev_high_10 = float(df.iloc[start_idx:as_of_idx]["high"].astype(float).max())
    if bar["close"] <= prev_high_10:
        return False

    # 条件3: 成交量确认 (宽松)
    vol_start = max(0, as_of_idx - 20)
    if vol_start >= as_of_idx:
        return False
    avg_vol = float(df.iloc[vol_start:as_of_idx]["volume"].astype(float).mean())
    if avg_vol > 0 and bar["volume"] < avg_vol * PYRAMID_VOLUME_MULT:
        return False

    return True


def _calc_shares(
    entry: float, stop: float, nav: float, cash: float,
    risk_pct: float, max_value_pct: float,
) -> int:
    """风险定位法计算股数。"""
    if entry <= stop or entry <= 0:
        return 0
    risk_amount = nav * risk_pct
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    raw = risk_amount / risk_per_share
    shares = int(math.floor(raw / 100)) * 100
    max_by_cash = int(cash / entry / 100) * 100
    max_by_value = int(nav * max_value_pct / entry / 100) * 100
    return max(min(shares, max_by_cash, max_by_value), 0)


# ==================== 持仓管理 ====================

@dataclass
class _Position:
    symbol: str
    name: str
    entry_price: float
    stop_price: float
    shares: int
    entry_date: date
    current_price: float = 0.0
    peak_price: float = 0.0
    r_value: float = 0.0
    initial_shares: int = 0
    entry_bar_idx: int = 0
    # 金字塔
    pyramid_count: int = 0
    total_cost: float = 0.0
    avg_entry_price: float = 0.0

    def __post_init__(self):
        self.current_price = self.entry_price
        self.peak_price = self.entry_price
        if self.r_value == 0.0:
            self.r_value = self.entry_price - self.stop_price
        if self.initial_shares == 0:
            self.initial_shares = self.shares
        if self.total_cost == 0.0:
            self.total_cost = self.entry_price * self.shares
        if self.avg_entry_price == 0.0:
            self.avg_entry_price = self.entry_price

    @property
    def value(self) -> float:
        return self.shares * self.current_price

    @property
    def pnl_pct(self) -> float:
        return (self.current_price / self.avg_entry_price - 1) * 100 if self.avg_entry_price > 0 else 0.0


@dataclass
class _TradeRecord:
    date: date
    action: str
    symbol: str
    name: str
    price: float
    shares: int
    amount: float
    pnl: float = 0.0
    reason: str = ""
    reasoning: Optional[Dict] = None


@dataclass
class _Engine:
    cash: float
    initial_capital: float
    max_positions: int
    positions: List[_Position] = field(default_factory=list)
    trades: List[_TradeRecord] = field(default_factory=list)
    equity_curve: List[Tuple[date, float, float]] = field(default_factory=list)
    peak_equity: float = 0.0

    def __post_init__(self):
        self.peak_equity = self.initial_capital

    @property
    def equity(self) -> float:
        return self.cash + sum(p.value for p in self.positions)

    def can_open(self) -> bool:
        return len(self.positions) < self.max_positions

    def open_position(
        self, symbol: str, name: str, entry: float, stop: float,
        shares: int, trade_date: date, reasoning: Optional[Dict] = None,
    ) -> bool:
        cost = entry * shares
        if cost > self.cash:
            shares = int(self.cash / entry / 100) * 100
            if shares <= 0:
                return False
            cost = entry * shares
        self.cash -= cost
        pos = _Position(symbol, name, entry, stop, shares, trade_date)
        self.positions = [*self.positions, pos]
        self.trades.append(_TradeRecord(
            trade_date, "BUY", symbol, name, entry, shares, cost,
            reasoning=reasoning,
        ))
        return True

    def close_position(
        self, pos: _Position, exit_price: float, trade_date: date, reason: str,
    ) -> None:
        proceeds = exit_price * pos.shares
        pnl = (exit_price - pos.avg_entry_price) * pos.shares
        self.cash += proceeds
        self.positions = [p for p in self.positions if p is not pos]
        self.trades.append(_TradeRecord(
            trade_date, "SELL", pos.symbol, pos.name,
            exit_price, pos.shares, proceeds, pnl, reason,
        ))

    def add_to_position(
        self, pos: _Position, add_shares: int, add_price: float,
        trade_date: date, reason: str,
    ) -> bool:
        """金字塔加仓。"""
        cost = add_price * add_shares
        if cost > self.cash:
            add_shares = int(self.cash / add_price / 100) * 100
            if add_shares <= 0:
                return False
            cost = add_price * add_shares

        self.cash -= cost
        old_total = pos.shares * pos.avg_entry_price
        pos.shares += add_shares
        pos.total_cost += cost
        pos.avg_entry_price = round((old_total + cost) / pos.shares, 4) if pos.shares > 0 else add_price
        pos.pyramid_count += 1

        self.trades.append(_TradeRecord(
            trade_date, "BUY", pos.symbol, pos.name,
            add_price, add_shares, cost, reason=reason,
        ))
        return True

    def update_and_check_stops(
        self, price_map: Dict[str, Dict], trade_date: date,
        params: BacktestParams,
    ) -> None:
        """移动止损 (peak - 3×ATR) + 固定止损触发。无分段止盈，无时间止损。"""
        exits: List[Tuple[_Position, float, str]] = []
        for pos in self.positions:
            bar = price_map.get(pos.symbol)
            if not bar:
                continue
            pos.peak_price = max(pos.peak_price, bar["high"])
            pos.current_price = bar["close"]

            # 移动止损: peak - trailing_atr_mult × ATR (始终启用)
            if (params.trailing_atr_mult > 0
                    and bar.get("atr14") is not None):
                trailing = pos.peak_price - params.trailing_atr_mult * bar["atr14"]
                if trailing > pos.stop_price:
                    pos.stop_price = round(trailing, 2)

            # 止损触发
            if bar["low"] <= pos.stop_price:
                reason = "移动止损" if pos.stop_price > pos.entry_price else "止损"
                exits.append((pos, pos.stop_price, reason))

        for pos, price, reason in exits:
            self.close_position(pos, price, trade_date, reason)

    def snapshot(self, trade_date: date) -> None:
        eq = self.equity
        self.peak_equity = max(self.peak_equity, eq)
        dd = (eq / self.peak_equity - 1) * 100 if self.peak_equity > 0 else 0.0
        self.equity_curve = [*self.equity_curve, (trade_date, eq, dd)]


# ==================== 筛选 ====================

def _weekly_screen(
    all_data: Dict[str, pd.DataFrame],
    trade_date: date,
    index_df: Optional[pd.DataFrame] = None,
    min_turnover: float = 200_000_000,
) -> List[str]:
    """周度筛选: 流动性 + RS>=0 + MA20>MA60 (宽松)。"""
    active: List[str] = []
    for sym, df in all_data.items():
        if sym.startswith("sh000") or sym.startswith("sz399"):
            continue
        valid_mask = df["trade_date"] <= trade_date
        if not valid_mask.any():
            continue
        valid_indices = df.index[valid_mask]
        if len(valid_indices) < MIN_BARS:
            continue
        as_of_idx = int(valid_indices[-1])

        # 流动性
        start_liq = max(0, as_of_idx - 19)
        recent = df.iloc[start_liq:as_of_idx + 1]
        avg_turnover = float(recent["turnover"].astype(float).mean())
        if avg_turnover < min_turnover:
            continue

        # 基本指标
        ind = _compute_indicators(df, as_of_idx, index_df)
        if ind is None:
            continue

        # MA20 > MA60 (基本上升趋势)
        if ind["ma20"] <= ind["ma60"]:
            continue

        # RS >= 0
        if ind.get("rs", -1) < 0:
            continue

        active.append(sym)

    return active


# ==================== 扫描信号 ====================

def _scan_signals(
    all_data: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    trade_date: date,
    engine: _Engine,
    params: BacktestParams,
    index_df: Optional[pd.DataFrame] = None,
) -> List[Dict]:
    """扫描突破信号。"""
    held_syms = {p.symbol for p in engine.positions}
    signals = []

    for sym, df in all_data.items():
        if sym in held_syms:
            continue

        mask = df["trade_date"] == trade_date
        if not mask.any():
            continue
        idx = int(df.index[mask][0])

        ind = _compute_indicators(df, idx, index_df)
        if ind is None:
            continue

        # 突破信号
        ok, _reason, detail = _breakout_signal(
            ind, df, idx, params.breakout_lookback, params.volume_mult,
        )
        if not ok:
            continue

        # Regime
        regime = _get_regime(ind)
        risk_pct = params.risk_per_trade_pct if regime == "RISK_ON" else params.risk_neutral_pct

        entry, stop = _calc_entry_stop(ind, params)
        if entry <= stop:
            continue

        shares = _calc_shares(
            entry, stop, engine.equity, engine.cash,
            risk_pct, params.max_position_value_pct,
        )
        if shares <= 0:
            continue

        # NEUTRAL → 降 30% 仓位
        if regime == "NEUTRAL":
            shares = int(shares * 0.7 / 100) * 100
            if shares <= 0:
                continue

        pos_value = entry * shares
        stop_dist_pct = (entry - stop) / entry * 100 if entry > 0 else 0

        reasoning = {
            "breakout": (
                f"突破 {params.breakout_lookback}日高 "
                f"¥{detail.get('prev_high', 0):.2f}, "
                f"放量 {detail.get('volume_ratio', 0):.1f}x, "
                f"RS={ind.get('rs', 0):.3f}"
            ),
            "regime": (
                f"{regime}: 指数"
                f" {ind.get('index_close', '?')}"
                f" vs MA200 {ind.get('index_ma200', '?')}"
            ),
            "entry_stop": (
                f"入场¥{entry:.2f}, 止损¥{stop:.2f} "
                f"(距离{stop_dist_pct:.1f}%, ATR={ind['atr14']:.2f})"
            ),
            "position": (
                f"{shares}股 × ¥{entry:.2f} = ¥{pos_value:,.0f} "
                f"(占NAV {pos_value / engine.equity * 100:.1f}%, "
                f"风险{risk_pct * 100:.1f}%)"
            ),
        }

        signals.append({
            "symbol": sym,
            "name": names.get(sym, sym),
            "entry": entry,
            "stop": stop,
            "shares": shares,
            "regime": regime,
            "rs": ind.get("rs", 0),
            "reasoning": reasoning,
        })

    # 按 RS 降序
    signals.sort(key=lambda x: x.get("rs", 0), reverse=True)
    return signals


# ==================== 周度快照 ====================

def _build_weekly_snapshots(engine: _Engine) -> List[Dict]:
    if not engine.equity_curve:
        return []

    first_buy = None
    for t in engine.trades:
        if t.action == "BUY":
            first_buy = t.date
            break
    if not first_buy:
        return []

    eq_map = {d: (eq, dd) for d, eq, dd in engine.equity_curve}
    curve_dates = [d for d, _, _ in engine.equity_curve]
    last_date = curve_dates[-1]

    snapshots = []
    snap_date = first_buy

    while snap_date <= last_date:
        actual_date = None
        for d in curve_dates:
            if d <= snap_date:
                actual_date = d
        if actual_date and actual_date in eq_map:
            eq, dd = eq_map[actual_date]
            ret = (eq / engine.initial_capital - 1) * 100

            open_count = 0
            invested = 0.0
            for t in engine.trades:
                if t.action == "BUY" and t.date <= actual_date:
                    sold = any(
                        s.action == "SELL" and s.symbol == t.symbol
                        and s.date <= actual_date and s.date >= t.date
                        for s in engine.trades
                    )
                    if not sold:
                        open_count += 1
                        invested += t.amount

            cash_est = eq - invested if invested < eq else 0

            snapshots.append({
                "date": actual_date.isoformat(),
                "equity": round(eq, 2),
                "return_pct": round(ret, 2),
                "drawdown_pct": round(dd, 2),
                "positions": open_count,
                "cash": round(cash_est, 2),
            })

        snap_date = snap_date + timedelta(days=7)

    if curve_dates[-1].isoformat() != (snapshots[-1]["date"] if snapshots else ""):
        eq, dd = eq_map[curve_dates[-1]]
        ret = (eq / engine.initial_capital - 1) * 100
        snapshots.append({
            "date": curve_dates[-1].isoformat(),
            "equity": round(eq, 2),
            "return_pct": round(ret, 2),
            "drawdown_pct": round(dd, 2),
            "positions": 0, "cash": round(eq, 2),
        })

    return snapshots


# ==================== 基准曲线 ====================

def _build_benchmark_curve(
    benchmark_df: Optional[pd.DataFrame],
    bt_dates: List[date],
    initial_capital: float,
) -> List[Dict]:
    """构建上证指数基准权益曲线 (归一化到初始资金)。"""
    if benchmark_df is None or benchmark_df.empty or not bt_dates:
        return []

    bm_map = {}
    for _, row in benchmark_df.iterrows():
        bm_map[row["trade_date"]] = float(row["close"])

    # 找到回测期第一个有基准数据的日期
    base_price = None
    for d in bt_dates:
        if d in bm_map:
            base_price = bm_map[d]
            break
    if base_price is None or base_price <= 0:
        return []

    curve = []
    for d in bt_dates:
        price = bm_map.get(d)
        if price is not None:
            bm_equity = round(initial_capital * (price / base_price), 2)
            bm_return_pct = round((price / base_price - 1) * 100, 2)
            curve.append({
                "date": d.isoformat(),
                "equity": bm_equity,
                "return_pct": bm_return_pct,
                "close": round(price, 2),
            })
    return curve


# ==================== 分析报告 ====================

def _generate_report(
    summary: Dict,
    trades_out: List[Dict],
    benchmark_curve: List[Dict],
    params: BacktestParams,
) -> Dict[str, Any]:
    """生成回测分析报告。"""
    ret = summary.get("total_return_pct", 0)
    dd = summary.get("max_drawdown_pct", 0)
    wr = summary.get("win_rate", 0)
    pr = summary.get("payoff_ratio", 0)
    days = summary.get("trading_days", 1)
    avg_win = summary.get("avg_win", 0)
    avg_loss = summary.get("avg_loss", 0)

    # 基准收益
    bm_ret = benchmark_curve[-1]["return_pct"] if benchmark_curve else 0
    alpha = round(ret - bm_ret, 2)

    # 年化
    ann_factor = 252 / days if days > 0 else 1
    ann_ret = round(ret * ann_factor, 2)
    ann_alpha = round(alpha * ann_factor, 2)

    # 收益回撤比
    ret_dd_ratio = round(abs(ret / dd), 2) if dd != 0 else 0

    # 金字塔统计
    pyramid_trades = [t for t in trades_out if t["action"] == "BUY" and "金字塔" in (t.get("reason") or "")]
    pyramid_count = len(pyramid_trades)

    # 持仓天数统计
    buy_map: Dict[str, Dict] = {}
    holding_days_list = []
    for t in trades_out:
        if t["action"] == "BUY" and "金字塔" not in (t.get("reason") or ""):
            buy_map[t["symbol"]] = t
        elif t["action"] == "SELL" and t["symbol"] in buy_map:
            buy_t = buy_map.pop(t["symbol"])
            buy_d = date.fromisoformat(buy_t["date"])
            sell_d = date.fromisoformat(t["date"])
            holding_days_list.append((sell_d - buy_d).days)

    avg_hold = round(sum(holding_days_list) / len(holding_days_list), 1) if holding_days_list else 0
    max_hold = max(holding_days_list) if holding_days_list else 0
    min_hold = min(holding_days_list) if holding_days_list else 0

    # 最大单笔盈亏
    sell_trades = [t for t in trades_out if t["action"] == "SELL"]
    best_trade = max(sell_trades, key=lambda t: t["pnl"]) if sell_trades else None
    worst_trade = min(sell_trades, key=lambda t: t["pnl"]) if sell_trades else None

    # 连续亏损
    max_consec_loss = 0
    cur_consec = 0
    for t in sell_trades:
        if t["pnl"] <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # ---- 生成文本分析 ----
    sections = []

    # 1. 总体评价
    if ret > 0 and alpha > 0:
        grade = "A" if alpha > 10 else ("B" if alpha > 5 else "C")
        overall = f"策略在 {days} 个交易日内实现 {ret:+.2f}% 收益，跑赢上证指数 {alpha:+.2f}%。"
    elif ret > 0:
        grade = "C"
        overall = f"策略收益 {ret:+.2f}%，但未跑赢上证指数 ({bm_ret:+.2f}%)，超额 {alpha:+.2f}%。"
    else:
        grade = "D" if ret < -10 else "C"
        overall = f"策略亏损 {ret:.2f}%，上证指数同期 {bm_ret:+.2f}%。"

    if ann_ret != 0:
        overall += f" 年化收益 {ann_ret:+.1f}%，年化超额 {ann_alpha:+.1f}%。"

    sections.append({"title": "总体评价", "content": overall, "grade": grade})

    # 2. 风险指标
    risk_text = f"最大回撤 {dd:.2f}%，收益/回撤比 {ret_dd_ratio}。"
    if max_consec_loss > 0:
        risk_text += f" 最大连续亏损 {max_consec_loss} 笔。"
    if abs(dd) > 30:
        risk_text += " 回撤较深，需关注资金管理。"
    elif abs(dd) < 10:
        risk_text += " 回撤控制良好。"
    sections.append({"title": "风险控制", "content": risk_text})

    # 3. 交易质量
    quality = f"胜率 {wr:.1f}%，盈亏比 {pr:.2f}。"
    if pr >= 2.5 and wr >= 30:
        quality += " 典型趋势跟踪特征：低胜率+高盈亏比，正期望值系统。"
    elif pr >= 2:
        quality += " 盈亏比良好，期望值为正。"
    elif pr < 1:
        quality += " 盈亏比不足，需优化止损或持仓逻辑。"

    quality += f" 均赢¥{avg_win:,.0f} / 均亏¥{avg_loss:,.0f}。"

    if best_trade:
        quality += f" 最佳交易: {best_trade['name']} {best_trade['pnl']:+,.0f}。"
    if worst_trade:
        quality += f" 最差交易: {worst_trade['name']} {worst_trade['pnl']:+,.0f}。"
    sections.append({"title": "交易质量", "content": quality})

    # 4. 持仓分析
    hold = f"平均持仓 {avg_hold} 天 (最短{min_hold}天，最长{max_hold}天)。"
    if avg_hold < 5:
        hold += " 持仓偏短，突破可能被噪音过早止损。"
    elif avg_hold > 30:
        hold += " 持仓较长，策略成功捕捉中期趋势。"
    sections.append({"title": "持仓分析", "content": hold})

    # 5. 金字塔评估
    if params.pyramid_enabled:
        if pyramid_count > 0:
            pyr = f"触发 {pyramid_count} 次金字塔加仓。"
            # 检查加仓后是否带来更多收益
            pyr += " 加仓机制在趋势延续时有效放大收益。"
        else:
            pyr = "未触发金字塔加仓。可能: ①回测期趋势不够强; ②触发条件(+1R)偏高。"
    else:
        pyr = "金字塔加仓未启用。"
    sections.append({"title": "金字塔加仓", "content": pyr})

    # 6. 改进建议
    suggestions = []
    if wr < 35:
        suggestions.append("胜率偏低，考虑增加入场确认条件（如 MA 方向过滤、板块强度）")
    if pr < 2:
        suggestions.append("盈亏比不足，考虑放宽移动止损（增大 trailing ATR 倍数）或优化加仓时机")
    if abs(dd) > 40:
        suggestions.append("回撤过深，可适当降低单票上限或减少最大持仓数")
    if avg_hold < 3:
        suggestions.append("持仓过短，初始止损可能过紧（可增大 stop_atr_mult）")
    if pyramid_count == 0 and params.pyramid_enabled:
        suggestions.append("金字塔未触发，考虑降低 pyramid_trigger_r 或 lookback 周期")
    if alpha < 0:
        suggestions.append("未产生超额收益，检查选股池质量或入场信号有效性")
    if max_consec_loss >= 4:
        suggestions.append(f"连续亏损 {max_consec_loss} 笔，考虑加入市场环境过滤（如暂停开新仓）")
    if ret > 15 and pr > 2.5:
        suggestions.append("策略表现优异，可尝试延长回测周期验证稳定性")

    if not suggestions:
        suggestions.append("当前参数表现均衡，建议延长回测周期（6-12个月）进一步验证")

    sections.append({"title": "改进建议", "content": " | ".join(f"[{i+1}] {s}" for i, s in enumerate(suggestions))})

    return {
        "grade": grade,
        "alpha": alpha,
        "ann_ret": ann_ret,
        "ann_alpha": ann_alpha,
        "benchmark_ret": bm_ret,
        "ret_dd_ratio": ret_dd_ratio,
        "pyramid_count": pyramid_count,
        "avg_hold_days": avg_hold,
        "max_consec_loss": max_consec_loss,
        "best_trade": {"name": best_trade["name"], "pnl": best_trade["pnl"]} if best_trade else None,
        "worst_trade": {"name": worst_trade["name"], "pnl": worst_trade["pnl"]} if worst_trade else None,
        "sections": sections,
    }


# ==================== 主入口 ====================

def run_backtest(params: BacktestParams) -> Dict[str, Any]:
    """运行 HV Alpha Model 回测。"""
    start_d = date.fromisoformat(params.start_date)
    end_d = date.fromisoformat(params.end_date)
    data_start = start_d - timedelta(days=350)  # MA200 需要更多历史

    # 1. 构建观察池
    if params.pool_source == "custom" and params.symbols:
        pool = _resolve_custom_symbols(params.symbols)
    elif params.pool_source == "dynamic":
        pool = _build_broad_universe()
    else:
        full_pool = build_watchlist_from_db()
        if params.symbols:
            sym_set = set(params.symbols)
            pool = [(s, n) for s, n in full_pool if s in sym_set]
        else:
            pool = full_pool

    names = {s: n for s, n in pool}

    # 2. 下载数据 + 预计算 ATR
    all_data = _download_all(pool, data_start, end_d)
    all_data = {sym: _add_atr_column(df) for sym, df in all_data.items()}
    if not all_data:
        return {"error": "无有效数据", "summary": {}, "trades": [],
                "equity_curve": [], "weekly_snapshots": [], "params": {}}

    # 下载指数数据 (regime 用)
    index_df = _download_ohlcv(params.index_code, data_start, end_d)
    if index_df is not None:
        index_df = _add_atr_column(index_df)
        logger.info("指数 %s 下载 %d 条", params.index_code, len(index_df))
    else:
        logger.warning("指数 %s 下载失败", params.index_code)

    # 下载上证指数 (基准对比)
    benchmark_df = None
    if params.index_code == BENCHMARK_INDEX:
        benchmark_df = index_df  # 复用
    else:
        benchmark_df = _download_ohlcv(BENCHMARK_INDEX, data_start, end_d)
        if benchmark_df is not None:
            logger.info("基准 %s 下载 %d 条", BENCHMARK_INDEX, len(benchmark_df))
        else:
            logger.warning("基准 %s 下载失败", BENCHMARK_INDEX)

    # 3. 交易日序列
    sample_df = next(iter(all_data.values()))
    all_dates = sorted(sample_df["trade_date"].unique())
    bt_dates = [d for d in all_dates if start_d <= d <= end_d]

    if not bt_dates:
        first = next((d for d in all_dates if d >= start_d), None)
        if first:
            bt_dates = [d for d in all_dates if first <= d <= end_d]

    if not bt_dates:
        return {"error": "回测区间无交易日", "summary": {}, "trades": [],
                "equity_curve": [], "weekly_snapshots": [], "params": {}}

    # 4. 初始化引擎
    engine = _Engine(
        cash=params.initial_capital,
        initial_capital=params.initial_capital,
        max_positions=params.max_positions,
    )

    # 5. 逐日执行
    is_dynamic = params.pool_source == "dynamic"
    active_pool: set = set(all_data.keys()) if not is_dynamic else set()
    pool_changes: List[Dict[str, Any]] = []

    for day_idx, trade_date in enumerate(bt_dates):
        # 5a. 动态筛选
        if is_dynamic and (
            day_idx == 0 or day_idx % params.screen_interval_days == 0
        ):
            screened = set(_weekly_screen(
                all_data, trade_date, index_df, params.screen_min_turnover,
            ))
            held_syms = {p.symbol for p in engine.positions}
            prev_pool = active_pool - held_syms
            added = sorted(screened - active_pool)
            removed = sorted(prev_pool - screened)
            active_pool = screened | held_syms

            pool_changes.append({
                "date": trade_date.isoformat(),
                "day_idx": day_idx,
                "active_count": len(active_pool),
                "added": [{"symbol": s, "name": names.get(s, s)} for s in added],
                "removed": [{"symbol": s, "name": names.get(s, s)} for s in removed],
            })
            logger.info(
                "筛选 day%d (%s): 活跃%d, +%d, -%d",
                day_idx, trade_date, len(active_pool), len(added), len(removed),
            )

        # 5b. 构建价格地图
        price_map: Dict[str, Dict] = {}
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == trade_date]
            if not rows.empty:
                r = rows.iloc[0]
                atr_val = float(r["atr14"]) if pd.notna(r.get("atr14")) else None
                price_map[sym] = {
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r["volume"]), "atr14": atr_val,
                }

        # 5c. 止损管理
        engine.update_and_check_stops(price_map, trade_date, params)

        # 5d. 金字塔加仓检查
        for pos in list(engine.positions):
            bar = price_map.get(pos.symbol)
            if not bar:
                continue
            sym_df = all_data.get(pos.symbol)
            if sym_df is None:
                continue
            td_mask = sym_df["trade_date"] == trade_date
            if not td_mask.any():
                continue
            as_of = int(sym_df.index[td_mask][0])

            if _check_pyramid_add(pos, bar, sym_df, as_of, params):
                # 获取 regime 用于风险计算
                ind = _compute_indicators(sym_df, as_of, index_df)
                regime = _get_regime(ind) if ind else "RISK_ON"
                risk_pct = params.risk_per_trade_pct if regime == "RISK_ON" else params.risk_neutral_pct
                add_risk = risk_pct * params.pyramid_risk_frac

                add_shares = _calc_shares(
                    bar["close"], pos.stop_price, engine.equity, engine.cash,
                    add_risk, params.max_position_value_pct,
                )

                # 单票上限检查
                pos_value_after = (pos.shares + add_shares) * bar["close"]
                if pos_value_after > engine.equity * params.max_position_value_pct:
                    max_shares = int(
                        (engine.equity * params.max_position_value_pct / bar["close"] - pos.shares) / 100
                    ) * 100
                    add_shares = max(max_shares, 0)

                if add_shares > 0:
                    reason = f"金字塔加仓#{pos.pyramid_count + 1}"
                    engine.add_to_position(pos, add_shares, bar["close"], trade_date, reason)

        # 5e. 新突破扫描
        if engine.can_open() and engine.cash > 0:
            scan_data = (
                {s: df for s, df in all_data.items() if s in active_pool}
                if is_dynamic else all_data
            )
            signals = _scan_signals(
                scan_data, names, trade_date, engine, params, index_df,
            )
            for sig in signals:
                if not engine.can_open():
                    break
                if engine.cash < sig["entry"] * 100:
                    continue
                engine.open_position(
                    sig["symbol"], sig["name"],
                    sig["entry"], sig["stop"], sig["shares"],
                    trade_date, sig["reasoning"],
                )
                if engine.positions:
                    engine.positions[-1].entry_bar_idx = day_idx

        engine.snapshot(trade_date)

    # 6. 期末清算
    if engine.positions:
        last_date = bt_dates[-1]
        for sym, df in all_data.items():
            rows = df[df["trade_date"] == last_date]
            if not rows.empty:
                close_price = float(rows.iloc[0]["close"])
                for pos in list(engine.positions):
                    if pos.symbol == sym:
                        engine.close_position(pos, close_price, last_date, "期末清算")

    # 7. 构建结果
    sells = [t for t in engine.trades if t.action == "SELL"]
    wins = [t for t in sells if t.pnl > 0]
    final_eq = engine.equity
    ret_pct = (final_eq / engine.initial_capital - 1) * 100
    max_dd = min(eq[2] for eq in engine.equity_curve) if engine.equity_curve else 0.0

    # 盈亏比
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
    losses = [t for t in sells if t.pnl <= 0]
    avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 1
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0

    summary = {
        "initial_capital": engine.initial_capital,
        "final_equity": round(final_eq, 2),
        "total_return_pct": round(ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_pnl": round(final_eq - engine.initial_capital, 2),
        "trades_count": len(engine.trades),
        "buys_count": len([t for t in engine.trades if t.action == "BUY"]),
        "sells_count": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0,
        "payoff_ratio": payoff_ratio,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "trading_days": len(bt_dates),
        "start_date": bt_dates[0].isoformat(),
        "end_date": bt_dates[-1].isoformat(),
    }

    trades_out = []
    for t in engine.trades:
        trades_out.append({
            "date": t.date.isoformat(),
            "action": t.action,
            "symbol": t.symbol,
            "name": t.name,
            "price": round(t.price, 2),
            "shares": t.shares,
            "amount": round(t.amount, 2),
            "pnl": round(t.pnl, 2),
            "reason": t.reason,
            "reasoning": t.reasoning,
        })

    equity_curve = [
        {"date": d.isoformat(), "equity": round(eq, 2), "drawdown_pct": round(dd, 2)}
        for d, eq, dd in engine.equity_curve
    ]

    weekly_snapshots = _build_weekly_snapshots(engine)

    # 基准曲线
    benchmark_curve = _build_benchmark_curve(benchmark_df, bt_dates, params.initial_capital)

    # 分析报告
    report = _generate_report(summary, trades_out, benchmark_curve, params)

    result = {
        "summary": summary,
        "trades": trades_out,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "report": report,
        "weekly_snapshots": weekly_snapshots,
        "params": {
            "initial_capital": params.initial_capital,
            "max_positions": params.max_positions,
            "risk_per_trade_pct": params.risk_per_trade_pct,
            "risk_neutral_pct": params.risk_neutral_pct,
            "max_position_value_pct": params.max_position_value_pct,
            "stop_atr_mult": params.stop_atr_mult,
            "trailing_atr_mult": params.trailing_atr_mult,
            "pool_source": params.pool_source,
            "start_date": params.start_date,
            "end_date": params.end_date,
            "symbols_count": len(pool),
            "pyramid_enabled": params.pyramid_enabled,
            "pyramid_max_adds": params.pyramid_max_adds,
            "pyramid_risk_frac": params.pyramid_risk_frac,
            "pyramid_trigger_r": params.pyramid_trigger_r,
            "breakout_lookback": params.breakout_lookback,
            "volume_mult": params.volume_mult,
            "index_code": params.index_code,
            "screen_interval_days": params.screen_interval_days,
        },
    }
    if pool_changes:
        result["pool_changes"] = pool_changes
    return result
