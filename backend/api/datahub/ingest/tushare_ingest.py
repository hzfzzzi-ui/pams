#!/usr/bin/env python3
"""
Tushare PIT 数据导入模块。

将 Tushare Pro 数据落地到 Postgres PIT 表，
为回测和策略提供离线、可复现的数据源。

Usage:
    python -m datahub.ingest.tushare_ingest backfill --start 2010-01-01 --end 2026-02-19
    python -m datahub.ingest.tushare_ingest daily --date 2026-02-19
    python -m datahub.ingest.tushare_ingest status

环境变量:
    TUSHARE_TOKEN  (必填)
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
"""

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import tushare as ts

from datahub.symbol_conv import exchange_from_ts, ts_to_symbol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------- Rate limiting ----------

CALL_INTERVAL = 0.35  # seconds between API calls (< 200/min)
_last_call_ts = 0.0


def _rate_limit():
    """Enforce minimum interval between Tushare API calls."""
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < CALL_INTERVAL:
        time.sleep(CALL_INTERVAL - elapsed)
    _last_call_ts = time.time()


# ---------- DB connection ----------


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _init_pro() -> ts.pro_api:
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        log.error("TUSHARE_TOKEN environment variable is required")
        sys.exit(1)
    return ts.pro_api(token)


# ---------- Step 1: Security Master ----------


def ingest_security_master(pro: ts.pro_api, conn) -> int:
    """stock_basic() → pit_security_master (全量 upsert).

    stock_basic has strict rate limits (1/hour at 120 points).
    Fetches L/D/P statuses with retry on rate-limit errors.
    """
    log.info("[1/7] Ingesting security master ...")

    rows_total = 0
    for status in ("L", "D", "P"):
        # stock_basic has strict rate limit (1/min at 120 points)
        log.info(f"  Fetching stock_basic(list_status={status}) ...")
        time.sleep(62)  # Ensure 1+ minute between calls
        df = pro.stock_basic(
            exchange="",
            list_status=status,
            fields="ts_code,symbol,name,exchange,list_date,delist_date,industry",
        )
        if df is None or df.empty:
            continue

        records = []
        for _, r in df.iterrows():
            ts_code = r["ts_code"]
            records.append((
                ts_code,
                ts_to_symbol(ts_code),
                r.get("name"),
                r.get("exchange", ""),
                _parse_date(r.get("list_date")),
                _parse_date(r.get("delist_date")),
                r.get("industry"),
                "CN_A",
            ))

        sql = """
            INSERT INTO pit_security_master
                (ts_code, symbol, name, exchange, list_date, delist_date, industry, market_type)
            VALUES %s
            ON CONFLICT (ts_code) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                name = EXCLUDED.name,
                exchange = EXCLUDED.exchange,
                list_date = EXCLUDED.list_date,
                delist_date = EXCLUDED.delist_date,
                industry = EXCLUDED.industry,
                updated_at = NOW()
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=500)
        conn.commit()
        cur.close()
        rows_total += len(records)

    log.info(f"  security_master: {rows_total} rows upserted")
    return rows_total


def ingest_security_master_delisted(pro: ts.pro_api, conn) -> int:
    """补充/更新退市标的，确保 delist_date 可用。"""
    log.info("  Ingesting delisted security master ...")

    _rate_limit()
    df = pro.stock_basic(
        exchange="",
        list_status="D",
        fields="ts_code,symbol,name,exchange,list_date,delist_date,industry",
    )
    if df is None or df.empty:
        log.info("  security_master(delisted): 0 rows")
        return 0

    records = []
    for _, r in df.iterrows():
        ts_code = r["ts_code"]
        records.append((
            ts_code,
            ts_to_symbol(ts_code),
            r.get("name"),
            r.get("exchange", ""),
            _parse_date(r.get("list_date")),
            _parse_date(r.get("delist_date")),
            r.get("industry"),
            "CN_A",
        ))

    sql = """
        INSERT INTO pit_security_master
            (ts_code, symbol, name, exchange, list_date, delist_date, industry, market_type)
        VALUES %s
        ON CONFLICT (ts_code) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            name = EXCLUDED.name,
            exchange = EXCLUDED.exchange,
            list_date = EXCLUDED.list_date,
            delist_date = EXCLUDED.delist_date,
            industry = EXCLUDED.industry,
            updated_at = NOW()
    """
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, sql, records, page_size=500)
    conn.commit()
    cur.close()
    log.info("  security_master(delisted): %d rows upserted", len(records))
    return len(records)


# ---------- Step 2: Trade Calendar ----------


def ingest_trade_cal(pro: ts.pro_api, conn) -> int:
    """trade_cal() → pit_trade_cal (SSE + SZSE)."""
    log.info("[2/7] Ingesting trade calendar ...")

    rows_total = 0
    for exchange in ("SSE", "SZSE"):
        _rate_limit()
        try:
            df = pro.trade_cal(exchange=exchange, fields="exchange,cal_date,is_open")
        except Exception as e:
            log.warning(f"  trade_cal API failed for {exchange}: {e}")
            continue
        if df is None or df.empty:
            continue

        records = [
            (r["exchange"], _parse_date(r["cal_date"]), bool(r["is_open"]))
            for _, r in df.iterrows()
            if r.get("cal_date")
        ]

        sql = """
            INSERT INTO pit_trade_cal (exchange, cal_date, is_open)
            VALUES %s
            ON CONFLICT (exchange, cal_date) DO UPDATE SET
                is_open = EXCLUDED.is_open
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=1000)
        conn.commit()
        cur.close()
        rows_total += len(records)

    log.info(f"  trade_cal: {rows_total} rows upserted")
    return rows_total


