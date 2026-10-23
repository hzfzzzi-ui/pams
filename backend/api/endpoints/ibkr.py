"""IBKR read-only integration endpoints."""
from __future__ import annotations

import os
from datetime import date
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
from services.ibkr_performance import (
    read_published_performance,
    published_report_status,
    get_conn,
    _norm,
    _dec as _dec,
    _ib_date as _ib_date,
    _latest_flex_xml_path as _latest_flex_xml_path,
    _latest_usd_cny_rate as _latest_usd_cny_rate,
    _parse_flex_performance as _parse_flex_performance,
)
from services.investment_trade_fact import backfill_investment_trade_facts
from services.ibkr_reconciliation import read_ibkr_reconciliation

router = APIRouter(prefix="/ibkr", tags=["ibkr"])


class IBKRFlexSyncRequest(BaseModel):
    """Options for a manual IBKR Flex sync trigger."""

    as_of: str | None = None
    align_to_latest_snapshot: bool = False
    expected_statement_reference: date | None = None


def _record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: _norm(v) for k, v in row.items()}


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
        quality = published_report_status(conn)
    return {
        "source": "ibkr_flex",
        "configured": bool(token and query_id),
        "has_token": bool(token),
        "has_query_id": bool(query_id),
        "latest": _record(latest),
        "report_quality": quality,
    }


@router.get("/reconciliation")
def ibkr_reconciliation() -> dict[str, Any]:
    """Inspect published evidence and existing ledgers without any refresh or repair."""
    return read_ibkr_reconciliation()


@router.get("/performance")
def ibkr_performance() -> dict[str, Any]:
    """Return IBKR account performance from Flex account NAV and net deposits."""
    try:
        result = read_published_performance()
        if result is None:
            raise HTTPException(status_code=404, detail="No published IBKR Flex report is available")
        return result
    except HTTPException:
        raise
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
            trade_fact_backfill: dict[str, int] = {}

            def backfill_facts(sync_conn: Any) -> None:
                with sync_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    trade_fact_backfill.update(backfill_investment_trade_facts(cur))

            result = sync_ibkr_flex_to_db(
                conn,
                token=token,
                query_id=query_id,
                requested_as_of=req.as_of,
                align_to_latest_snapshot=req.align_to_latest_snapshot,
                after_write=backfill_facts,
                expected_statement_reference=req.expected_statement_reference,
            )
            result["trade_fact_backfill"] = trade_fact_backfill
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
