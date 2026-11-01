"""Bank statement reconciliation staging API."""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Literal
from uuid import UUID

import psycopg2
import psycopg2.extras
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from collectors.bank_statement_parser import ParsedStatement, parse_statement
from domain.imports.isolation import source_enabled
from domain.investments.holding_evidence_writer import finish_holding_change, lock_holding_accounts, prepare_holding_change
from domain.ledger.bank_statement_rules import (
    _classify as _classify, _classify_by_amount as _classify_by_amount,
    _classify_statement_row,
)

router = APIRouter()

CONFIG_PATH = Path(os.environ.get("PAMS_BANK_RECONCILE_CONFIG", "/app/config/bank_reconcile.yaml"))
LOCAL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "bank_reconcile.yaml"
HOST_DOWNLOADS_DIR = Path(os.environ.get("PAMS_HOST_DOWNLOADS_DIR", "/host/Downloads"))
HOST_ACCOUNT_LEDGER_DIR = Path(os.environ.get("PAMS_ACCOUNT_LEDGER_DIR", "/host/account-ledger"))
LIABILITY_ACCOUNT_KINDS = {"credit_card"}
INTERNAL_TRANSFER_CATEGORY = "内部划转"
INTERNAL_TRANSFER_SUBCATEGORY = "投资账户转账"
BANK_ACCOUNT_TRANSFER_SUBCATEGORY = "银行账户转账"
CREDIT_CARD_PAYMENT_SUBCATEGORY = "信用卡还款"
PAYMENT_PROXY_CATEGORY = "支付通道"
PAYMENT_PROXY_SUBCATEGORY = "银行卡扣款映射"
BENEFIT_INCOME_CATEGORY = "收入"
BENEFIT_INCOME_SUBCATEGORY = "医保缴费"


def get_conn():
    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _net_asset_balance(value: Any, account_kind: str | None) -> Decimal | None:
    """Convert a raw statement balance to the net-asset sign used by PAMS summaries."""
    if value is None or value == "":
        return None
    amount = Decimal(str(value))
    if account_kind in LIABILITY_ACCOUNT_KINDS:
        return -abs(amount)
    return amount


