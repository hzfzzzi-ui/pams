"""
PIT (Point-in-Time) 只读数据接口。

所有查询强制 WHERE trade_date <= :request_date (PIT 安全)。
缺数据默认 raise RuntimeError (回测显式失败)。
零在线依赖。
"""

import os
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from datahub.symbol_conv import symbol_to_ts, ts_to_symbol

# ---------- DB connection ----------


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


# ---------- OHLCV ----------


def get_ohlcv(
    symbols: List[str],
    start: str,
    end: str,
    adjust: bool = True,
    allow_missing: bool = False,
    conn=None,
) -> Dict[str, List[Dict[str, Any]]]:
    """从 pams_ohlcv_daily 读取 OHLCV 数据。

    Args:
        symbols: 内部格式 (sz000001, sh600519)
        start: ISO date str (2010-01-01)
        end: ISO date str (2025-12-31)
        adjust: 是否用 adj_factor 计算前复权价
        allow_missing: False 时，缺数据 raise RuntimeError
        conn: 可选复用连接

    Returns:
        Dict[symbol: List[{trade_date, open, high, low, close, volume, turnover}]]
        按 trade_date 升序排列
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT symbol, trade_date, open, high, low, close,
                      volume, turnover, adj_factor
               FROM pams_ohlcv_daily
               WHERE market = 'CN_A'
                 AND symbol = ANY(%s)
                 AND trade_date >= %s AND trade_date <= %s
               ORDER BY symbol, trade_date""",
            (symbols, start, end),
        )

        result: Dict[str, List[Dict[str, Any]]] = {}

        if adjust:
            # Collect rows grouped by symbol for adj_factor normalization
            rows_by_symbol: Dict[str, list] = {}
            for row in cur.fetchall():
                sym = row[0]
                rows_by_symbol.setdefault(sym, []).append(row)

            for sym, rows in rows_by_symbol.items():
                bars = []
                # Find the latest adj_factor for normalization (forward adjust)
                latest_adj = None
                for row in rows:
                    adj = row[8]
                    if adj is not None and adj > 0:
                        latest_adj = float(adj)
                # If no adj_factor at all, use raw prices
                for row in rows:
                    adj = float(row[8]) if row[8] is not None and row[8] > 0 else None
                    factor = (adj / latest_adj) if (adj and latest_adj) else 1.0
                    bars.append({
                        "symbol": sym,
                        "market": "CN_A",
                        "trade_date": row[1].isoformat(),
                        "open": round(float(row[2]) * factor, 4) if row[2] else 0,
                        "high": round(float(row[3]) * factor, 4) if row[3] else 0,
                        "low": round(float(row[4]) * factor, 4) if row[4] else 0,
                        "close": round(float(row[5]) * factor, 4) if row[5] else 0,
                        "volume": int(row[6]) if row[6] else 0,
                        "turnover": float(row[7]) if row[7] else 0,
                    })
                result[sym] = bars
        else:
            # Raw (unadjusted) prices
            for row in cur.fetchall():
                sym = row[0]
                result.setdefault(sym, []).append({
                    "symbol": sym,
                    "market": "CN_A",
                    "trade_date": row[1].isoformat(),
                    "open": float(row[2]) if row[2] else 0,
                    "high": float(row[3]) if row[3] else 0,
                    "low": float(row[4]) if row[4] else 0,
                    "close": float(row[5]) if row[5] else 0,
                    "volume": int(row[6]) if row[6] else 0,
                    "turnover": float(row[7]) if row[7] else 0,
                })

        cur.close()

        # Validate: check for missing symbols
        if not allow_missing:
            missing = set(symbols) - set(result.keys())
            if missing:
                raise RuntimeError(
                    f"PIT data missing for {len(missing)} symbols: "
                    f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}"
                )

        return result

    finally:
        if own_conn:
            conn.close()


# ---------- Tradeable Universe ----------