def derive_trade_cal_from_ohlcv(conn) -> int:
    """从已有 OHLCV 数据推导交易日历 (trade_cal API 无权限时的替代方案)."""
    log.info("  Deriving trade calendar from OHLCV data ...")

    cur = conn.cursor()
    rows_total = 0
    for exchange in ("SSE", "SZSE"):
        cur.execute("""
            INSERT INTO pit_trade_cal (exchange, cal_date, is_open)
            SELECT %s, trade_date, TRUE
            FROM (SELECT DISTINCT trade_date FROM pams_ohlcv_daily WHERE market = 'CN_A') t
            ON CONFLICT (exchange, cal_date) DO NOTHING
        """, (exchange,))
        rows_total += cur.rowcount

    conn.commit()
    cur.close()
    log.info(f"  trade_cal (derived from OHLCV): {rows_total} rows inserted")
    return rows_total


# ---------- Step 3: OHLCV + adj_factor ----------


def ingest_ohlcv_batch(
    pro: ts.pro_api,
    conn,
    ts_codes: list[str],
    start_date: str,
    end_date: str,
) -> int:
    """daily() + adj_factor() → pams_ohlcv_daily (per stock, upsert)."""
    log.info(f"[3/7] Ingesting OHLCV for {len(ts_codes)} stocks ...")

    rows_total = 0
    for i, ts_code in enumerate(ts_codes):
        if (i + 1) % 100 == 0:
            log.info(f"  OHLCV progress: {i + 1}/{len(ts_codes)}")

        try:
            # Fetch daily OHLCV (unadjusted)
            _rate_limit()
            df_daily = pro.daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )

            # Fetch adj_factor
            _rate_limit()
            df_adj = pro.adj_factor(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            log.warning(f"  OHLCV fetch failed for {ts_code}: {e}")
            continue

        if df_daily is None or df_daily.empty:
            continue

        # Merge adj_factor
        if df_adj is not None and not df_adj.empty:
            df_daily = df_daily.merge(
                df_adj[["trade_date", "adj_factor"]],
                on="trade_date",
                how="left",
            )
        else:
            df_daily["adj_factor"] = None

        symbol = ts_to_symbol(ts_code)
        records = []
        for _, r in df_daily.iterrows():
            td = _parse_date(r.get("trade_date"))
            if td is None:
                continue
            # Data quality: skip rows with zero OHLCV
            if not (r.get("open", 0) > 0 and r.get("close", 0) > 0):
                continue
            adj = r.get("adj_factor")
            if pd.notna(adj) and adj <= 0:
                continue  # Invalid adj_factor

            records.append((
                symbol,
                "CN_A",
                td,
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                int(r.get("vol", 0) * 100) if pd.notna(r.get("vol")) else 0,  # Tushare vol in 手
                r.get("amount", 0) * 1000 if pd.notna(r.get("amount")) else 0,  # Tushare amount in 千元
                adj if pd.notna(adj) else None,
            ))

        if not records:
            continue

        sql = """
            INSERT INTO pams_ohlcv_daily
                (symbol, market, trade_date, open, high, low, close, volume, turnover, adj_factor)
            VALUES %s
            ON CONFLICT (symbol, market, trade_date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                turnover = EXCLUDED.turnover,
                adj_factor = EXCLUDED.adj_factor
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=500)
        conn.commit()
        cur.close()
        rows_total += len(records)

    log.info(f"  ohlcv: {rows_total} rows upserted")
    return rows_total


def ingest_ohlcv_single_day(
    pro: ts.pro_api,
    conn,
    trade_date: str,
) -> int:
    """Fast path for one trading day: pull all stocks in one request by trade_date."""
    td_fmt = trade_date.replace("-", "")
    log.info("  ohlcv(single-day): fetching all symbols for %s ...", td_fmt)

    _rate_limit()
    try:
        df_daily = pro.daily(
            trade_date=td_fmt,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )
    except Exception as e:
        log.warning("  ohlcv(single-day) failed for %s: %s", td_fmt, e)
        return 0

    if df_daily is None or df_daily.empty:
        log.info("  ohlcv(single-day): no rows for %s", td_fmt)
        return 0

    # Try attach adj_factor for the same trade_date.
    df_adj = None
    try:
        _rate_limit()
        df_adj = pro.adj_factor(
            trade_date=td_fmt,
            fields="ts_code,trade_date,adj_factor",
        )
    except Exception as e:
        log.warning("  adj_factor(single-day) skipped for %s: %s", td_fmt, e)

    if df_adj is not None and not df_adj.empty:
        df_daily = df_daily.merge(
            df_adj[["ts_code", "trade_date", "adj_factor"]],
            on=["ts_code", "trade_date"],
            how="left",
        )
    else:
        df_daily["adj_factor"] = None

    records = []
    for _, r in df_daily.iterrows():
        td = _parse_date(r.get("trade_date"))
        if td is None:
            continue
        if not (r.get("open", 0) > 0 and r.get("close", 0) > 0):
            continue
        adj = r.get("adj_factor")
        if pd.notna(adj) and adj <= 0:
            continue

        records.append((
            ts_to_symbol(r["ts_code"]),
            "CN_A",
            td,
            r.get("open"),
            r.get("high"),
            r.get("low"),
            r.get("close"),
            int(r.get("vol", 0) * 100) if pd.notna(r.get("vol")) else 0,
            r.get("amount", 0) * 1000 if pd.notna(r.get("amount")) else 0,
            adj if pd.notna(adj) else None,
        ))

    if not records:
        log.info("  ohlcv(single-day): no valid rows after cleanup for %s", td_fmt)
        return 0

    sql = """
        INSERT INTO pams_ohlcv_daily
            (symbol, market, trade_date, open, high, low, close, volume, turnover, adj_factor)
        VALUES %s
        ON CONFLICT (symbol, market, trade_date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            turnover = EXCLUDED.turnover,
            adj_factor = EXCLUDED.adj_factor
    """
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, sql, records, page_size=1000)
    conn.commit()
    cur.close()
    log.info("  ohlcv(single-day): %d rows upserted", len(records))
    return len(records)


# ---------- Step 4: Index Daily ----------

# Key A-share indexes
INDEX_CODES = [
    "000001.SH",  # 上证指数
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000016.SH",  # 上证50
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "399005.SZ",  # 中小板指
]


def ingest_index_daily(
    pro: ts.pro_api,
    conn,
    start_date: str,
    end_date: str,
) -> int:
    """index_daily() → pit_index_daily. Falls back to AkShare if Tushare fails."""
    log.info("[4/7] Ingesting index daily ...")

    rows_total = 0
    tushare_failed = False
    for idx_code in INDEX_CODES:
        _rate_limit()
        try:
            df = pro.index_daily(
                ts_code=idx_code,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            log.warning(f"  Index fetch failed for {idx_code}: {e}")
            tushare_failed = True
            break  # All index_daily calls will likely fail if one does

        if df is None or df.empty:
            continue

        records = [
            (
                idx_code,
                _parse_date(r["trade_date"]),
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                int(r.get("vol", 0) * 100) if pd.notna(r.get("vol")) else 0,
                r.get("amount", 0) * 1000 if pd.notna(r.get("amount")) else 0,
            )
            for _, r in df.iterrows()
            if r.get("trade_date")
        ]

        sql = """
            INSERT INTO pit_index_daily
                (index_code, trade_date, open, high, low, close, volume, turnover)
            VALUES %s
            ON CONFLICT (index_code, trade_date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                turnover = EXCLUDED.turnover
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=500)
        conn.commit()
        cur.close()
        rows_total += len(records)

    # Fallback to AkShare if Tushare index_daily API is unavailable
    if tushare_failed:
        rows_total += _ingest_index_daily_akshare(conn, start_date, end_date)

    log.info(f"  index_daily: {rows_total} rows upserted")
    return rows_total


def sync_index_to_ohlcv_daily(
    conn,
    start_date: str,
    end_date: str,
) -> int:
    """Sync pit_index_daily into pams_ohlcv_daily as market='CN_INDEX'."""
    log.info("  Syncing index rows into pams_ohlcv_daily (CN_INDEX) ...")

    sql = """
        INSERT INTO pams_ohlcv_daily
            (symbol, market, trade_date, open, high, low, close, volume, turnover, adj_factor)
        SELECT
            CASE
                WHEN i.index_code LIKE '%%.SH' THEN 'sh' || left(i.index_code, 6)
                WHEN i.index_code LIKE '%%.SZ' THEN 'sz' || left(i.index_code, 6)
                ELSE lower(i.index_code)
            END AS symbol,
            'CN_INDEX' AS market,
            i.trade_date,
            i.open,
            i.high,
            i.low,
            i.close,
            COALESCE(i.volume, 0),
            COALESCE(i.turnover, 0),
            NULL::numeric AS adj_factor
        FROM pit_index_daily i
        WHERE i.trade_date >= %s AND i.trade_date <= %s
        ON CONFLICT (symbol, market, trade_date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            turnover = EXCLUDED.turnover
    """
    cur = conn.cursor()
    cur.execute(sql, (_parse_date(start_date), _parse_date(end_date)))
    affected = cur.rowcount
    conn.commit()
    cur.close()

    log.info("  CN_INDEX sync: %d rows upserted", affected)
    return affected


# Tushare ts_code → AkShare symbol mapping for indexes
_INDEX_TS_TO_AK = {
    "000001.SH": "sh000001",
    "000300.SH": "sh000300",
    "000905.SH": "sh000905",
    "000016.SH": "sh000016",
    "399001.SZ": "sz399001",
    "399006.SZ": "sz399006",
    "399005.SZ": "sz399005",
}


def _ingest_index_daily_akshare(conn, start_date: str, end_date: str) -> int:
    """AkShare fallback for index daily data."""
    log.info("  Falling back to AkShare for index daily ...")

    try:
        import akshare as ak
    except ImportError:
        log.warning("  AkShare not installed, skipping index daily fallback")
        return 0

    # Convert YYYYMMDD → YYYY-MM-DD for comparison
    start_iso = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}" if len(start_date) == 8 else start_date
    end_iso = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}" if len(end_date) == 8 else end_date

    rows_total = 0
    for ts_code, ak_symbol in _INDEX_TS_TO_AK.items():
        try:
            df = ak.stock_zh_index_daily(symbol=ak_symbol)
            if df is None or df.empty:
                continue

            df["date_iso"] = df["date"].dt.strftime("%Y-%m-%d")
            df = df[(df["date_iso"] >= start_iso) & (df["date_iso"] <= end_iso)]

            records = []
            for _, r in df.iterrows():
                td = r["date"].date() if hasattr(r["date"], "date") else r["date"]
                records.append((
                    ts_code, td,
                    r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                    int(r.get("volume", 0)),
                    float(r.get("amount", 0)) if pd.notna(r.get("amount")) else 0,
                ))

            if not records:
                continue

            sql = """
                INSERT INTO pit_index_daily
                    (index_code, trade_date, open, high, low, close, volume, turnover)
                VALUES %s
                ON CONFLICT (index_code, trade_date) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, turnover = EXCLUDED.turnover
            """
            cur = conn.cursor()
            psycopg2.extras.execute_values(cur, sql, records, page_size=500)
            conn.commit()
            cur.close()
            rows_total += len(records)
            log.info(f"  AkShare {ts_code} ({ak_symbol}): {len(records)} rows")

        except Exception as e:
            log.warning(f"  AkShare index failed for {ak_symbol}: {e}")

    return rows_total


