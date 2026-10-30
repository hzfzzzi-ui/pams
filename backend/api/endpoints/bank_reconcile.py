"""Bank statement reconciliation staging API."""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from collectors.bank_statement_parser import ParsedStatement, parse_statement
from domain.ledger.cashflow_fact import backfill_cashflow_facts
from domain.ledger.bank_statement_rules import (
    _classify as _classify, _classify_by_amount as _classify_by_amount,
    _classify_statement_row, _row_transfer_amount, _amount_close,
)

router = APIRouter()

CONFIG_PATH = Path(os.environ.get("PAMS_BANK_RECONCILE_CONFIG", "/app/config/bank_reconcile.yaml"))
LOCAL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "bank_reconcile.yaml"
BANK_STATEMENT_ROWS_TABLE = "pams_bank_statement_rows"
TRANSFER_MATCH_TABLE = "pams_bank_transfer_matches"
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
    return psycopg2.connect(
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
        """,
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
    return payload


def _row_text(row: dict[str, Any]) -> str:
    """Return a searchable text blob for one staged bank statement row."""
    raw = row.get("raw_json") or row.get("raw") or {}
    raw_text = ""
    if isinstance(raw, dict):
        raw_text = " ".join(str(v or "") for v in raw.values())
    return " ".join(
        str(row.get(k) or "") for k in ("summary", "description", "counterparty", "category", "subcategory")
    ) + " " + raw_text


def _row_cny_transfer_amount(row: dict[str, Any]) -> Decimal:
    """Return the CNY-side amount when a statement row has foreign-currency settlement.

    Credit-card USD repayments often settle on the card in USD while the funding
    account pays a CNY amount. For matching against bank debits, prefer the
    original CNY amount when available.
    """
    original_currency = str(row.get("original_currency") or "").upper()
    original_amount = row.get("original_amount")
    if original_currency == "CNY" and original_amount not in (None, ""):
        return Decimal(str(original_amount)).copy_abs()

    raw = row.get("raw_json") or row.get("raw") or {}
    if isinstance(raw, dict):
        raw_original_currency = str(raw.get("original_currency") or raw.get("original_currency_text") or "").upper()
        if raw_original_currency in {"CNY", "人民币"} and raw.get("original_amount") not in (None, ""):
            return Decimal(str(raw.get("original_amount"))).copy_abs()

    return _row_transfer_amount(row)


def _date_delta_days(left: date, right: date) -> int:
    """Return the absolute date distance in calendar days."""
    return abs((left - right).days)


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(str(k) and str(k) in text for k in keywords)


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


def _load_staged_rows(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id, import_id, source_uid, profile_code, bank, account_code, account_kind,
               transaction_date, posting_date, amount, currency, balance,
               original_amount, original_currency, category, subcategory,
               auto_classified, summary, description, counterparty, raw_json
        FROM pams_bank_statement_rows
        ORDER BY transaction_date, id
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _best_amount_date_match(
    reference: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    used_ids: set[int],
    date_window_days: int,
    tolerance: Decimal,
    reference_amount: Decimal | None = None,
) -> tuple[dict[str, Any] | None, int]:
    ref_amount = reference_amount if reference_amount is not None else _row_transfer_amount(reference)
    best: tuple[int, int, dict[str, Any]] | None = None
    for candidate in candidates:
        if candidate["id"] in used_ids:
            continue
        if not _amount_close(ref_amount, _row_transfer_amount(candidate), tolerance):
            continue
        delta = _date_delta_days(reference["transaction_date"], candidate["transaction_date"])
        if delta > date_window_days:
            continue
        rank = (delta, int(candidate["id"]))
        if best is None or rank < best[:2]:
            best = (rank[0], rank[1], candidate)
    if best is None:
        return None, 0
    return best[2], best[0]


def _match_key(match_type: str, relation_code: str, source_row_id: int | None, target_row_id: int | None, amount: Decimal) -> str:
    source = source_row_id if source_row_id is not None else "none"
    target = target_row_id if target_row_id is not None else "none"
    cents = amount.quantize(Decimal("0.01"))
    return f"{match_type}:{relation_code}:{source}:{target}:{cents}"


def _make_match(
    *,
    match_type: str,
    relation_code: str,
    from_account_code: str,
    to_account_code: str,
    amount: Decimal,
    source_row: dict[str, Any] | None,
    target_row: dict[str, Any] | None,
    date_delta_days: int,
    confidence: Decimal,
    note: str,
) -> dict[str, Any]:
    source_id = source_row.get("id") if source_row else None
    target_id = target_row.get("id") if target_row else None
    match_date = None
    if source_row and source_row.get("transaction_date"):
        match_date = source_row["transaction_date"]
    elif target_row and target_row.get("transaction_date"):
        match_date = target_row["transaction_date"]
    currency = (source_row or target_row or {}).get("currency") or "CNY"
    return {
        "match_key": _match_key(match_type, relation_code, source_id, target_id, amount),
        "match_type": match_type,
        "relation_code": relation_code,
        "from_account_code": from_account_code,
        "to_account_code": to_account_code,
        "source_row_id": source_id,
        "target_row_id": target_id,
        "amount": amount,
        "currency": currency,
        "match_date": match_date,
        "date_delta_days": date_delta_days,
        "confidence": confidence,
        "note": note,
    }


def _build_transfer_matches(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    matching_cfg = config.get("internal_transfer_matching") or {}
    date_window_days = int(matching_cfg.get("date_window_days", 2))
    tolerance = Decimal(str(matching_cfg.get("amount_tolerance", "0.01")))

    ccb_rows = [r for r in rows if r.get("account_code") == "ccb_7332"]
    wechat_rows = [r for r in rows if r.get("account_code") == "wechat"]
    used_bank_ids: set[int] = set()
    used_wechat_ids: set[int] = set()
    matches: list[dict[str, Any]] = []

    bank_investment_candidates = [
        r
        for r in ccb_rows
        if Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["财付通", "微信支付", "腾安基金", "理财通"])
    ]
    wechat_licaitong_from_bank = [
        r
        for r in wechat_rows
        if _row_transfer_amount(r) > 0
        and _has_any(_row_text(r), ["购买理财通", "理财通"])
        and _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
    ]
    for wechat_row in wechat_licaitong_from_bank:
        bank_row, delta = _best_amount_date_match(
            wechat_row,
            bank_investment_candidates,
            used_ids=used_bank_ids,
            date_window_days=date_window_days,
            tolerance=tolerance,
        )
        if not bank_row:
            continue
        amount = _row_transfer_amount(wechat_row)
        used_bank_ids.add(bank_row["id"])
        used_wechat_ids.add(wechat_row["id"])
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="ccb_to_tencent_licaitong",
                from_account_code="ccb_7332",
                to_account_code="tencent_licaitong",
                amount=amount,
                source_row=bank_row,
                target_row=wechat_row,
                date_delta_days=delta,
                confidence=Decimal("0.98"),
                note="建行扣款与微信理财通中性记录金额匹配",
            )
        )

    wechat_lqt_to_licaitong = [
        r
        for r in wechat_rows
        if r["id"] not in used_wechat_ids
        and _row_transfer_amount(r) > 0
        and _has_any(_row_text(r), ["购买理财通", "理财通"])
        and _has_any(_row_text(r), ["零钱通"])
        and not _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
    ]
    for row in wechat_lqt_to_licaitong:
        amount = _row_transfer_amount(row)
        used_wechat_ids.add(row["id"])
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="wechat_lqt_to_tencent_licaitong",
                from_account_code="wechat_lqt",
                to_account_code="tencent_licaitong",
                amount=amount,
                source_row=row,
                target_row=None,
                date_delta_days=0,
                confidence=Decimal("0.90"),
                note="微信零钱通申购理财通，微信账单为中性记录",
            )
        )

    bank_payment_candidates = [
        r
        for r in ccb_rows
        if r["id"] not in used_bank_ids
        and Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["财付通", "微信支付"])
        and not _has_any(_row_text(r), ["腾安基金", "理财通"])
    ]
    wechat_bank_card_payments = [
        r
        for r in wechat_rows
        if r["id"] not in used_wechat_ids
        and Decimal(str(r.get("amount") or "0")) < 0
        and _has_any(_row_text(r), ["建设银行储蓄卡(7332)"])
        and not _has_any(_row_text(r), ["理财通"])
    ]
    for wechat_row in wechat_bank_card_payments:
        bank_row, delta = _best_amount_date_match(
            wechat_row,
            bank_payment_candidates,
            used_ids=used_bank_ids,
            date_window_days=1,
            tolerance=tolerance,
        )
        if not bank_row:
            continue
        amount = _row_transfer_amount(wechat_row)
        used_bank_ids.add(bank_row["id"])
        used_wechat_ids.add(wechat_row["id"])
        matches.append(
            _make_match(
                match_type="payment_proxy",
                relation_code="ccb_wechat_payment_proxy",
                from_account_code="ccb_7332",
                to_account_code="wechat_balance",
                amount=amount,
                source_row=bank_row,
                target_row=wechat_row,
                date_delta_days=delta,
                confidence=Decimal("0.95"),
                note="同一笔外部消费：建行实际扣款，微信只作支付通道",
            )
        )

    payment_proxy_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "payment_proxy"
        and r.get("code") != "ccb_wechat_payment_proxy"
    ]
    used_proxy_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    used_proxy_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in payment_proxy_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        source_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_proxy_source_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not from_keywords or _has_any(_row_text(r), from_keywords))
        ]
        target_candidates = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and r["id"] not in used_proxy_target_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not to_keywords or _has_any(_row_text(r), to_keywords))
        ]
        target_row: dict[str, Any] | None
        for target_row in target_candidates:
            source_row, delta = _best_amount_date_match(
                target_row,
                source_candidates,
                used_ids=used_proxy_source_ids,
                date_window_days=1,
                tolerance=tolerance,
            )
            if not source_row:
                continue
            amount = _row_transfer_amount(target_row)
            used_proxy_source_ids.add(source_row["id"])
            used_proxy_target_ids.add(target_row["id"])
            if source_row.get("account_code") == "ccb_7332":
                used_bank_ids.add(source_row["id"])
            matches.append(
                _make_match(
                    match_type="payment_proxy",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}_proxy"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=source_row,
                    target_row=target_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.95"),
                    note="同一笔外部消费：资金账户实际扣款，第三方支付账户只作支付通道",
                )
            )

    direct_investment_rows = [
        r
        for r in ccb_rows
        if r["id"] not in used_bank_ids
        and _has_any(_row_text(r), ["基金下账", "基金上账", "基金定投申购", "基金赎回款", "理财产品赎回", "理财赎回"])
    ]
    for row in direct_investment_rows:
        amount = _row_transfer_amount(row)
        if amount <= 0:
            continue
        is_outflow = Decimal(str(row.get("amount") or "0")) < 0
        matches.append(
            _make_match(
                match_type="investment_funding",
                relation_code="ccb_direct_investment_flow",
                from_account_code="ccb_7332" if is_outflow else "direct_investment",
                to_account_code="direct_investment" if is_outflow else "ccb_7332",
                amount=amount,
                source_row=row,
                target_row=None,
                date_delta_days=0,
                confidence=Decimal("0.75"),
                note="建行账单中的基金/理财申赎流水，当前未找到平台侧成对记录",
            )
        )

    source_only_investment_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "investment_funding" and r.get("match_mode") == "source_only"
    ]
    matched_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    for relation in source_only_investment_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        for row in rows:
            if row.get("account_code") != from_account:
                continue
            if row["id"] in matched_source_ids:
                continue
            if Decimal(str(row.get("amount") or "0")) >= 0:
                continue
            if from_keywords and not _has_any(_row_text(row).upper(), [str(k).upper() for k in from_keywords]):
                continue
            amount = _row_transfer_amount(row)
            if amount <= 0:
                continue
            matched_source_ids.add(row["id"])
            matches.append(
                _make_match(
                    match_type="account_transfer",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=row,
                    target_row=None,
                    date_delta_days=0,
                    confidence=Decimal("0.85"),
                    note="银行账单中的单边投资账户转账，投资平台侧到账记录缺失，暂按在途资金处理",
                )
            )

    benefit_income_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "benefit_income"
    ]
    matched_benefit_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in benefit_income_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        for row in rows:
            if row.get("account_code") != to_account:
                continue
            if row["id"] in matched_benefit_target_ids:
                continue
            if Decimal(str(row.get("amount") or "0")) <= 0:
                continue
            if to_keywords and not _has_any(_row_text(row), to_keywords):
                continue
            amount = _row_transfer_amount(row)
            if amount <= 0:
                continue
            matched_benefit_target_ids.add(row["id"])
            matches.append(
                _make_match(
                    match_type="benefit_income",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=None,
                    target_row=row,
                    date_delta_days=0,
                    confidence=Decimal("0.90"),
                    note="雇主福利/医保缴费入账，按医保台账单边确认",
                )
            )

    account_transfer_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "account_transfer"
    ]
    used_transfer_source_ids = {int(m["source_row_id"]) for m in matches if m.get("source_row_id") is not None}
    used_transfer_target_ids = {int(m["target_row_id"]) for m in matches if m.get("target_row_id") is not None}
    for relation in account_transfer_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = (relation.get("match_keywords") or {}).get("from") or []
        to_keywords = (relation.get("match_keywords") or {}).get("to") or []
        source_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_transfer_source_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and (not from_keywords or _has_any(_row_text(r), from_keywords))
        ]
        target_candidates = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and Decimal(str(r.get("amount") or "0")) > 0
            and (not to_keywords or _has_any(_row_text(r), to_keywords))
        ]
        for source_row in source_candidates:
            target_row, delta = _best_amount_date_match(
                source_row,
                target_candidates,
                used_ids=used_transfer_target_ids,
                date_window_days=date_window_days,
                tolerance=tolerance,
            )
            if not target_row:
                continue
            amount = _row_transfer_amount(source_row)
            used_transfer_source_ids.add(source_row["id"])
            used_transfer_target_ids.add(target_row["id"])
            matches.append(
                _make_match(
                    match_type="account_transfer",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=amount,
                    source_row=source_row,
                    target_row=target_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.98"),
                    note="银行账户之间内部转账，金额与日期匹配",
                )
            )

    credit_payment_relations = [
        r
        for r in (config.get("account_relationships") or [])
        if r.get("relation_type") == "credit_card_payment"
    ]
    for relation in credit_payment_relations:
        from_account = relation.get("from_account")
        to_account = relation.get("to_account")
        from_keywords = ((relation.get("match_keywords") or {}).get("from") or []) + ["信用卡", "还款"]
        to_keywords = ((relation.get("match_keywords") or {}).get("to") or []) + ["还款"]
        bank_candidates = [
            r
            for r in rows
            if r.get("account_code") == from_account
            and r["id"] not in used_bank_ids
            and Decimal(str(r.get("amount") or "0")) < 0
            and _has_any(_row_text(r), from_keywords)
        ]
        credit_repayments = [
            r
            for r in rows
            if r.get("account_code") == to_account
            and Decimal(str(r.get("amount") or "0")) > 0
            and _has_any(_row_text(r), to_keywords)
        ]
        for credit_row in credit_repayments:
            repayment_amount = _row_cny_transfer_amount(credit_row)
            bank_row, delta = _best_amount_date_match(
                credit_row,
                bank_candidates,
                used_ids=used_bank_ids,
                date_window_days=date_window_days,
                tolerance=tolerance,
                reference_amount=repayment_amount,
            )
            if not bank_row:
                continue
            used_bank_ids.add(bank_row["id"])
            matches.append(
                _make_match(
                    match_type="credit_card_payment",
                    relation_code=str(relation.get("code") or f"{from_account}_to_{to_account}"),
                    from_account_code=str(from_account),
                    to_account_code=str(to_account),
                    amount=repayment_amount,
                    source_row=bank_row,
                    target_row=credit_row,
                    date_delta_days=delta,
                    confidence=Decimal("0.97"),
                    note="银行扣款与信用卡账单还款记录金额匹配",
                )
            )

    matches.sort(key=lambda m: (m["match_date"] or date.min, m["match_type"], m["source_row_id"] or 0), reverse=True)
    return matches


def _write_transfer_matches(cur, matches: list[dict[str, Any]]) -> int:
    if not _table_exists(cur, TRANSFER_MATCH_TABLE):
        raise HTTPException(status_code=503, detail="bank transfer match table is not initialized")
    cur.execute("DELETE FROM pams_bank_transfer_matches WHERE status = 'auto'")
    inserted = 0
    for match in matches:
        cur.execute(
            """
            INSERT INTO pams_bank_transfer_matches (
                match_key, match_type, relation_code, from_account_code, to_account_code,
                source_row_id, target_row_id, amount, currency, match_date, date_delta_days,
                confidence, status, note
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, 'auto', %s
            )
            ON CONFLICT (match_key) DO UPDATE SET
                match_type = EXCLUDED.match_type,
                relation_code = EXCLUDED.relation_code,
                from_account_code = EXCLUDED.from_account_code,
                to_account_code = EXCLUDED.to_account_code,
                source_row_id = EXCLUDED.source_row_id,
                target_row_id = EXCLUDED.target_row_id,
                amount = EXCLUDED.amount,
                currency = EXCLUDED.currency,
                match_date = EXCLUDED.match_date,
                date_delta_days = EXCLUDED.date_delta_days,
                confidence = EXCLUDED.confidence,
                note = CASE
                    WHEN pams_bank_transfer_matches.status = 'confirmed'
                    THEN pams_bank_transfer_matches.note
                    ELSE EXCLUDED.note
                END,
                updated_at = NOW()
            """,
            (
                match["match_key"],
                match["match_type"],
                match["relation_code"],
                match["from_account_code"],
                match["to_account_code"],
                match["source_row_id"],
                match["target_row_id"],
                match["amount"],
                match["currency"],
                match["match_date"],
                match["date_delta_days"],
                match["confidence"],
                match["note"],
            ),
        )
        inserted += 1

    _sync_reconciliation_cases_from_transfer_matches(cur, matches)

    internal_row_ids: set[int] = set()
    bank_transfer_row_ids: set[int] = set()
    credit_payment_row_ids: set[int] = set()
    benefit_income_row_ids: set[int] = set()
    proxy_wechat_row_ids: set[int] = set()
    for match in matches:
        row_ids = [match.get("source_row_id"), match.get("target_row_id")]
        if match["match_type"] == "investment_funding":
            internal_row_ids.update(int(x) for x in row_ids if x is not None)
        elif match["match_type"] == "account_transfer":
            bank_transfer_row_ids.update(int(x) for x in row_ids if x is not None)
        elif match["match_type"] == "credit_card_payment":
            credit_payment_row_ids.update(int(x) for x in row_ids if x is not None)
        elif match["match_type"] == "benefit_income":
            benefit_income_row_ids.update(int(x) for x in row_ids if x is not None)
        elif match["match_type"] == "payment_proxy" and match.get("target_row_id") is not None:
            proxy_wechat_row_ids.add(int(match["target_row_id"]))

    if internal_row_ids:
        cur.execute(
            """
            UPDATE pams_bank_statement_rows
            SET category = %s, subcategory = %s, auto_classified = TRUE
            WHERE id = ANY(%s)
            """,
            (INTERNAL_TRANSFER_CATEGORY, INTERNAL_TRANSFER_SUBCATEGORY, list(internal_row_ids)),
        )
    if bank_transfer_row_ids:
        cur.execute(
            """
            UPDATE pams_bank_statement_rows
            SET category = %s, subcategory = %s, auto_classified = TRUE
            WHERE id = ANY(%s)
            """,
            (INTERNAL_TRANSFER_CATEGORY, BANK_ACCOUNT_TRANSFER_SUBCATEGORY, list(bank_transfer_row_ids)),
        )
    if credit_payment_row_ids:
        cur.execute(
            """
            UPDATE pams_bank_statement_rows
            SET category = %s, subcategory = %s, auto_classified = TRUE
            WHERE id = ANY(%s)
            """,
            (INTERNAL_TRANSFER_CATEGORY, CREDIT_CARD_PAYMENT_SUBCATEGORY, list(credit_payment_row_ids)),
        )
    if benefit_income_row_ids:
        cur.execute(
            """
            UPDATE pams_bank_statement_rows
            SET category = %s, subcategory = %s, auto_classified = TRUE
            WHERE id = ANY(%s)
            """,
            (BENEFIT_INCOME_CATEGORY, BENEFIT_INCOME_SUBCATEGORY, list(benefit_income_row_ids)),
        )
    if proxy_wechat_row_ids:
        cur.execute(
            """
            UPDATE pams_bank_statement_rows
            SET category = %s, subcategory = %s, auto_classified = TRUE
            WHERE id = ANY(%s)
            """,
            (PAYMENT_PROXY_CATEGORY, PAYMENT_PROXY_SUBCATEGORY, list(proxy_wechat_row_ids)),
        )
    return inserted


def _sync_reconciliation_cases_from_transfer_matches(cur, matches: list[dict[str, Any]]) -> int:
    """Mirror current bank-transfer matches into the canonical reconciliation table."""

    if not _table_exists(cur, "pams_reconciliation_case"):
        return 0

    cur.execute(
        """
        DELETE FROM pams_reconciliation_case
        WHERE COALESCE(status, 'auto') IN ('auto', 'open')
          AND reviewed_at IS NULL
          AND case_type <> 'account_review'
          AND (
                source_ref_table = 'pams_bank_statement_rows'
             OR target_ref_table = 'pams_bank_statement_rows'
          )
        """
    )

    inserted = 0
    for match in matches:
        has_target = match.get("target_row_id") is not None
        status = "auto" if has_target else "open"
        raw_json = {
            **match,
            "source": "bank_transfer_matcher",
            "status_policy": "paired_auto" if has_target else "source_only_open",
        }
        cur.execute(
            """
            INSERT INTO pams_reconciliation_case (
                case_type, relation_code, from_account_code, to_account_code,
                source_ref_table, source_ref_id, target_ref_table, target_ref_id,
                amount, currency, case_date, confidence, status, note, raw_json
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                match["match_type"],
                match["relation_code"],
                match["from_account_code"],
                match["to_account_code"],
                "pams_bank_statement_rows" if match.get("source_row_id") is not None else None,
                str(match["source_row_id"]) if match.get("source_row_id") is not None else None,
                "pams_bank_statement_rows" if match.get("target_row_id") is not None else None,
                str(match["target_row_id"]) if match.get("target_row_id") is not None else None,
                match["amount"],
                match["currency"],
                match["match_date"],
                match["confidence"],
                status,
                match["note"],
                psycopg2.extras.Json(_norm(raw_json)),
            ),
        )
        inserted += max(int(getattr(cur, "rowcount", 0) or 0), 0)
    return inserted


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


