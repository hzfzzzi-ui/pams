"""IBKR read-only integration endpoints."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from collectors.ibkr_flex import (
    IBKRFlexError,
    ensure_ibkr_trade_table,
    ensure_ibkr_position_history_table,
    latest_sync_status,
    sync_ibkr_flex_to_db,
)
from services.investment_trade_fact import backfill_investment_trade_facts

router = APIRouter(prefix="/ibkr", tags=["ibkr"])
ROOT = Path(__file__).resolve().parents[3]


class IBKRFlexSyncRequest(BaseModel):
    """Options for a manual IBKR Flex sync trigger."""

    as_of: str | None = None
    align_to_latest_snapshot: bool = False


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: _norm(v) for k, v in row.items()}


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception:
        return default


def _ib_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _latest_flex_xml_path() -> Path | None:
    data_dir = Path(os.environ.get("PAMS_DATA_DIR") or (ROOT / "data"))
    raw_dir = data_dir / "raw" / "ibkr"
    if not raw_dir.exists():
        return None
    files = [p for p in raw_dir.glob("*.xml") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _latest_usd_cny_rate() -> Decimal | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                  AND quote_currency = 'USD'
                ORDER BY as_of DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    return _dec(row[0]) if row else None


def _parse_flex_performance(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()

    cash_rows = [
        elem.attrib
        for elem in root.iter("CashReportCurrency")
        if elem.attrib.get("currency") == "BASE_SUMMARY"
    ]
    if not cash_rows:
        raise IBKRFlexError("IBKR Flex report has no BASE_SUMMARY cash report")

    cash_rows.sort(key=lambda row: row.get("toDate", ""))
    latest_cash = cash_rows[-1]
    report_date = latest_cash.get("toDate")

    positions = [
        elem.attrib
        for elem in root.iter("OpenPosition")
        if not report_date or elem.attrib.get("reportDate") == report_date
    ]
    if not positions:
        latest_position_date = max((elem.attrib.get("reportDate", "") for elem in root.iter("OpenPosition")), default="")
        report_date = latest_position_date or report_date
        positions = [
            elem.attrib
            for elem in root.iter("OpenPosition")
            if elem.attrib.get("reportDate") == report_date
        ]

    position_value_base = sum(
        _dec(row.get("positionValue")) * _dec(row.get("fxRateToBase"), Decimal("1"))
        for row in positions
    )
    gross_exposure_base = sum(
        abs(_dec(row.get("positionValue")) * _dec(row.get("fxRateToBase"), Decimal("1")))
        for row in positions
    )
    unrealized_pnl_base = sum(
        _dec(row.get("fifoPnlUnrealized")) * _dec(row.get("fxRateToBase"), Decimal("1"))
        for row in positions
    )

    ending_cash_base = _dec(latest_cash.get("endingCash"))
    net_liquidation_base = position_value_base + ending_cash_base
    net_deposit_base = _dec(latest_cash.get("depositWithdrawalsYTD"))
    total_pnl_base = net_liquidation_base - net_deposit_base

    realized_pnl_base = Decimal("0")
    for elem in root.iter("Trade"):
        row = elem.attrib
        if report_date and row.get("tradeDate", "") > report_date:
            continue
        realized_pnl_base += _dec(row.get("fifoPnlRealized")) * _dec(row.get("fxRateToBase"), Decimal("1"))

    other_pnl_base = total_pnl_base - unrealized_pnl_base - realized_pnl_base
    leverage_ratio = gross_exposure_base / net_liquidation_base if net_liquidation_base > 0 else Decimal("0")
    return_ratio = total_pnl_base / net_deposit_base if net_deposit_base > 0 else Decimal("0")
    negative_cash_ratio = abs(ending_cash_base) / net_liquidation_base if ending_cash_base < 0 and net_liquidation_base > 0 else Decimal("0")

    usd_cny = _latest_usd_cny_rate()
    payload: dict[str, Any] = {
        "status": "ok",
        "as_of": _ib_date(report_date),
        "base_currency": "USD",
        "source_file": path.name,
        "net_deposit_base": net_deposit_base,
        "net_liquidation_base": net_liquidation_base,
        "total_pnl_base": total_pnl_base,
        "return_ratio": return_ratio,
        "gross_exposure_base": gross_exposure_base,
        "leverage_ratio": leverage_ratio,
        "negative_cash_base": abs(ending_cash_base) if ending_cash_base < 0 else Decimal("0"),
        "negative_cash_ratio": negative_cash_ratio,
        "cash_base": ending_cash_base,
        "position_value_base": position_value_base,
        "unrealized_pnl_base": unrealized_pnl_base,
        "realized_pnl_base": realized_pnl_base,
        "other_pnl_base": other_pnl_base,
        "position_count": len(positions),
    }
    if usd_cny is not None:
        payload["fx_rate_to_cny"] = usd_cny
        for key in (
            "net_deposit",
            "net_liquidation",
            "total_pnl",
            "gross_exposure",
            "negative_cash",
            "cash",
            "position_value",
            "unrealized_pnl",
            "realized_pnl",
            "other_pnl",
        ):
            payload[f"{key}_cny"] = payload[f"{key}_base"] * usd_cny
    return {k: _norm(v) for k, v in payload.items()}


def _credentials() -> tuple[str | None, str | None]:
    token = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = (
        os.environ.get("IBKR_FLEX_POSITIONS_QUERY_ID")
        or os.environ.get("IBKR_FLEX_OPEN_POSITIONS_QUERY_ID")
        or os.environ.get("IBKR_FLEX_QUERY_ID")
    )
    return token, query_id


@router.get("/flex/status")
def ibkr_flex_status() -> dict[str, Any]:
    """Return the latest local IBKR Flex sync status without exposing secrets."""
    token, query_id = _credentials()
    with get_conn() as conn:
        latest = latest_sync_status(conn)
    return {
        "source": "ibkr_flex",
        "configured": bool(token and query_id),
        "has_token": bool(token),
        "has_query_id": bool(query_id),
        "latest": _record(latest),
    }


@router.get("/performance")
def ibkr_performance() -> dict[str, Any]:
    """Return IBKR account performance from Flex account NAV and net deposits."""
    path = _latest_flex_xml_path()
    if path is None:
        raise HTTPException(status_code=404, detail="No local IBKR Flex XML report is available")
    try:
        return _parse_flex_performance(path)
    except IBKRFlexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/flex/sync")
def ibkr_flex_sync(req: IBKRFlexSyncRequest) -> dict[str, Any]:
    """Run a manual read-only IBKR Flex sync into `pams_holdings_snapshot`."""
    token, query_id = _credentials()
    if not token or not query_id:
        raise HTTPException(
            status_code=400,
            detail="Missing IBKR_FLEX_TOKEN and IBKR_FLEX_POSITIONS_QUERY_ID/IBKR_FLEX_QUERY_ID",
        )

    try:
        with get_conn() as conn:
            result = sync_ibkr_flex_to_db(
                conn,
                token=token,
                query_id=query_id,
                requested_as_of=req.as_of,
                align_to_latest_snapshot=req.align_to_latest_snapshot,
            )
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                result["trade_fact_backfill"] = backfill_investment_trade_facts(cur)
            conn.commit()
            return result
    except IBKRFlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/trades")
def ibkr_trades(limit: int = 200) -> dict[str, Any]:
    """Return locally imported IBKR trade records."""
    limit = max(1, min(limit, 5000))
    with get_conn() as conn:
        ensure_ibkr_trade_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT account_code, source_account_id, trade_date, symbol, name, currency,
                       side, quantity, price, gross_amount, commission, net_cash,
                       realized_pnl, trade_id, transaction_id, order_id
                FROM pams_ibkr_trades
                ORDER BY trade_date DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return {"rows": [_record(dict(row)) for row in rows]}


@router.get("/position-history")
def ibkr_position_history(symbol: str | None = None, limit: int = 500) -> dict[str, Any]:
    """Return locally imported IBKR historical position snapshots."""
    limit = max(1, min(limit, 10000))
    where = ["account_code = 'ibkr'"]
    params: list[Any] = []
    if symbol:
        where.append("symbol = %s")
        params.append(symbol.upper())
    params.append(limit)
    with get_conn() as conn:
        ensure_ibkr_position_history_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT account_code, source_account_id, as_of, symbol, name, currency,
                       major_category, minor_category, quantity, buy_price,
                       current_price, market_value, cost, holding_pnl
                FROM pams_ibkr_position_history
                WHERE {' AND '.join(where)}
                ORDER BY as_of DESC, market_value DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return {"rows": [_record(dict(row)) for row in rows]}


@router.get("/reconcile")
def ibkr_reconcile() -> dict[str, Any]:
    """Compare latest IBKR holdings symbols with imported IBKR trade symbols."""
    with get_conn() as conn:
        ensure_ibkr_trade_table(conn)
        ensure_ibkr_position_history_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT MAX(as_of) AS as_of
                    FROM pams_holdings_snapshot
                    WHERE account_code = 'ibkr'
                )
                SELECT h.as_of, h.symbol, h.name, h.quantity, h.market_value
                FROM pams_holdings_snapshot h
                JOIN latest l ON l.as_of = h.as_of
                WHERE h.account_code = 'ibkr'
                  AND h.major_category = '美股'
                ORDER BY h.symbol
                """
            )
            holdings = cur.fetchall()
            cur.execute(
                """
                SELECT symbol,
                       MIN(trade_date) AS first_trade_date,
                       MAX(trade_date) AS last_trade_date,
                       COUNT(*) AS trade_count,
                       SUM(CASE WHEN side = 'BUY' THEN quantity
                                WHEN side = 'SELL' THEN -quantity
                                ELSE quantity END) AS signed_quantity
                FROM pams_ibkr_trades
                WHERE account_code = 'ibkr'
                GROUP BY symbol
                ORDER BY symbol
                """
            )
            trades = cur.fetchall()
            cur.execute(
                """
                SELECT symbol,
                       MIN(as_of) AS first_position_date,
                       MAX(as_of) AS last_position_date,
                       COUNT(*) AS position_snapshot_count
                FROM pams_ibkr_position_history
                WHERE account_code = 'ibkr'
                  AND major_category = '美股'
                GROUP BY symbol
                ORDER BY symbol
                """
            )
            position_history = cur.fetchall()

    trade_by_symbol = {row["symbol"]: row for row in trades}
    history_by_symbol = {row["symbol"]: row for row in position_history}
    holding_symbols = {row["symbol"] for row in holdings}
    trade_symbols = set(trade_by_symbol)
    history_symbols = set(history_by_symbol)
    matched = sorted(holding_symbols & trade_symbols)
    missing_trade_history = sorted(holding_symbols - trade_symbols)
    closed_or_nonheld_traded = sorted(trade_symbols - holding_symbols)

    return {
        "latest_holding_as_of": _record(dict(holdings[0]))["as_of"] if holdings else None,
        "holding_symbol_count": len(holding_symbols),
        "trade_symbol_count": len(trade_symbols),
        "position_history_symbol_count": len(history_symbols),
        "matched_symbols": matched,
        "missing_trade_history_symbols": missing_trade_history,
        "closed_or_nonheld_traded_symbols": closed_or_nonheld_traded,
        "trades_by_symbol": [_record(dict(row)) for row in trades],
        "position_history_by_symbol": [_record(dict(row)) for row in position_history],
    }
