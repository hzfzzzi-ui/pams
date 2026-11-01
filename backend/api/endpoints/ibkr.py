"""Official report ingestion and read-only diagnostics; no broker order capability."""
from __future__ import annotations

import os
from contextlib import closing
from datetime import date
from typing import Any
from uuid import UUID

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from collectors.ibkr_flex import (
    IBKRFlexError,
    latest_sync_status,
    sync_ibkr_flex_to_db,
)
from domain.brokers.ibkr.ibkr_performance import (
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
from domain.brokers.ibkr.ibkr_reconciliation import read_ibkr_reconciliation
from domain.brokers.ibkr.ibkr_account_audit_read import read_ibkr_account_audit
from domain.brokers.ibkr.ibkr_daily_close_sync import daily_close_status, sync_daily_close
from domain.brokers.ibkr import ibkr_cash_event_sync

router = APIRouter(prefix="/ibkr", tags=["ibkr"])


class HoldingReviewStage(BaseModel):
    source_account_id: str = Field(min_length=1, max_length=120)
    business_date: date
    xml_text: str = Field(min_length=1, max_length=32 * 1024 * 1024)


class HoldingReviewDecision(BaseModel):
    item_id: UUID
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    scope_complete: bool = False


class HoldingIdentityDecision(BaseModel):
    item_id: UUID
    holding_id: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)


class HoldingAccountBindingDecision(BaseModel):
    source_account_id: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)


class HoldingAccountScopeDecision(BaseModel):
    expected_revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    confirmed: bool = False


class HoldingReviewPublish(BaseModel):
    expected_revision: int = Field(ge=1)


def _holding_review_call(request: Request, response: Response, action):
    """Strict same-origin + explicitly designated disposable DB; never default DSN."""
    import re
    from psycopg2.extensions import parse_dsn
    from domain.imports import isolation
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    origin=request.headers.get('origin')
    expected=f'{request.url.scheme}://{request.url.netloc}'
    if (origin != expected or request.headers.get('sec-fetch-site') == 'cross-site' or
        request.headers.get('x-pams-isolated-review') != '1'):
        raise HTTPException(status_code=403,detail='Same-origin isolated review required')
    try:
        mode = isolation.source_mode()
        if mode is None:
            raise HTTPException(status_code=404,detail='Isolated review unavailable')
        if mode == 'development':
            conn = get_conn()
        else:
            dsn=os.environ.get('PAMS_HOLDING_REVIEW_TEST_DSN','')
            params=parse_dsn(dsn)
            allowed={'host','port','dbname','user','password','connect_timeout','sslmode'}
            if (not dsn or set(params)-allowed or params.get('host') not in {'127.0.0.1','localhost','::1'} or
                not re.fullmatch(r'pams_import_test_[0-9a-f]{32}',params.get('dbname',''))):
                raise ValueError('invalid disposable target')
            conn = psycopg2.connect(**params)
        with closing(conn) as conn:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    if not isolation.source_enabled(cur):
                        raise ValueError('disposable source gate unavailable')
                    return action(cur)
    except (ValueError, psycopg2.Error) as exc:
        raise HTTPException(status_code=409,detail='Holding review evidence or revision conflict') from exc


@router.post('/holding-review/stage')
def holding_review_stage(req: HoldingReviewStage, request: Request, response: Response):
    from uuid import uuid4
    from domain.imports.ibkr_holding_source import stage_report
    return _holding_review_call(request,response,lambda cur: stage_report(cur,batch_id=uuid4(),account_code='ibkr',
        source_account_id=req.source_account_id,business_date=req.business_date,content=req.xml_text.encode('utf-8')))