def _statement_snapshot_payload(result: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    """Build the holdings snapshot row implied by the latest statement balance."""
    target = profile.get("pams_target") or {}
    snapshot = target.get("snapshot") or {}
    latest_balance = result.get("latest_balance")
    latest_balance_date = result.get("latest_balance_date") or result.get("statement_end")
    if not snapshot or latest_balance in (None, "") or latest_balance_date is None:
        return None

    net_balance = _net_asset_balance(
        latest_balance,
        result.get("account_kind") or profile.get("account_kind"),
    )
    if net_balance is None:
        return None

    asset_liability = str(snapshot.get("asset_liability") or ("负债" if net_balance < 0 else "资产"))
    market_value = abs(net_balance) if asset_liability == "负债" else net_balance
    rows = result.get("rows") or []
    row_currency = rows[-1].get("currency") if rows and isinstance(rows[-1], dict) else None
    currency = str(row_currency or profile.get("currency") or result.get("currency") or "CNY").upper()

    return {
        "account_code": str(target.get("account_code") or result.get("account_code") or profile.get("account_code")),
        "display_name": str(target.get("display_name") or result.get("account_code") or profile.get("account_code")),
        "is_asset": bool(target.get("is_asset", asset_liability != "负债")),
        "symbol": snapshot.get("symbol"),
        "name": str(snapshot.get("name") or target.get("display_name") or result.get("account_code")),
        "asset_liability": asset_liability,
        "currency": currency,
        "major_category": str(snapshot.get("major_category") or "现金"),
        "minor_category": str(snapshot.get("minor_category") or "现金/活期"),
        "market_value": market_value,
        "cost": market_value,
        "as_of": latest_balance_date,
    }


def _sync_holding_snapshot_from_statement(cur, result: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    """Upsert the account-level holdings snapshot selected by bank reconciliation."""
    payload = _statement_snapshot_payload(result, profile)
    if payload is None:
        return None
    lock_holding_accounts(cur, [payload['account_code']])
    isolated = source_enabled(cur)

    cur.execute(
        """
        INSERT INTO pams_accounts (account_code, display_name, base_currency, is_asset, active)
        VALUES (%s, %s, %s, %s, true)
        ON CONFLICT (account_code) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            base_currency = EXCLUDED.base_currency,
            is_asset = EXCLUDED.is_asset,
            active = true
        """,
        (
            payload["account_code"],
            payload["display_name"],
            payload["currency"],
            payload["is_asset"],
        ),
    )
    if _table_exists(cur, "pams_asset_categories"):
        cur.execute(
            """
            INSERT INTO pams_asset_categories (major_category, minor_category)
            VALUES (%s, %s)
            ON CONFLICT (major_category, minor_category) DO NOTHING
            """,
            (payload["major_category"], payload["minor_category"]),
        )
    if isolated:
        prepare_holding_change(cur, account_code=payload['account_code'], name=payload['name'],
                               as_of=payload['as_of'], source_item_id=None)
    cur.execute(
        """
        INSERT INTO pams_holdings_snapshot (
            symbol, name, asset_liability, currency, account_code,
            major_category, minor_category, market_value, cost, as_of
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_code, name, as_of) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            asset_liability = EXCLUDED.asset_liability,
            currency = EXCLUDED.currency,
            major_category = EXCLUDED.major_category,
            minor_category = EXCLUDED.minor_category,
            market_value = EXCLUDED.market_value,
            cost = EXCLUDED.cost,
            updated_at = NOW()
        """ + (""" WHERE (pams_holdings_snapshot.symbol,pams_holdings_snapshot.asset_liability,
            pams_holdings_snapshot.currency,pams_holdings_snapshot.major_category,pams_holdings_snapshot.minor_category,
            pams_holdings_snapshot.market_value,pams_holdings_snapshot.cost)
            IS DISTINCT FROM (EXCLUDED.symbol,EXCLUDED.asset_liability,EXCLUDED.currency,EXCLUDED.major_category,
            EXCLUDED.minor_category,EXCLUDED.market_value,EXCLUDED.cost) RETURNING id""" if isolated else ""),
        (
            payload["symbol"],
            payload["name"],
            payload["asset_liability"],
            payload["currency"],
            payload["account_code"],
            payload["major_category"],
            payload["minor_category"],
            payload["market_value"],
            payload["cost"],
            payload["as_of"],
        ),
    )
    if isolated:
        row = cur.fetchone()
        if row:
            identifier = row['id'] if isinstance(row, dict) else row[0]
            finish_holding_change(cur, holding_id=identifier, source_item_id=None,
                                  input_refs=[{'kind':'source_item', 'key':None, 'role':'bank_statement'}])
    return payload


def _load_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else LOCAL_CONFIG_PATH
    if not path.exists():
        return {"defaults": {"since": "2026-01-01"}, "profiles": {}, "rules": []}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_date(value: Optional[str], fallback: Optional[str] = None) -> date | None:
    raw = value or fallback
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    return datetime.strptime(str(raw), "%Y-%m-%d").date()


def _resolve_statement_path(file_path: str) -> Path:
    path = Path(file_path)
    if path.exists():
        return path
    normalized = file_path.replace("\\", "/")
    marker = "/Downloads/"
    if marker in normalized and HOST_DOWNLOADS_DIR.exists():
        candidate = HOST_DOWNLOADS_DIR / normalized.split(marker, 1)[1]
        if candidate.exists():
            return candidate
    ledger_marker = "/账户台账/"
    if ledger_marker in normalized and HOST_ACCOUNT_LEDGER_DIR.exists():
        candidate = HOST_ACCOUNT_LEDGER_DIR / normalized.split(ledger_marker, 1)[1]
        if candidate.exists():
            return candidate
    return path


def _profile(config: dict[str, Any], code: str) -> dict[str, Any]:
    profiles = config.get("profiles", {}) or {}
    if code not in profiles:
        raise HTTPException(status_code=404, detail=f"profile {code} not found")
    data = dict(profiles[code] or {})
    data["profile_code"] = code
    return data


def _statement_to_dict(
    stmt: ParsedStatement,
    rules: list[dict[str, Any]],
    amount_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    for row in stmt.rows:
        category, subcategory, auto_classified = _classify_statement_row(row, rules, amount_rules)
        item = dict(row)
        item["category"] = category
        item["subcategory"] = subcategory
        item["auto_classified"] = auto_classified
        rows.append(item)

    inflow = sum((r["amount"] for r in rows if r["amount"] > 0), Decimal("0"))
    outflow = sum((-r["amount"] for r in rows if r["amount"] < 0), Decimal("0"))
    latest_balance = None
    latest_balance_date = None
    balance_rows = [r for r in rows if r.get("balance") is not None]
    if balance_rows:
        latest = balance_rows[-1]
        latest_balance = latest["balance"]
        latest_balance_date = latest["transaction_date"]

    return {
        "profile_code": stmt.profile_code,
        "source_file": stmt.source_file,
        "bank": stmt.bank,
        "account_code": stmt.account_code,
        "account_kind": stmt.account_kind,
        "account_tail": stmt.account_tail,
        "statement_start": stmt.statement_start,
        "statement_end": stmt.statement_end,
        "statement_balance": stmt.statement_balance,
        "latest_balance": latest_balance if latest_balance is not None else stmt.statement_balance,
        "net_asset_balance": _net_asset_balance(
            latest_balance if latest_balance is not None else stmt.statement_balance,
            stmt.account_kind,
        ),
        "latest_balance_date": latest_balance_date or stmt.statement_end,
        "balance_source": "statement_row" if latest_balance is not None else ("statement_balance" if stmt.statement_balance is not None else None),
        "row_count": len(rows),
        "inflow": inflow,
        "outflow": outflow,
        "net_flow": inflow - outflow,
        "rows": rows,
    }


class PreviewRequest(BaseModel):
    profile_code: Optional[str] = None
    files: Optional[list[str]] = None
    since: Optional[str] = None
    include_rows: bool = True


class ImportRequest(PreviewRequest):
    pass


def _collect_preview(req: PreviewRequest) -> list[dict[str, Any]]:
    config = _load_config()
    rules = config.get("rules", []) or []
    amount_rules = config.get("amount_rules", []) or []
    default_since = str((config.get("defaults") or {}).get("since", "2026-01-01"))
    requested_since = _parse_date(req.since) if req.since else None

    profile_codes = [req.profile_code] if req.profile_code else list((config.get("profiles") or {}).keys())
    results: list[dict[str, Any]] = []
    for profile_code in profile_codes:
        if not profile_code:
            continue
        profile = _profile(config, profile_code)
        since = requested_since or _parse_date(profile.get("since"), default_since)
        files = req.files if req.files else profile.get("default_files", [])
        for file_path in files:
            p = _resolve_statement_path(file_path)
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"statement file not found: {file_path}")
            stmt = parse_statement(p, profile, since)
            item = _statement_to_dict(stmt, rules, amount_rules)
            if not req.include_rows:
                item["rows"] = []
            results.append(item)
    return results


def _summarize(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for result in results:
        code = result["profile_code"]
        b = buckets.setdefault(
            code,
            {
                "profile_code": code,
                "account_code": result["account_code"],
                "account_kind": result["account_kind"],
                "bank": result["bank"],
                "files": 0,
                "row_count": 0,
                "inflow": Decimal("0"),
                "outflow": Decimal("0"),
                "net_flow": Decimal("0"),
                "latest_balance": None,
                "net_asset_balance": None,
                "latest_balance_date": None,
                "balance_source": None,
            },
        )
        b["files"] += 1
        b["row_count"] += result["row_count"]
        b["inflow"] += Decimal(str(result["inflow"]))
        b["outflow"] += Decimal(str(result["outflow"]))
        b["net_flow"] += Decimal(str(result["net_flow"]))
        latest_date = result.get("latest_balance_date")
        if latest_date and (not b["latest_balance_date"] or latest_date > b["latest_balance_date"]):
            b["latest_balance_date"] = latest_date
            b["latest_balance"] = result.get("latest_balance")
            b["net_asset_balance"] = _net_asset_balance(result.get("latest_balance"), result.get("account_kind"))
            b["balance_source"] = result.get("balance_source")
    return list(buckets.values())


def ensure_tables(conn) -> None:
    """Bank reconciliation tables are provisioned by database schema migrations."""
    return None


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS table_name", (table_name,))
    row = cur.fetchone()
    if not row:
        return False
    if isinstance(row, dict):
        return row.get("table_name") is not None
    return row[0] is not None


def _transfer_match_summary(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for match in matches:
        key = f"{match['match_type']}:{match.get('currency') or 'CNY'}"
        bucket = buckets.setdefault(
            key,
            {
                "match_type": match["match_type"],
                "currency": match.get("currency") or "CNY",
                "match_count": 0,
                "total_amount": Decimal("0"),
            },
        )
        bucket["match_count"] += 1
        bucket["total_amount"] += Decimal(str(match.get("amount") or "0"))
    return list(buckets.values())


class ConfirmTransferRequest(BaseModel):
    from_kind: Literal['cashflow', 'trade']
    from_id: int
    to_kind: Literal['cashflow', 'trade']
    to_id: int
    note: str
    confirmed: bool = False


class RevokeTransferRequest(BaseModel):
    note: str
    confirmed: bool = False


class ConfirmPaymentProxyRequest(BaseModel):
    source_id: int
    target_id: int
    note: str
    confirmed: bool = False


@router.post("/bank-reconcile/payment-proxy/confirm")
def confirm_payment_proxy(req: ConfirmPaymentProxyRequest):
    from domain.ledger import payment_proxy_confirmation
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _norm(payment_proxy_confirmation.confirm(cur, **req.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/bank-reconcile/payment-proxy/{confirmation_id}/revoke")
def revoke_payment_proxy(confirmation_id: UUID, req: RevokeTransferRequest):
    from domain.ledger import payment_proxy_confirmation
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _norm(payment_proxy_confirmation.revoke(cur, confirmation_id=confirmation_id, **req.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/bank-reconcile/payment-proxy")
def list_payment_proxy():
    from domain.ledger import payment_proxy_confirmation
    with get_conn() as conn:
        conn.set_session(isolation_level='REPEATABLE READ', readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            return _norm({'decisions': payment_proxy_confirmation.decisions(cur)})


@router.post("/bank-reconcile/transfers/confirm")
def confirm_transfer(req: ConfirmTransferRequest):
    """Record a user-confirmed link between two independent source facts."""
    from domain.ledger import confirmed_transfers
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            result = confirmed_transfers.confirm(cur, **req.model_dump())
        return _norm(result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/bank-reconcile/transfers/{transfer_id}/revoke")
def revoke_transfer(transfer_id: UUID, req: RevokeTransferRequest):
    """Append a revocation; the original confirmation remains immutable."""
    from domain.ledger import confirmed_transfers
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            result = confirmed_transfers.revoke(cur, transfer_id=transfer_id, **req.model_dump())
        return _norm(result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/bank-reconcile/bank-sources/{confirmation_id}/revoke")
def revoke_bank_source(confirmation_id: UUID, req: RevokeTransferRequest):
    """Append a correction to the bank-source confirmation evidence."""
    from domain.ledger import bank_source_confirmation
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            result = bank_source_confirmation.revoke(cur, confirmation_id=confirmation_id, **req.model_dump())
        return _norm(result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/bank-reconcile/config")
def get_bank_reconcile_config():
    """Return bank reconciliation profiles and rules."""
    config = _load_config()
    return _norm(config)


@router.post("/bank-reconcile/preview")
def preview_bank_reconcile(req: PreviewRequest):
    """Parse statement files and return a staging preview without writing to PAMS."""
    results = _collect_preview(req)
    return _norm({"status": "ok", "summary": _summarize(results), "statements": results})


@router.post("/bank-reconcile/import")
def import_bank_reconcile(req: ImportRequest):
    """Legacy entry: all new imports require the shared draft/preview approval."""
    raise HTTPException(409, detail={"code": "import_review_required",
        "message": "请通过导入资料统一核对后入库", "destination": "account_import.html?kind=cash"})


@router.get("/bank-reconcile/relationships")
def get_bank_reconcile_relationships():
    """Return the configured bank/account relationship graph used for reconciliation."""
    config = _load_config()
    return _norm(
        {
            "account_entities": config.get("account_entities", {}) or {},
            "account_relationships": config.get("account_relationships", []) or [],
            "internal_transfer_matching": config.get("internal_transfer_matching", {}) or {},
        }
    )


@router.get("/bank-reconcile/transfer-matches")
def list_transfer_matches(limit: int = 200):
    """List auto-matched account relationship rows from bank-statement staging."""
    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            from domain.ledger import confirmed_transfers
            matches = confirmed_transfers.read(cur, limit=max(1, min(limit, 1000)))
            for match in matches:
                left, right = match['from_evidence'], match['to_evidence']
                match.update(id=match['transfer_id'], match_key=str(match['transfer_id']),
                    match_type='investment_funding' if 'trade' in {left['kind'],right['kind']} else 'internal_transfer',
                    relation_code='manual_confirmed', source_row_id=left['id'], target_row_id=right['id'],
                    source_ref_table='pams_cashflow_fact' if left['kind']=='cashflow' else 'pams_investment_trade_fact',
                    target_ref_table='pams_cashflow_fact' if right['kind']=='cashflow' else 'pams_investment_trade_fact',
                    source_date=left['day'], target_date=right['day'], source_amount=left['signed_amount'],
                    target_amount=right['signed_amount'], confidence=None, status=match['evidence_status'])
            summary = _transfer_match_summary([m for m in matches if m['evidence_status']=='confirmed'])
            return _norm({"matches": matches, "summary": summary, "table_ready": True,
                          "data_status": "confirmed_relations_only", "coverage_status": "unknown"})


@router.get("/bank-reconcile/unmatched-rows")
def list_unmatched_rows(since: str = "2026-01-01", limit: int = 500):
    """List unmatched or one-sided flows that require user confirmation."""
    try:
        since_date = date.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD") from exc

    limit = max(1, min(int(limit or 500), 1000))
    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            from domain.ledger import bank_source_read
            rows = bank_source_read.unmatched(cur, since=since_date, limit=limit)

    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row.get('issue_type')}:{row.get('account_code')}:{row.get('currency') or 'CNY'}"
        bucket = summary.setdefault(
            key,
            {
                "issue_type": row.get("issue_type"),
                "account_code": row.get("account_code"),
                "currency": row.get("currency") or "CNY",
                "count": 0,
                "amount_abs": Decimal("0"),
            },
        )
        bucket["count"] += 1
        bucket["amount_abs"] += Decimal(str(row.get("amount") or "0")).copy_abs()

    return _norm(
        {
            "since": since_date,
            "limit": limit,
            "rows": rows,
            "summary": list(summary.values()),
        }
    )


@router.post("/bank-reconcile/transfer-matches/run")
def run_transfer_matching():
    """Return pending independent facts; confirmation remains explicit."""
    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            from domain.ledger import bank_source_read
            pending = bank_source_read.unmatched(cur, since=date(1900, 1, 1), limit=1000)
            from domain.ledger.transfer_candidates import list_candidates
            candidates = list_candidates(cur, _load_config())
    return _norm({'status':'review_required', 'matched':0, 'matches':candidates, 'summary':[],
                  'pending':pending, 'coverage_status':'unknown', 'cashflow_backfill':{'status':'not_run'}})


@router.get("/bank-reconcile/imports")
def list_bank_reconcile_imports():
    """List unified bank sources and explicitly partial account summaries."""
    with get_conn() as conn:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            from domain.ledger import bank_source_read
            imports, summaries = bank_source_read.imports(cur)
            return _norm({'imports':imports, 'summary':summaries, 'coverage_status':'unknown'})