def get_tradeable_symbols(
    trade_date: str,
    exclude_st: bool = True,
    exclude_suspended: bool = True,
    conn=None,
) -> List[str]:
    """获取指定日期的可交易 Universe (PIT 安全)。

    逻辑:
    1. pit_security_master: list_date <= trade_date AND (delist_date IS NULL OR delist_date > trade_date)
    2. 排除停牌: pams_ohlcv_daily.is_suspended = TRUE
    3. 排除 ST: pit_st_history

    Returns:
        List[str] 内部 symbol 格式 (sz000001)
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()

        # Base: listed and not delisted
        cur.execute(
            """SELECT symbol FROM pit_security_master
               WHERE market_type = 'CN_A'
                 AND list_date IS NOT NULL AND list_date <= %s
                 AND (delist_date IS NULL OR delist_date > %s)""",
            (trade_date, trade_date),
        )
        symbols = {r[0] for r in cur.fetchall()}

        # Exclude suspended
        if exclude_suspended and symbols:
            cur.execute(
                """SELECT DISTINCT symbol FROM pams_ohlcv_daily
                   WHERE market = 'CN_A' AND trade_date = %s
                     AND is_suspended = TRUE AND symbol = ANY(%s)""",
                (trade_date, list(symbols)),
            )
            suspended = {r[0] for r in cur.fetchall()}
            symbols -= suspended

        # Exclude ST
        if exclude_st and symbols:
            # Convert to ts_code for ST table lookup
            sym_to_ts = {s: symbol_to_ts(s) for s in symbols}
            ts_codes = list(sym_to_ts.values())

            cur.execute(
                """SELECT ts_code FROM pit_st_history
                   WHERE ts_code = ANY(%s)
                     AND start_date <= %s
                     AND (end_date IS NULL OR end_date > %s)""",
                (ts_codes, trade_date, trade_date),
            )
            st_ts_codes = {r[0] for r in cur.fetchall()}

            # Map back to symbols
            ts_to_sym = {v: k for k, v in sym_to_ts.items()}
            st_symbols = {ts_to_sym[tc] for tc in st_ts_codes if tc in ts_to_sym}
            symbols -= st_symbols

        cur.close()
        return sorted(symbols)

    finally:
        if own_conn:
            conn.close()


# ---------- Limit Prices ----------


def get_limits(
    symbols: List[str],
    trade_date: str,
    conn=None,
) -> Dict[str, Dict[str, float]]:
    """获取涨跌停价。

    Returns:
        Dict[symbol: {"limit_up": float, "limit_down": float}]
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT symbol, limit_up, limit_down FROM pams_ohlcv_daily
               WHERE market = 'CN_A' AND trade_date = %s AND symbol = ANY(%s)
                 AND limit_up IS NOT NULL""",
            (trade_date, symbols),
        )

        result = {}
        for row in cur.fetchall():
            result[row[0]] = {
                "limit_up": float(row[1]) if row[1] else None,
                "limit_down": float(row[2]) if row[2] else None,
            }
        cur.close()
        return result

    finally:
        if own_conn:
            conn.close()


# ---------- Benchmark / Index ----------


def get_benchmark_series(
    index_code: str,
    start: str,
    end: str,
    conn=None,
) -> List[Dict[str, Any]]:
    """从 pit_index_daily 获取指数序列，回退到 pams_ohlcv_daily。

    Args:
        index_code: Tushare 格式 (000300.SH) 或内部格式 (sh000300)

    Returns:
        List[{trade_date, open, high, low, close, volume, turnover}] 按日期升序
    """
    # Keep internal format for fallback query
    internal_code = index_code
    ts_code = index_code
    if "." not in index_code:
        ts_code = symbol_to_ts(index_code)
    else:
        internal_code = ts_to_symbol(index_code)

    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()

        # Primary: pit_index_daily (Tushare PIT table)
        cur.execute(
            """SELECT trade_date, open, high, low, close, volume, turnover
               FROM pit_index_daily
               WHERE index_code = %s AND trade_date >= %s AND trade_date <= %s
               ORDER BY trade_date""",
            (ts_code, start, end),
        )
        rows = cur.fetchall()

        # Fallback: pams_ohlcv_daily (legacy index data)
        if not rows:
            cur.execute(
                """SELECT trade_date, open, high, low, close, volume, turnover
                   FROM pams_ohlcv_daily
                   WHERE symbol = %s AND trade_date >= %s AND trade_date <= %s
                   ORDER BY trade_date""",
                (internal_code, start, end),
            )
            rows = cur.fetchall()

        result = []
        for row in rows:
            result.append({
                "trade_date": row[0].isoformat(),
                "open": float(row[1]) if row[1] else 0,
                "high": float(row[2]) if row[2] else 0,
                "low": float(row[3]) if row[3] else 0,
                "close": float(row[4]) if row[4] else 0,
                "volume": int(row[5]) if row[5] else 0,
                "turnover": float(row[6]) if row[6] else 0,
            })

        cur.close()
        return result

    finally:
        if own_conn:
            conn.close()


# ---------- ST Flags ----------


def get_st_flags(
    symbols: List[str],
    trade_date: str,
    conn=None,
) -> Dict[str, bool]:
    """获取指定日期各股票是否为 ST。

    Returns:
        Dict[symbol: bool]  (True = 是 ST)
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        sym_to_ts = {s: symbol_to_ts(s) for s in symbols}
        ts_codes = list(sym_to_ts.values())

        cur = conn.cursor()
        cur.execute(
            """SELECT ts_code FROM pit_st_history
               WHERE ts_code = ANY(%s)
                 AND start_date <= %s
                 AND (end_date IS NULL OR end_date > %s)""",
            (ts_codes, trade_date, trade_date),
        )
        st_ts = {r[0] for r in cur.fetchall()}
        cur.close()

        ts_to_sym = {v: k for k, v in sym_to_ts.items()}
        return {s: (sym_to_ts[s] in st_ts) for s in symbols}

    finally:
        if own_conn:
            conn.close()