def _fetch_transfer_matches(cur, limit: int = 200) -> list[dict[str, Any]]:
    if not _table_exists(cur, TRANSFER_MATCH_TABLE):
        return []
    cur.execute(
        """
        SELECT m.id, m.match_key, m.match_type, m.relation_code,
               m.from_account_code, m.to_account_code, m.source_row_id, m.target_row_id,
               m.amount, m.currency, m.match_date, m.date_delta_days,
               m.confidence, m.status, m.note, m.updated_at,
               sr.transaction_date AS source_date,
               sr.summary AS source_summary,
               sr.description AS source_description,
               sr.amount AS source_amount,
               tr.transaction_date AS target_date,
               tr.summary AS target_summary,
               tr.description AS target_description,
               tr.amount AS target_amount,
               tr.original_amount AS target_original_amount
        FROM pams_bank_transfer_matches m
        LEFT JOIN pams_bank_statement_rows sr ON sr.id = m.source_row_id
        LEFT JOIN pams_bank_statement_rows tr ON tr.id = m.target_row_id
        ORDER BY m.match_date DESC NULLS LAST, m.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_transfer_match_summary(cur) -> list[dict[str, Any]]:
    if not _table_exists(cur, TRANSFER_MATCH_TABLE):
        return []
    cur.execute(
        """
        SELECT match_type,
               currency,
               COUNT(*) AS match_count,
               SUM(amount) AS total_amount
        FROM pams_bank_transfer_matches
        GROUP BY match_type, currency
        ORDER BY match_type, currency
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_unmatched_confirmation_rows(cur, since: date, limit: int = 500) -> list[dict[str, Any]]:
    """Return statement flows that still need user confirmation after auto-matching."""
    if not _table_exists(cur, BANK_STATEMENT_ROWS_TABLE):
        return []
    if not _table_exists(cur, TRANSFER_MATCH_TABLE):
        cur.execute(
            """
            SELECT
                'unmatched_row' AS issue_type,
                NULL::TEXT AS match_type,
                NULL::TEXT AS relation_code,
                NULL::TEXT AS from_account_code,
                NULL::TEXT AS to_account_code,
                NULL::NUMERIC AS confidence,
                '转账匹配表未初始化，暂按未匹配流水展示' AS issue_note,
                r.id AS row_id,
                r.profile_code,
                r.account_code,
                r.transaction_date,
                r.amount,
                r.currency,
                r.original_amount,
                r.original_currency,
                r.category,
                r.subcategory,
                r.summary,
                r.description,
                r.counterparty
            FROM pams_bank_statement_rows r
            WHERE r.transaction_date >= %s
              AND (
                  COALESCE(r.category, '未分类') = '未分类'
                  OR r.category = %s
              )
            ORDER BY r.transaction_date DESC, r.id DESC
            LIMIT %s
            """,
            (since, INTERNAL_TRANSFER_CATEGORY, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    cur.execute(
        """
        WITH matched_rows AS (
            SELECT source_row_id AS row_id
            FROM pams_bank_transfer_matches
            WHERE source_row_id IS NOT NULL
            UNION
            SELECT target_row_id AS row_id
            FROM pams_bank_transfer_matches
            WHERE target_row_id IS NOT NULL
        ), unmatched_rows AS (
            SELECT
                'unmatched_row' AS issue_type,
                NULL::TEXT AS match_type,
                NULL::TEXT AS relation_code,
                NULL::TEXT AS from_account_code,
                NULL::TEXT AS to_account_code,
                NULL::NUMERIC AS confidence,
                '没有命中任何内部转账/支付通道匹配规则' AS issue_note,
                r.id AS row_id,
                r.profile_code,
                r.account_code,
                r.transaction_date,
                r.amount,
                r.currency,
                r.original_amount,
                r.original_currency,
                r.category,
                r.subcategory,
                r.summary,
                r.description,
                r.counterparty
            FROM pams_bank_statement_rows r
            LEFT JOIN matched_rows mr ON mr.row_id = r.id
            WHERE r.transaction_date >= %s
              AND mr.row_id IS NULL
              AND (
                  COALESCE(r.category, '未分类') = '未分类'
                  OR r.category = %s
              )
        ), one_sided_matches AS (
            SELECT
                'one_sided_match' AS issue_type,
                m.match_type,
                m.relation_code,
                m.from_account_code,
                m.to_account_code,
                m.confidence,
                COALESCE(m.note, '已识别为内部关系，但没有找到成对平台/账户侧流水') AS issue_note,
                r.id AS row_id,
                r.profile_code,
                r.account_code,
                r.transaction_date,
                r.amount,
                r.currency,
                r.original_amount,
                r.original_currency,
                r.category,
                r.subcategory,
                r.summary,
                r.description,
                r.counterparty
            FROM pams_bank_transfer_matches m
            JOIN pams_bank_statement_rows r ON r.id = m.source_row_id
            WHERE m.target_row_id IS NULL
              AND r.transaction_date >= %s
        )
        SELECT *
        FROM (
            SELECT * FROM unmatched_rows
            UNION ALL
            SELECT * FROM one_sided_matches
        ) x
        ORDER BY transaction_date DESC, row_id DESC
        LIMIT %s
        """,
        (since, INTERNAL_TRANSFER_CATEGORY, since, limit),
    )
    return [dict(r) for r in cur.fetchall()]


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
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            table_ready = _table_exists(cur, TRANSFER_MATCH_TABLE)
            matches = _fetch_transfer_matches(cur, limit=limit)
            summary = _fetch_transfer_match_summary(cur)
    return _norm({"matches": matches, "summary": summary, "table_ready": table_ready})


@router.get("/bank-reconcile/unmatched-rows")
def list_unmatched_rows(since: str = "2026-01-01", limit: int = 500):
    """List unmatched or one-sided flows that require user confirmation."""
    try:
        since_date = date.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD") from exc

    limit = max(1, min(int(limit or 500), 1000))
    with get_conn() as conn:
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rows = _fetch_unmatched_confirmation_rows(cur, since_date, limit=limit)

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
    """Run auto-matching for internal transfers and payment-proxy duplicate rows."""
    config = _load_config()
    with get_conn() as conn:
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rows = _load_staged_rows(cur)
            matches = _build_transfer_matches(rows, config)
            inserted = _write_transfer_matches(cur, matches)
            cashflow_backfill = backfill_cashflow_facts(cur)
            conn.commit()
            persisted = _fetch_transfer_matches(cur)
            summary = _fetch_transfer_match_summary(cur)
    return _norm(
        {
            "status": "ok",
            "matched": inserted,
            "cashflow_backfill": cashflow_backfill,
            "summary": summary,
            "matches": persisted,
        }
    )


@router.get("/bank-reconcile/imports")
def list_bank_reconcile_imports():
    """List bank-statement staging imports and account-level summaries."""
    with get_conn() as conn:
        ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, profile_code, bank, account_code, account_kind, source_file,
                       statement_start, statement_end, statement_balance, row_count, imported_at
                FROM pams_bank_statement_imports
                ORDER BY imported_at DESC, id DESC
                LIMIT 50
                """
            )
            imports = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                WITH latest_import AS (
                    SELECT DISTINCT ON (profile_code)
                           profile_code,
                           statement_balance AS statement_balance,
                           statement_end AS statement_balance_date
                    FROM pams_bank_statement_imports
                    ORDER BY profile_code, statement_end DESC NULLS LAST, id DESC
                ),
                latest_row_balance AS (
                    SELECT DISTINCT ON (profile_code)
                           profile_code,
                           balance AS row_balance,
                           transaction_date AS row_balance_date
                    FROM pams_bank_statement_rows
                    WHERE balance IS NOT NULL
                    ORDER BY profile_code, transaction_date DESC, id DESC
                ),
                latest_holding_asof AS (
                    SELECT account_code, MAX(as_of) AS as_of
                    FROM pams_holdings_snapshot
                    GROUP BY account_code
                ),
                latest_holding_balance AS (
                    SELECT h.account_code,
                           h.as_of AS holding_balance_date,
                           SUM(
                               CASE
                                   WHEN h.asset_liability = '负债' THEN -ABS(h.market_value)
                                   ELSE h.market_value
                               END
                           ) AS holding_balance
                    FROM pams_holdings_snapshot h
                    JOIN latest_holding_asof la
                      ON la.account_code = h.account_code
                     AND la.as_of = h.as_of
                    GROUP BY h.account_code, h.as_of
                ),
                account_summary AS (
                    SELECT r.profile_code, r.account_code, r.account_kind, r.bank,
                           COUNT(*) AS row_count,
                           SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS inflow,
                           SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS outflow,
                           SUM(amount) AS net_flow,
                           MAX(transaction_date) AS latest_transaction_date,
                           COALESCE(lrb.row_balance, li.statement_balance, lhb.holding_balance) AS latest_balance,
                           COALESCE(lrb.row_balance_date, li.statement_balance_date, lhb.holding_balance_date) AS latest_balance_date,
                           CASE
                               WHEN lrb.row_balance IS NOT NULL THEN 'statement_row'
                               WHEN li.statement_balance IS NOT NULL THEN 'statement_balance'
                               WHEN lhb.holding_balance IS NOT NULL THEN 'holding_snapshot'
                               ELSE NULL
                           END AS balance_source,
                           CASE
                               WHEN r.account_kind = 'credit_card' AND lrb.row_balance IS NOT NULL
                               THEN -ABS(lrb.row_balance)
                               WHEN r.account_kind = 'credit_card' AND li.statement_balance IS NOT NULL
                               THEN -ABS(li.statement_balance)
                               ELSE COALESCE(lrb.row_balance, li.statement_balance, lhb.holding_balance)
                           END AS net_asset_balance
                    FROM pams_bank_statement_rows r
                    LEFT JOIN latest_import li ON li.profile_code = r.profile_code
                    LEFT JOIN latest_row_balance lrb ON lrb.profile_code = r.profile_code
                    LEFT JOIN latest_holding_balance lhb ON lhb.account_code = r.account_code
                    GROUP BY r.profile_code, r.account_code, r.account_kind, r.bank,
                             li.statement_balance, li.statement_balance_date,
                             lrb.row_balance, lrb.row_balance_date,
                             lhb.holding_balance, lhb.holding_balance_date
                )
                SELECT *
                FROM account_summary
                ORDER BY account_code
                """
            )
            summaries = [dict(r) for r in cur.fetchall()]
    return _norm({"imports": imports, "summary": summaries})