@router.get('/holding-review/{batch_id}')
def holding_review_inspect(batch_id: UUID, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import inspect
    return _holding_review_call(request,response,lambda cur: inspect(cur,batch_id))


@router.post('/holding-review/{batch_id}/review')
def holding_review_decision(batch_id: UUID, req: HoldingReviewDecision, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import review
    return _holding_review_call(request,response,lambda cur: review(cur,batch_id=batch_id,item_id=req.item_id,
        expected_revision=req.expected_revision,note=req.note,scope_complete=req.scope_complete))


@router.post('/holding-review/{batch_id}/identity')
def holding_review_identity(batch_id: UUID, req: HoldingIdentityDecision, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import review_identity
    return _holding_review_call(request,response,lambda cur: review_identity(cur,batch_id=batch_id,item_id=req.item_id,
        holding_id=req.holding_id,expected_revision=req.expected_revision,note=req.note))


@router.post('/holding-review/{batch_id}/account-binding')
def holding_review_account_binding(batch_id: UUID, req: HoldingAccountBindingDecision, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import review_account_binding
    return _holding_review_call(request,response,lambda cur: review_account_binding(cur,batch_id=batch_id,
        source_account_id=req.source_account_id,expected_revision=req.expected_revision,note=req.note))


@router.post('/holding-review/{batch_id}/account-scope')
def holding_review_account_scope(batch_id: UUID, req: HoldingAccountScopeDecision, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import review_account_scope
    return _holding_review_call(request,response,lambda cur: review_account_scope(cur,batch_id=batch_id,
        expected_revision=req.expected_revision,note=req.note,confirmed=req.confirmed))


@router.post('/holding-review/{batch_id}/publish')
def holding_review_publish(batch_id: UUID, req: HoldingReviewPublish, request: Request, response: Response):
    from domain.brokers.ibkr.ibkr_holding_review import publish
    return _holding_review_call(request,response,lambda cur: publish(cur,batch_id=batch_id,expected_revision=req.expected_revision))


class IBKRRetirementSource(BaseModel):
    batch_id: UUID
    item_id: UUID
    identity_batch_id: UUID | None = None
    identity_item_id: UUID | None = None


class IBKRFlexSyncRequest(BaseModel):
    """Options for a manual IBKR Flex sync trigger."""

    as_of: str | None = None
    align_to_latest_snapshot: bool = False
    expected_statement_reference: date | None = None
    holding_retirement_sources: dict[str, IBKRRetirementSource] | None = None


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
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
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


@router.get("/daily-close/status")
def ibkr_daily_close_status() -> dict[str, Any]:
    return daily_close_status()


@router.post("/daily-close/sync")
def ibkr_daily_close_sync(req: IBKRFlexSyncRequest) -> dict[str, Any]:
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    if req.as_of is not None or req.align_to_latest_snapshot:
        raise HTTPException(status_code=422, detail="Daily close cannot relabel report dates")
    return sync_daily_close(expected_statement_reference=req.expected_statement_reference)


@router.get("/cash-events/status")
def ibkr_cash_event_status() -> dict[str, Any]:
    with closing(get_conn()) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn:
            return ibkr_cash_event_sync.status(conn)


@router.post("/cash-events/sync")
def ibkr_cash_event_sync_run(req: IBKRFlexSyncRequest) -> dict[str, Any]:
    if req.as_of is not None or req.align_to_latest_snapshot or req.expected_statement_reference is not None:
        raise HTTPException(status_code=422, detail="Cash events use published evidence and the configured cutoff")
    try:
        with closing(get_conn()) as conn:
            return ibkr_cash_event_sync.sync(conn)
    except Exception as exc:
        # Do not return source attributes, database credentials, or provider errors.
        raise HTTPException(status_code=503, detail="Cash event sync failed; previous facts retained") from exc


@router.get("/account-audit")
def ibkr_account_audit(period_start: date | None = None) -> dict[str, Any]:
    """Audit published account evidence without refreshing or changing any ledger."""
    return read_ibkr_account_audit(period_start=period_start)


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
    """Import official reports into local ledgers within one transaction."""
    from domain.imports.isolation import source_mode
    from fastapi import HTTPException
    if source_mode() == "development":
        raise HTTPException(status_code=409, detail="External synchronization is disabled in development mode")
    token, query_id = _credentials()
    if not token or not query_id:
        raise HTTPException(
            status_code=400,
            detail="Missing IBKR_FLEX_TOKEN and IBKR_FLEX_POSITIONS_QUERY_ID/IBKR_FLEX_QUERY_ID",
        )

    try:
        with get_conn() as conn:
            isolated_options = ({'holding_retirement_sources': {name: source.model_dump(mode='json')
                                for name, source in req.holding_retirement_sources.items()}}
                                if req.holding_retirement_sources is not None else {})
            result = sync_ibkr_flex_to_db(
                conn,
                token=token,
                query_id=query_id,
                requested_as_of=req.as_of,
                align_to_latest_snapshot=req.align_to_latest_snapshot,
                **isolated_options,
                expected_statement_reference=req.expected_statement_reference,
            )
            result["trade_fact_backfill"] = {'status': 'not_run', 'reason': 'unified_report_only'}
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS facts")
            if not cur.fetchone()['facts']:
                return {'rows': [], 'data_status': 'unavailable'}
            cur.execute("""SELECT account_code,raw_json #>> '{source_row,raw_json,accountId}' AS source_account_id,
                trade_date,symbol,name,currency,UPPER(side) AS side,quantity,price,gross_amount,
                CASE WHEN raw_json #>> '{source_row,raw_json,ibCommission}' ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                     THEN (raw_json #>> '{source_row,raw_json,ibCommission}')::numeric ELSE NULL END AS commission,
                net_cash,realized_pnl,raw_json #>> '{source_row,raw_json,tradeID}' AS trade_id,
                raw_json #>> '{source_row,raw_json,transactionID}' AS transaction_id,
                raw_json #>> '{source_row,raw_json,ibOrderID}' AS order_id,trade_id AS sort_id
                FROM pams_investment_trade_fact WHERE account_code='ibkr' AND source_table='ibkr_flex_trade'
                ORDER BY trade_date DESC,sort_id DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.pop('sort_id', None)
        result.append(_record(item))
    return {'rows': result}


@router.get("/position-history")
def ibkr_position_history(symbol: str | None = None, limit: int = 500) -> dict[str, Any]:
    """Return locally imported IBKR historical position snapshots."""
    limit = max(1, min(limit, 10000))
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.valuation_evidence') IS NOT NULL AS versions, "
                        "to_regclass('public.valuation_evidence') IS NOT NULL AS evidence")
            available = cur.fetchone()
            if not available['versions'] or not available['evidence']:
                return {'rows': [], 'data_status': 'unavailable'}
            from domain.investments.valuation_input_evidence import verify_recorded_version
            cur.execute("""SELECT DISTINCT ON (v.business_date,v.scope_key)
                v.version_key,v.business_date,e.evidence->'snapshot'->'content' AS content
                FROM valuation_evidence v
                CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS e(input_kind text,input_hash text,evidence jsonb)
                WHERE v.result_kind='holding' AND v.account_code='ibkr'
                  AND e.input_kind='holding'
                  AND (%s::text IS NULL OR UPPER(e.evidence->'snapshot'->'content'->>'symbol')=%s)
                ORDER BY v.business_date DESC,v.scope_key,v.created_at DESC,v.version_key DESC LIMIT %s""",
                (symbol.upper() if symbol else None, symbol.upper() if symbol else None, limit))
            captured = cur.fetchall()
            rows = []
            for candidate in captured:
                content = candidate['content']
                if not isinstance(content, dict) or not verify_recorded_version(cur, candidate['version_key']):
                    continue
                fields = ('account_code','as_of','symbol','name','currency','major_category','minor_category',
                          'quantity','buy_price','current_price','market_value','cost','holding_pnl')
                rows.append(_record({'source_account_id': None, **{field: content.get(field) for field in fields}}))
            return {'rows': rows, 'data_status': 'recorded_only'}


@router.get("/reconcile")
def ibkr_reconcile() -> dict[str, Any]:
    """Compare latest IBKR holdings symbols with imported IBKR trade symbols."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS facts")
            available = cur.fetchone()
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
            trade_sources = []
            if available['facts']:
                trade_sources.append("""SELECT symbol,trade_date,CASE WHEN side='buy' THEN ABS(quantity)
                    WHEN side='sell' THEN -ABS(quantity) ELSE quantity END AS signed_quantity
                    FROM pams_investment_trade_fact WHERE account_code='ibkr' AND source_table='ibkr_flex_trade'""")
            trades = []
            if trade_sources:
                cur.execute('SELECT symbol,MIN(trade_date) AS first_trade_date,MAX(trade_date) AS last_trade_date, '
                            'COUNT(*) AS trade_count,SUM(signed_quantity) AS signed_quantity FROM (' +
                            ' UNION ALL '.join(trade_sources) + ') AS trades GROUP BY symbol ORDER BY symbol')
                trades = cur.fetchall()
            position_history: list[dict[str, Any]] = []

    trade_by_symbol = {row["symbol"]: row for row in trades}
    holding_symbols = {row["symbol"] for row in holdings}
    trade_symbols = set(trade_by_symbol)
    matched = sorted(holding_symbols & trade_symbols)
    missing_trade_history = sorted(holding_symbols - trade_symbols)
    closed_or_nonheld_traded = sorted(trade_symbols - holding_symbols)

    return {
        "latest_holding_as_of": _norm(holdings[0]["as_of"]) if holdings else None,
        "holding_symbol_count": len(holding_symbols),
        "trade_symbol_count": len(trade_symbols),
        "position_history_symbol_count": None,
        "position_history_status": 'unavailable',
        "trade_history_status": 'available' if trade_sources else 'unavailable',
        "matched_symbols": matched,
        "missing_trade_history_symbols": missing_trade_history if trade_sources else None,
        "closed_or_nonheld_traded_symbols": closed_or_nonheld_traded,
        "trades_by_symbol": [_record(dict(row)) for row in trades],
        "position_history_by_symbol": [_record(dict(row)) for row in position_history],
    }