# ---------- Step 5: ST History ----------


def ingest_st_history(pro: ts.pro_api, conn) -> int:
    """namechange() → pit_st_history (筛选含 ST 的 name 变更)."""
    log.info("[5/7] Ingesting ST history from namechange ...")

    # Fetch all security codes
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM pit_security_master")
    all_codes = [r[0] for r in cur.fetchall()]
    cur.close()

    rows_total = 0
    for i, ts_code in enumerate(all_codes):
        if (i + 1) % 200 == 0:
            log.info(f"  ST history progress: {i + 1}/{len(all_codes)}")

        _rate_limit()
        try:
            df = pro.namechange(ts_code=ts_code)
        except Exception as e:
            log.warning(f"  namechange failed for {ts_code}: {e}")
            continue

        if df is None or df.empty:
            continue

        # Filter rows where name contains ST
        records = []
        for _, r in df.iterrows():
            name = r.get("name", "")
            if "ST" not in str(name).upper():
                continue

            st_type = "ST"
            if "*ST" in str(name):
                st_type = "*ST"
            elif "S*ST" in str(name):
                st_type = "S*ST"

            records.append((
                ts_code,
                _parse_date(r.get("start_date")),
                _parse_date(r.get("end_date")),
                st_type,
                None,  # name_before (could extract but not critical)
                name,
            ))

        if not records:
            continue

        sql = """
            INSERT INTO pit_st_history
                (ts_code, start_date, end_date, st_type, name_before, name_after)
            VALUES %s
            ON CONFLICT (ts_code, start_date) DO UPDATE SET
                end_date = EXCLUDED.end_date,
                st_type = EXCLUDED.st_type,
                name_after = EXCLUDED.name_after
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=200)
        conn.commit()
        cur.close()
        rows_total += len(records)

    log.info(f"  st_history: {rows_total} rows upserted")
    return rows_total


# ---------- Step 6: Limit Up/Down ----------


def ingest_stk_limit(
    pro: ts.pro_api,
    conn,
    start_date: str,
    end_date: str,
) -> int:
    """stk_limit() → pams_ohlcv_daily.limit_up/limit_down (按日期拉取)."""
    log.info("[6/7] Ingesting limit prices ...")

    # Get trading dates in range
    cur = conn.cursor()
    cur.execute(
        """SELECT cal_date FROM pit_trade_cal
           WHERE exchange = 'SSE' AND is_open = TRUE
             AND cal_date >= %s AND cal_date <= %s
           ORDER BY cal_date""",
        (start_date, end_date),
    )
    trade_dates = [r[0].strftime("%Y%m%d") for r in cur.fetchall()]
    cur.close()

    rows_total = 0
    for i, td in enumerate(trade_dates):
        if (i + 1) % 50 == 0:
            log.info(f"  stk_limit progress: {i + 1}/{len(trade_dates)} days")

        _rate_limit()
        try:
            df = pro.stk_limit(trade_date=td)
        except Exception as e:
            log.warning(f"  stk_limit failed for {td}: {e}")
            continue

        if df is None or df.empty:
            continue

        records = []
        for _, r in df.iterrows():
            symbol = ts_to_symbol(r["ts_code"])
            records.append((
                r.get("up_limit"),
                r.get("down_limit"),
                symbol,
                "CN_A",
                _parse_date(td),
            ))

        if not records:
            continue

        sql = """
            UPDATE pams_ohlcv_daily
            SET limit_up = data.limit_up, limit_down = data.limit_down
            FROM (VALUES %s) AS data(limit_up, limit_down, symbol, market, trade_date)
            WHERE pams_ohlcv_daily.symbol = data.symbol
              AND pams_ohlcv_daily.market = data.market
              AND pams_ohlcv_daily.trade_date = data.trade_date
        """
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, sql, records, page_size=500)
        conn.commit()
        cur.close()
        rows_total += len(records)

    log.info(f"  stk_limit: {rows_total} rows updated")
    if rows_total == 0:
        # 权限不足时退化为从历史价格推导（用于回测约束近似）
        rows_total = derive_limit_prices_from_ohlcv(conn, start_date, end_date)
    return rows_total


def derive_limit_prices_from_ohlcv(conn, start_date: str, end_date: str) -> int:
    """Derive limit_up/limit_down from previous close with board/ST heuristics."""
    log.info("  Deriving limit prices from OHLCV (fallback) ...")
    sql = """
        WITH base AS (
            SELECT
                d.symbol,
                d.trade_date,
                lag(d.close) OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS prev_close
            FROM pams_ohlcv_daily d
            WHERE d.market = 'CN_A'
              AND d.trade_date >= %s
              AND d.trade_date <= %s
        ),
        pct AS (
            SELECT
                b.symbol,
                b.trade_date,
                b.prev_close,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM pit_security_master sm
                        JOIN pit_st_history st ON st.ts_code = sm.ts_code
                        WHERE sm.symbol = b.symbol
                          AND st.start_date <= b.trade_date
                          AND (st.end_date IS NULL OR st.end_date >= b.trade_date)
                    ) THEN 0.05
                    WHEN b.symbol LIKE 'sh688%%' OR b.symbol LIKE 'sz300%%' THEN 0.20
                    WHEN b.symbol LIKE 'bj%%' THEN 0.30
                    ELSE 0.10
                END AS lp
            FROM base b
            WHERE b.prev_close IS NOT NULL
        )
        UPDATE pams_ohlcv_daily d
        SET
            limit_up = ROUND((p.prev_close * (1 + p.lp))::numeric, 4),
            limit_down = ROUND((p.prev_close * (1 - p.lp))::numeric, 4)
        FROM pct p
        WHERE d.market = 'CN_A'
          AND d.symbol = p.symbol
          AND d.trade_date = p.trade_date
          AND d.trade_date >= %s
          AND d.trade_date <= %s
          AND (d.limit_up IS NULL OR d.limit_down IS NULL)
    """
    cur = conn.cursor()
    cur.execute(sql, (_parse_date(start_date), _parse_date(end_date), _parse_date(start_date), _parse_date(end_date)))
    affected = cur.rowcount
    conn.commit()
    cur.close()
    log.info("  limit fallback: %d rows updated", affected)
    return affected


# ---------- Step 7: Suspend ----------


def ingest_suspend(
    pro: ts.pro_api,
    conn,
    start_date: str,
    end_date: str,
) -> int:
    """suspend_d() → pams_ohlcv_daily.is_suspended."""
    log.info("[7/7] Ingesting suspend data ...")

    # Get trading dates in range
    cur = conn.cursor()
    cur.execute(
        """SELECT cal_date FROM pit_trade_cal
           WHERE exchange = 'SSE' AND is_open = TRUE
             AND cal_date >= %s AND cal_date <= %s
           ORDER BY cal_date""",
        (start_date, end_date),
    )
    trade_dates = [r[0].strftime("%Y%m%d") for r in cur.fetchall()]
    cur.close()

    rows_total = 0
    for i, td in enumerate(trade_dates):
        if (i + 1) % 50 == 0:
            log.info(f"  suspend progress: {i + 1}/{len(trade_dates)} days")

        _rate_limit()
        try:
            df = pro.suspend_d(suspend_type="S", trade_date=td)
        except Exception as e:
            log.warning(f"  suspend_d failed for {td}: {e}")
            continue

        if df is None or df.empty:
            continue

        symbols = [ts_to_symbol(r["ts_code"]) for _, r in df.iterrows()]
        if not symbols:
            continue

        td_parsed = _parse_date(td)
        cur = conn.cursor()
        cur.execute(
            """UPDATE pams_ohlcv_daily SET is_suspended = TRUE
               WHERE market = 'CN_A' AND trade_date = %s AND symbol = ANY(%s)""",
            (td_parsed, symbols),
        )
        conn.commit()
        rows_total += cur.rowcount
        cur.close()

    log.info(f"  suspend: {rows_total} rows marked")
    return rows_total


# ---------- Data version ----------


def _write_data_version(conn, as_of: date, elapsed: float, notes: str = ""):
    """Write/update pams_data_version."""
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM pams_ohlcv_daily WHERE market = 'CN_A'")
    rows_ohlcv = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM pit_index_daily")
    rows_index = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM pit_security_master")
    rows_security = cur.fetchone()[0]

    cur.execute("SELECT MAX(trade_date) FROM pams_ohlcv_daily WHERE market = 'CN_A'")
    max_td = cur.fetchone()[0]

    # Simple checksum from counts
    raw = f"{rows_ohlcv}:{rows_index}:{rows_security}:{max_td}"
    checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]

    cur.execute(
        """INSERT INTO pams_data_version
               (as_of_date, rows_ohlcv, rows_index, rows_security, max_trade_date,
                checksum, ingest_seconds, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (as_of_date) DO UPDATE SET
               rows_ohlcv = EXCLUDED.rows_ohlcv,
               rows_index = EXCLUDED.rows_index,
               rows_security = EXCLUDED.rows_security,
               max_trade_date = EXCLUDED.max_trade_date,
               checksum = EXCLUDED.checksum,
               ingest_seconds = EXCLUDED.ingest_seconds,
               notes = EXCLUDED.notes""",
        (as_of, rows_ohlcv, rows_index, rows_security, max_td, checksum, elapsed, notes),
    )
    conn.commit()
    cur.close()
    log.info(
        f"  data_version: ohlcv={rows_ohlcv}, index={rows_index}, "
        f"security={rows_security}, max_date={max_td}, checksum={checksum}"
    )


# ---------- Backfill orchestrator ----------


def run_backfill(start: str, end: str, skip_limit: bool = False):
    """Full historical backfill from Tushare."""
    t0 = time.time()
    pro = _init_pro()
    conn = _get_conn()

    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")

    # Step 1: Security master (may rate-limit at 120 points: 1/hour)
    n_sec = 0
    n_sec_delisted = 0
    try:
        n_sec = ingest_security_master(pro, conn)
        try:
            n_sec_delisted = ingest_security_master_delisted(pro, conn)
        except Exception as e:
            log.warning(f"  security_master(delisted) failed: {e}")
    except Exception as e:
        log.warning(f"  security_master failed (rate limit?): {e}")
        # Check if we already have data
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pit_security_master")
        existing = cur.fetchone()[0]
        cur.close()
        if existing > 0:
            log.info(f"  Using existing security_master ({existing} rows)")
            n_sec = existing
        else:
            log.error("  No security_master data and API rate-limited. "
                       "Please wait 1 hour and retry, or manually populate.")
            conn.close()
            return

    # Step 2: Trade calendar (may fail with 120 points, will derive after OHLCV)
    n_cal = ingest_trade_cal(pro, conn)

    # Step 3: OHLCV + adj_factor (all stocks)
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM pit_security_master WHERE market_type = 'CN_A' ORDER BY ts_code")
    all_codes = [r[0] for r in cur.fetchall()]
    cur.close()
    log.info(f"  Total stocks to fetch: {len(all_codes)}")

    n_ohlcv = ingest_ohlcv_batch(pro, conn, all_codes, start_fmt, end_fmt)

    # Step 2b: Derive trade calendar from OHLCV if API failed
    if n_cal == 0:
        n_cal = derive_trade_cal_from_ohlcv(conn)

    # Step 4: Index daily (Tushare → AkShare fallback)
    n_idx = ingest_index_daily(pro, conn, start_fmt, end_fmt)
    n_idx_sync = sync_index_to_ohlcv_daily(conn, start, end)

    # Step 5: ST history
    n_st = ingest_st_history(pro, conn)

    # Step 6: Limit up/down (optional, requires 120+ points)
    n_limit = 0
    if not skip_limit:
        try:
            n_limit = ingest_stk_limit(pro, conn, start, end)
        except Exception as e:
            log.warning(f"  stk_limit skipped (may require higher Tushare points): {e}")

    # Step 7: Suspend
    n_susp = 0
    try:
        n_susp = ingest_suspend(pro, conn, start, end)
    except Exception as e:
        log.warning(f"  suspend_d skipped: {e}")

    elapsed = time.time() - t0

    # Write data version
    _write_data_version(
        conn,
        as_of=date.today(),
        elapsed=elapsed,
        notes=f"backfill {start}~{end}",
    )

    conn.close()

    # Print summary
    log.info("=" * 60)
    log.info(f"Backfill complete in {elapsed:.0f}s ({elapsed/60:.1f}min)")
    log.info(f"  security_master: {n_sec}")
    log.info(f"  sec_delisted:    {n_sec_delisted}")
    log.info(f"  trade_cal:       {n_cal}")
    log.info(f"  ohlcv:           {n_ohlcv}")
    log.info(f"  index_daily:     {n_idx}")
    log.info(f"  index_sync:      {n_idx_sync}")
    log.info(f"  st_history:      {n_st}")
    log.info(f"  stk_limit:       {n_limit}")
    log.info(f"  suspend:         {n_susp}")
    log.info("=" * 60)


# ---------- Daily incremental ----------


def run_daily(trade_date: str):
    """Incremental update for a single trading day."""
    t0 = time.time()
    pro = _init_pro()
    conn = _get_conn()

    td_fmt = trade_date.replace("-", "")

    # Check if trading day
    cur = conn.cursor()
    cur.execute(
        "SELECT is_open FROM pit_trade_cal WHERE exchange = 'SSE' AND cal_date = %s",
        (_parse_date(td_fmt),),
    )
    row = cur.fetchone()
    cur.close()

    if row and not row[0]:
        log.info(f"{trade_date} is not a trading day, skipping.")
        conn.close()
        return

    # Update security master (catches new IPOs).
    # Tushare stock_basic is heavily rate-limited (often 1/hour on low tiers).
    # When throttled, continue daily price ingest instead of failing the whole job.
    try:
        ingest_security_master(pro, conn)
        try:
            ingest_security_master_delisted(pro, conn)
        except Exception as e:
            log.warning("security_master(delisted) skipped: %s", e)
    except Exception as e:
        msg = str(e)
        if (
            "每小时最多访问该接口1次" in msg
            or "每分钟最多访问该接口" in msg
            or "每天最多访问该接口" in msg
        ):
            log.warning("security_master skipped due Tushare rate limit: %s", msg)
        else:
            raise

    # OHLCV for single day: fast all-symbol pull.
    # If source returns 0 rows (e.g. non-trading day / holiday / provider not updated),
    # do not fallback to per-symbol mode, which is too slow and often times out.
    n_day_ohlcv = ingest_ohlcv_single_day(pro, conn, trade_date)
    if n_day_ohlcv == 0:
        log.info("  ohlcv(single-day): no market rows for %s; skip slow fallback.", trade_date)

    # Index daily for single day
    ingest_index_daily(pro, conn, td_fmt, td_fmt)
    sync_index_to_ohlcv_daily(conn, trade_date, trade_date)

    # Limit prices for single day
    try:
        ingest_stk_limit(pro, conn, trade_date, trade_date)
    except Exception as e:
        log.warning(f"  stk_limit skipped: {e}")

    # Suspend for single day
    try:
        ingest_suspend(pro, conn, trade_date, trade_date)
    except Exception as e:
        log.warning(f"  suspend_d skipped: {e}")

    elapsed = time.time() - t0
    _write_data_version(conn, as_of=date.today(), elapsed=elapsed, notes=f"daily {trade_date}")
    conn.close()

    log.info(f"Daily ingest for {trade_date} complete in {elapsed:.0f}s")


# ---------- Status ----------


def run_status():
    """Print current data version and table counts."""
    conn = _get_conn()
    cur = conn.cursor()

    tables = [
        "pit_security_master",
        "pit_trade_cal",
        "pams_ohlcv_daily",
        "pit_index_daily",
        "pit_st_history",
        "pit_industry_history",
        "pams_data_version",
    ]

    print("\n--- PIT Data Status ---\n")
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")  # noqa: S608 — trusted table names
        cnt = cur.fetchone()[0]
        print(f"  {t:30s} {cnt:>12,} rows")

    cur.execute(
        "SELECT as_of_date, max_trade_date, checksum, ingest_seconds, notes "
        "FROM pams_data_version ORDER BY as_of_date DESC LIMIT 3"
    )
    rows = cur.fetchall()
    if rows:
        print("\n--- Recent Versions ---\n")
        for r in rows:
            print(f"  {r[0]} | max_date={r[1]} | cksum={r[2]} | {r[3]:.0f}s | {r[4]}")

    cur.close()
    conn.close()
    print()


# ---------- Helpers ----------


def _parse_date(val) -> Optional[date]:
    """Parse Tushare date string (YYYYMMDD or YYYY-MM-DD) to date."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).replace("-", "")
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


# ---------- CLI ----------


def main():
    parser = argparse.ArgumentParser(
        description="Tushare PIT Data Ingest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # backfill
    bf = sub.add_parser("backfill", help="Full historical backfill")
    bf.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    bf.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    bf.add_argument("--skip-limit", action="store_true", help="Skip stk_limit (requires 120+ points)")

    # daily
    dy = sub.add_parser("daily", help="Single-day incremental update")
    dy.add_argument("--date", required=True, help="Trade date (YYYY-MM-DD)")

    # status
    sub.add_parser("status", help="Print data status")

    args = parser.parse_args()

    if args.command == "backfill":
        run_backfill(args.start, args.end, skip_limit=args.skip_limit)
    elif args.command == "daily":
        run_daily(args.date)
    elif args.command == "status":
        run_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