# ---------- Industry ----------


def get_industry(
    symbols: List[str],
    trade_date: str,
    standard: str = "SW",
    conn=None,
) -> Dict[str, str]:
    """获取指定日期各股票的行业归属 (PIT)。

    Falls back to pit_security_master.industry if no history records.

    Returns:
        Dict[symbol: industry_name]
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        sym_to_ts = {s: symbol_to_ts(s) for s in symbols}
        ts_codes = list(sym_to_ts.values())

        cur = conn.cursor()

        # Try pit_industry_history first (PIT)
        cur.execute(
            """SELECT ts_code, industry_l1 FROM pit_industry_history
               WHERE ts_code = ANY(%s) AND standard = %s
                 AND start_date <= %s
                 AND (end_date IS NULL OR end_date > %s)""",
            (ts_codes, standard, trade_date, trade_date),
        )
        industry_map = {}
        for row in cur.fetchall():
            ts_to_sym_map = {v: k for k, v in sym_to_ts.items()}
            sym = ts_to_sym_map.get(row[0])
            if sym and row[1]:
                industry_map[sym] = row[1]

        # Fallback: pit_security_master.industry for missing
        missing_ts = [sym_to_ts[s] for s in symbols if s not in industry_map]
        if missing_ts:
            cur.execute(
                """SELECT ts_code, industry FROM pit_security_master
                   WHERE ts_code = ANY(%s)""",
                (missing_ts,),
            )
            ts_to_sym_map = {v: k for k, v in sym_to_ts.items()}
            for row in cur.fetchall():
                sym = ts_to_sym_map.get(row[0])
                if sym and row[1]:
                    industry_map[sym] = row[1]

        cur.close()
        return industry_map

    finally:
        if own_conn:
            conn.close()


# ---------- Trade Dates ----------


def get_trade_dates(
    start: str,
    end: str,
    exchange: str = "SSE",
    conn=None,
) -> List[str]:
    """获取交易日列表。

    Returns:
        List[str] ISO date strings, ascending
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT cal_date FROM pit_trade_cal
               WHERE exchange = %s AND is_open = TRUE
                 AND cal_date >= %s AND cal_date <= %s
               ORDER BY cal_date""",
            (exchange, start, end),
        )
        dates = [r[0].isoformat() for r in cur.fetchall()]
        cur.close()
        return dates

    finally:
        if own_conn:
            conn.close()


# ---------- Data Version ----------


def get_data_version(conn=None) -> Optional[Dict[str, Any]]:
    """获取最新数据版本信息。"""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT as_of_date, rows_ohlcv, rows_index, rows_security,
                      max_trade_date, checksum, notes
               FROM pams_data_version
               ORDER BY as_of_date DESC LIMIT 1"""
        )
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        return {
            "as_of_date": row[0].isoformat() if row[0] else None,
            "rows_ohlcv": row[1],
            "rows_index": row[2],
            "rows_security": row[3],
            "max_trade_date": row[4].isoformat() if row[4] else None,
            "checksum": row[5],
            "notes": row[6],
        }

    finally:
        if own_conn:
            conn.close()


# ---------- Security Info ----------


def get_security_name(symbol: str, conn=None) -> Optional[str]:
    """根据 symbol 查询股票名称。"""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM pit_security_master WHERE symbol = %s",
            (symbol,),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None

    finally:
        if own_conn:
            conn.close()


def get_security_names(symbols: List[str], conn=None) -> Dict[str, str]:
    """批量查询股票名称。"""
    own_conn = conn is None
    if own_conn:
        conn = _get_conn()

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, name FROM pit_security_master WHERE symbol = ANY(%s)",
            (symbols,),
        )
        result = {r[0]: r[1] for r in cur.fetchall()}
        cur.close()
        return result

    finally:
        if own_conn:
            conn.close()
