"""Account master-data and reconciliation overview endpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from domain.imports.source_row_read import source_row_relation

router = APIRouter()

CONFIG_PATH = Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml"))
LOCAL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "accounts.yaml"
BANK_CONFIG_PATH = Path(os.environ.get("PAMS_BANK_RECONCILE_CONFIG", "/app/config/bank_reconcile.yaml"))
LOCAL_BANK_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "bank_reconcile.yaml"
LIABILITY_ACCOUNT_TYPES = {"liability"}
LIABILITY_ACCOUNT_KINDS = {"credit_card"}

TRUTH_SOURCE_LABELS = {
    "bank_statement": "银行/支付账单",
    "platform_snapshot": "平台持仓快照",
    "ibkr_flex": "IBKR Flex",
    "official_statement": "官方余额/明细",
    "manual": "手动确认",
}

RECONCILE_GROUPS = {
    "cash": {"label": "现金与支付", "order": 10},
    "investment": {"label": "投资与权益", "order": 20},
    "benefit": {"label": "公积金与医保", "order": 30},
    "liability": {"label": "信用卡", "order": 40},
    "clearing": {"label": "在途款", "order": 50},
}

DEFAULT_ACCOUNT_ROOTS = {
    "ccb": {"display_name": "建设银行", "sort_order": 10},
    "icbc_6689": {"display_name": "工商银行", "sort_order": 20},
    "santander": {"display_name": "Santander", "sort_order": 30},
    "wechat": {"display_name": "微信", "sort_order": 40},
    "alipay": {"display_name": "支付宝", "sort_order": 50},
    "ibkr": {"display_name": "IBKR", "sort_order": 60},
    "tonghuashun": {"display_name": "同花顺", "sort_order": 70},
    "tencent_licaitong": {"display_name": "腾讯理财通", "sort_order": 80},
    "huawei": {"display_name": "华为", "sort_order": 90},
    "hpf": {"display_name": "深圳公积金", "sort_order": 100},
    "medical_insurance": {"display_name": "深圳医保", "sort_order": 110},
    "guangfa_credit": {"display_name": "广发银行", "sort_order": 120},
}

ACCOUNT_NATURE_LABELS = {
    "alipay": "支付余额",
    "ccb_7332": "国内储蓄卡",
    "santander": "海外储蓄卡",
    "icbc_6689_cny": "国内储蓄卡",
    "icbc_6689_usd": "海外储蓄卡",
    "wechat_balance": "支付余额",
    "wechat_lqt": "现金管理",
    "ibkr": "美股券商",
    "tonghuashun": "A股券商",
    "tencent_licaitong": "基金/理财平台",
    "huawei_stock": "ESOP",
    "hpf": "公积金",
    "medical_insurance": "医保个人账户",
    "ccb_credit_0453": "人民币信用卡",
    "gf_7443": "人民币信用卡",
    "gf_9711": "美元信用卡",
    "ibkr_in_transit_mxn": "入金在途",
}

REVIEW_ACTIONS = {"approve", "defer_missing"}


class AccountReviewRequest(BaseModel):
    """User decision for the current account reconciliation state."""

    action: str
    note: str | None = None


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


def _view_as_of():
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _load_yaml(primary: Path, fallback: Path) -> dict[str, Any]:
    path = primary if primary.exists() else fallback
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_accounts_config() -> dict[str, Any]:
    return _load_yaml(CONFIG_PATH, LOCAL_CONFIG_PATH)


def _load_bank_config() -> dict[str, Any]:
    return _load_yaml(BANK_CONFIG_PATH, LOCAL_BANK_CONFIG_PATH)


def _excluded_accounts(account_cfg: dict[str, Any]) -> set[str]:
    defaults = account_cfg.get("defaults") or {}
    values: list[Any] = []
    values.extend(account_cfg.get("excluded_accounts") or [])
    values.extend(defaults.get("excluded_accounts") or [])
    return {str(v).strip() for v in values if str(v).strip()}


def _account_root_meta(account_cfg: dict[str, Any], code: str, cfg: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    roots = {**DEFAULT_ACCOUNT_ROOTS, **(account_cfg.get("account_roots") or {})}
    root_code = str(cfg.get("parent_account_code") or code)
    root_cfg = roots.get(root_code) or {}
    return {
        "account_root_code": root_code,
        "account_root_label": root_cfg.get("display_name") or cfg.get("institution") or db.get("display_name") or cfg.get("display_name") or root_code,
        "account_root_order": int(root_cfg.get("sort_order", 999)),
        "subaccount_label": cfg.get("subaccount_label") or cfg.get("display_name") or db.get("display_name") or code,
    }


def _money(amount: Any, currency: str | None, as_of: Any = None, source: str | None = None) -> dict[str, Any] | None:
    if amount in (None, ""):
        return None
    return {
        "amount": Decimal(str(amount)),
        "currency": currency or "CNY",
        "as_of": as_of,
        "source": source,
    }


def _apply_owner_ratio_to_money(balance: dict[str, Any] | None, owner_ratio: Decimal) -> dict[str, Any] | None:
    if not balance:
        return None
    if owner_ratio == Decimal("1"):
        return balance
    gross_amount = Decimal(str(balance.get("amount") or "0"))
    adjusted = dict(balance)
    adjusted["amount"] = gross_amount * owner_ratio
    adjusted["gross_amount"] = gross_amount
    adjusted["owner_ratio"] = owner_ratio
    adjusted["source_amount_basis"] = "owner_adjusted"
    return adjusted


def _review_fingerprint(account: dict[str, Any]) -> str:
    balance = account.get("primary_balance") or {}
    parts = [
        str(account.get("status") or ""),
        str(balance.get("amount") or ""),
        str(balance.get("currency") or account.get("base_currency") or ""),
        str(_norm(balance.get("as_of")) or ""),
        str(balance.get("source") or ""),
    ]
    if "daily_close" in account:
        evidence = {"daily_close": account["daily_close"], "owner_ratio": account.get("owner_ratio"),
                    "amount_cny": balance.get("amount_cny")}
        parts.append(hashlib.sha256(json.dumps(_norm(evidence), sort_keys=True,
                                              separators=(",", ":")).encode("utf-8")).hexdigest())
    return "|".join(parts)


def _daily_close_amount(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("missing_or_invalid_amount")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("missing_or_invalid_amount")
    return amount


def _bind_daily_close_primary(account: dict[str, Any], canonical: dict[str, Any], *,
                              selected_platform: bool) -> None:
    """Bind shared proven amounts without valuing, converting, or owning them again."""
    raw_proof = canonical.get("daily_close")
    proof = raw_proof if isinstance(raw_proof, dict) else {}
    primary = account.get("primary_balance") or {}
    try:
        if not selected_platform:
            raise ValueError("non_platform_primary_preserved")
        if proof.get("status") != "ok":
            raise ValueError("unproven_canonical_balance")
        if (canonical.get("account_code") != "ibkr" or canonical.get("truth_source") != "ibkr_flex"
                or canonical.get("review_status") != "ready_for_review"):
            raise ValueError("canonical_account_scope_mismatch")
        day = _norm(primary.get("as_of"))
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid_primary_date")
        if (any(_norm(value) != day for value in (canonical.get("as_of"), proof.get("as_of"),
                                                 proof.get("requested_as_of")))
                or proof.get("carried") is not False):
            raise ValueError("canonical_date_mismatch")
        base = proof.get("source_base_currency")
        if (base != account.get("base_currency") or base != primary.get("currency")
                or base != canonical.get("base_currency")
                or canonical.get("currency") != "CNY" or proof.get("valuation_currency") != "CNY"):
            raise ValueError("canonical_currency_mismatch")
        if not isinstance(proof.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", proof["source_sha256"]):
            raise ValueError("missing_source_sha256")
        identity = proof.get("identity_evidence") or {}
        if (not isinstance(identity, dict) or identity.get("status") != "ok" or identity.get("as_of") != day
                or not isinstance(proof.get("source_account_id"), str) or not proof["source_account_id"].strip()):
            raise ValueError("unproven_account_identity")
        fx = proof.get("fx_evidence") or {}
        if (not isinstance(fx, dict) or fx.get("status") != "ok" or fx.get("as_of") != day or fx.get("requested_as_of") != day
                or fx.get("currency") != base or fx.get("base_currency") != "CNY"
                or not isinstance(fx.get("source"), str) or not fx["source"].strip()
                or _daily_close_amount(fx.get("rate")) <= 0
                or _daily_close_amount(fx.get("rate")) != _daily_close_amount(proof.get("fx_rate"))):
            raise ValueError("unproven_historical_fx")
        if (_daily_close_amount(proof.get("owner_ratio")) != account["owner_ratio"]
                or account["owner_ratio"] < 0):
            raise ValueError("owner_scope_mismatch")
        amount = _daily_close_amount(proof.get("owned_nav_base"))
        amount_cny = _daily_close_amount(canonical.get("amount_cny"))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raw_reasons = proof.get("reasons")
        reasons = raw_reasons if isinstance(raw_reasons, list) else []
        account["daily_close"] = {**proof, "status": "unconfirmed",
                                 "reasons": [*reasons, str(exc) or "invalid_canonical_balance"]}
        return
    account["primary_balance"] = {"amount": amount, "currency": base, "as_of": day,
                                  "source": "ibkr_flex", "amount_cny": amount_cny}
    account["daily_close"] = dict(proof)


def _ensure_review_table(cur) -> None:
    """Account review tables are provisioned by database schema migrations."""
    return None


def _fetch_latest_reviews(cur) -> dict[str, dict[str, Any]]:
    _ensure_review_table(cur)
    cur.execute(
        """
        SELECT DISTINCT ON (account_code)
               case_id AS id,
               account_code,
               review_action,
               source_status,
               review_fingerprint,
               reviewed_balance,
               currency,
               balance_as_of,
               note,
               reviewed_by,
               reviewed_at
        FROM (
            SELECT
                c.case_id,
                COALESCE(NULLIF(c.raw_json->>'account_code', ''), NULLIF(c.relation_code, '')) AS account_code,
                c.review_action,
                COALESCE(NULLIF(c.raw_json->>'source_status', ''), c.status) AS source_status,
                c.raw_json->>'review_fingerprint' AS review_fingerprint,
                COALESCE(NULLIF(c.raw_json->>'reviewed_balance', '')::numeric, c.amount) AS reviewed_balance,
                c.currency,
                COALESCE(NULLIF(c.raw_json->>'balance_as_of', '')::date, c.case_date) AS balance_as_of,
                c.note,
                c.reviewed_by,
                c.reviewed_at
            FROM pams_reconciliation_case c
            WHERE c.case_type = 'account_review'
        ) reviews
        WHERE account_code IS NOT NULL
        ORDER BY account_code, reviewed_at DESC NULLS LAST, id DESC
        """
    )
    return {str(r["account_code"]): dict(r) for r in cur.fetchall()}


def _apply_review(account: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    if not review:
        account["review_status"] = "unreviewed"
        return account

    original_status = str(account.get("status") or "")
    account["original_status"] = original_status
    account["latest_review"] = review
    current_fingerprint = _review_fingerprint(account)
    stored_fingerprint = str(review.get("review_fingerprint") or "")
    action = str(review.get("review_action") or "")

    if action == "approve" and stored_fingerprint == current_fingerprint:
        account["review_status"] = "approved"
        account["status"] = "approved"
        account["status_label"] = "已核准"
        return account

    if action == "defer_missing" and original_status.startswith("missing") and stored_fingerprint == current_fingerprint:
        account["review_status"] = "missing_deferred"
        account["status"] = "missing_deferred"
        account["status_label"] = "缺数据暂缓"
        return account

    account["review_status"] = "stale"
    account["review_stale_reason"] = "账户余额或状态已变化，需要重新核准"
    return account


def _reconcile_group(account_type: str) -> str:
    if account_type == "bridge":
        return "clearing"
    if account_type == "liability":
        return "liability"
    if account_type == "benefit":
        return "benefit"
    if account_type in {"brokerage", "fund_platform"}:
        return "investment"
    return "cash"


def _balance_basis_label(truth_source: str, account_type: str) -> str:
    if account_type == "bridge":
        return "等待到账后核销"
    return {
        "bank_statement": "以账单余额为准",
        "platform_snapshot": "以平台净值为准",
        "ibkr_flex": "以 IBKR Flex 为准",
        "official_statement": "以官方明细为准",
        "manual": "以手工确认值为准",
    }.get(truth_source, "待确认")


def _review_hint(status: str, truth_source: str, account_type: str) -> str:
    if status == "in_transit":
        return "到账后与目标账户余额核销"
    if status.startswith("missing"):
        return "先补账单、截图或平台快照"
    if account_type == "liability":
        return "核对信用卡账单金额，按负数计入净资产"
    if truth_source in {"platform_snapshot", "ibkr_flex"}:
        return "核对平台净值与持仓快照日期"
    if truth_source in {"bank_statement", "official_statement"}:
        return "核对最新账单余额和日期"
    return "核对手工录入金额和来源"


def _net_statement_balance(value: Any, account_kind: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    amount = Decimal(str(value))
    if account_kind in LIABILITY_ACCOUNT_KINDS:
        return -abs(amount)
    return amount


def _fetch_db_accounts(cur) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT account_code, display_name, base_currency, is_asset, active
        FROM pams_accounts
        """
    )
    return {str(r["account_code"]): dict(r) for r in cur.fetchall()}


def _fetch_statement_balances(cur) -> dict[str, list[dict[str, Any]]]:
    source_rows = source_row_relation(cur)
    cur.execute(
        f"""
        SELECT
            account_code AS profile_code,
            COALESCE(NULLIF(source_system, ''), source_table) AS bank,
            account_code,
            NULL::text AS account_kind,
            NULL::numeric AS statement_balance,
            MAX(event_date) AS statement_end,
            COUNT(*) AS row_count,
            MAX(imported_at) AS imported_at,
            MAX(event_date) AS latest_balance_date,
            NULL::numeric AS latest_balance
        FROM {source_rows} AS source_rows
        WHERE is_bank_statement
          AND account_code IS NOT NULL
        GROUP BY account_code, COALESCE(NULLIF(source_system, ''), source_table)
        ORDER BY account_code
        """
    )
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = list(cur.fetchall())
    cur.execute(
        """
        WITH fact_balance AS (
            SELECT DISTINCT ON (f.account_code)
                   f.account_code AS profile_code,
                   'pams_cashflow_fact'::text AS bank,
                   f.account_code,
                   CASE
                       WHEN COALESCE(a.account_type, '') = 'liability' OR a.is_asset IS FALSE THEN 'credit_card'
                       ELSE COALESCE(a.account_type, '')
                   END AS account_kind,
                   NULL::numeric AS statement_balance,
                   f.event_date AS statement_end,
                   1::bigint AS row_count,
                   f.updated_at AS imported_at,
                   f.event_date AS latest_balance_date,
                   NULLIF(COALESCE(
                       f.raw_json #>> '{statement_row,balance}',
                       f.raw_json #>> '{statement_row,raw_json,balance}',
                       f.raw_json #>> '{balance}'
                   ), '')::numeric AS latest_balance
            FROM pams_cashflow_fact f
            LEFT JOIN pams_accounts a ON a.account_code = f.account_code
            WHERE NULLIF(COALESCE(
                      f.raw_json #>> '{statement_row,balance}',
                      f.raw_json #>> '{statement_row,raw_json,balance}',
                      f.raw_json #>> '{balance}'
                  ), '') IS NOT NULL
              AND COALESCE(LOWER(f.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void', 'duplicate')
            ORDER BY f.account_code, f.event_date DESC, f.cashflow_id DESC
        )
        SELECT *
        FROM fact_balance
        ORDER BY account_code
        """
    )
    rows.extend(cur.fetchall())
    for row in rows:
        item = dict(row)
        raw_balance = item.get("latest_balance")
        if raw_balance is None:
            raw_balance = item.get("statement_balance")
        as_of = item.get("latest_balance_date") or item.get("statement_end")
        item["balance"] = _net_statement_balance(raw_balance, item.get("account_kind"))
        item["balance_as_of"] = as_of
        by_account[str(item["account_code"])].append(item)
    for items in by_account.values():
        items.sort(key=lambda item: (str(item.get("balance_as_of") or ""), str(item.get("imported_at") or "")))
    return by_account


def _fetch_holding_balances(cur) -> dict[str, list[dict[str, Any]]]:
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT h.account_code, h.currency, h.asset_liability, la.account_as_of AS as_of,
               SUM(h.market_value) AS market_value,
               SUM(h.cost) AS cost,
               SUM(h.holding_pnl) AS holding_pnl,
               COUNT(*) AS rows
        FROM pams_holdings_snapshot h
        JOIN latest_account la
          ON la.account_code = h.account_code
         AND la.account_as_of = h.as_of
        GROUP BY h.account_code, h.currency, h.asset_liability, la.account_as_of
        ORDER BY h.account_code, h.asset_liability, h.currency
        """,
        (_view_as_of(),),
    )
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cur.fetchall():
        item = dict(row)
        by_account[str(item["account_code"])].append(item)
    return by_account


def _fetch_latest_fx_rates_by_base(cur) -> dict[str, dict[str, Decimal]]:
    cur.execute(
        """
        SELECT DISTINCT ON (base_currency, quote_currency)
               base_currency, quote_currency, rate
        FROM pams_fx_rates_daily
        ORDER BY base_currency, quote_currency, as_of DESC
        """
    )
    rates: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in cur.fetchall():
        base = str(row["base_currency"] or "").upper()
        quote = str(row["quote_currency"] or "").upper()
        rates[base][quote] = Decimal(str(row["rate"] or "1"))
    return rates


def _fetch_transfer_rollups(cur) -> dict[str, dict[str, list[dict[str, Any]]]]:
    cur.execute(
        """
        SELECT from_account_code, to_account_code, relation_code, case_type AS match_type, currency,
               COUNT(*) AS match_count,
               SUM(amount) AS total_amount,
               MAX(case_date) AS latest_match_date
        FROM pams_reconciliation_case
        WHERE COALESCE(status, 'auto') <> 'rejected'
          AND case_type <> 'account_review'
        GROUP BY from_account_code, to_account_code, relation_code, case_type, currency
        ORDER BY latest_match_date DESC NULLS LAST, relation_code
        """
    )
    rollups: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"in": [], "out": []})
    for row in cur.fetchall():
        item = dict(row)
        from_code = str(item.get("from_account_code") or "")
        to_code = str(item.get("to_account_code") or "")
        if from_code:
            rollups[from_code]["out"].append(item)
        if to_code:
            rollups[to_code]["in"].append(item)
    return rollups


def _primary_platform_balance(
    platform_balances: list[dict[str, Any]],
    base_currency: str,
    fx_rates_by_base: dict[str, dict[str, Decimal]] | None = None,
) -> dict[str, Any] | None:
    if not platform_balances:
        return None
    base = base_currency.upper()
    currency_totals: dict[str, Decimal] = defaultdict(Decimal)
    currency_as_of: dict[str, Any] = {}
    latest_as_of = None
    for row in platform_balances:
        currency = str(row.get("currency") or base).upper()
        currency_totals[currency] += Decimal(str(row.get("market_value") or "0"))
        row_as_of = row.get("as_of")
        if row_as_of is not None:
            if latest_as_of is None or str(row_as_of) > str(latest_as_of):
                latest_as_of = row_as_of
            if currency not in currency_as_of or str(row_as_of) > str(currency_as_of[currency]):
                currency_as_of[currency] = row_as_of
    if len(currency_totals) == 1:
        currency, amount = next(iter(currency_totals.items()))
        return _money(amount, currency, currency_as_of.get(currency) or latest_as_of, "platform_snapshot")

    fx_rates = (fx_rates_by_base or {}).get(base, {})
    if fx_rates or base in currency_totals:
        converted = currency_totals.get(base, Decimal("0"))
        converted_any = base in currency_totals
        missing_material_fx = []
        # Tiny residual cash/rounding balances must not make IBKR fall back to USD-only.
        immaterial_native_balance = Decimal("10")
        for currency, amount in currency_totals.items():
            if currency == base or amount == 0:
                continue
            rate = fx_rates.get(currency)
            if rate is None:
                if abs(amount) <= immaterial_native_balance:
                    continue
                missing_material_fx.append(currency)
                continue
            converted += amount * rate
            converted_any = True
        if converted_any and not missing_material_fx:
            return _money(converted, base_currency, latest_as_of, "platform_snapshot")

    if base in currency_totals:
        return _money(currency_totals[base], base_currency, currency_as_of.get(base) or latest_as_of, "platform_snapshot")
    return None


def _manual_balance(config: dict[str, Any], base_currency: str) -> dict[str, Any] | None:
    raw = config.get("manual_balance") or {}
    if not raw:
        return None
    balance = _money(raw.get("amount"), raw.get("currency") or base_currency, raw.get("as_of"), raw.get("source") or "manual")
    if balance:
        balance["note"] = raw.get("note")
    return balance


def _reconcile_status(
    *,
    account_type: str,
    truth_source: str,
    statement_balance: dict[str, Any] | None,
    platform_balance: dict[str, Any] | None,
    manual_balance: dict[str, Any] | None,
) -> str:
    if account_type == "bridge":
        return "in_transit" if manual_balance else "missing_data"
    if truth_source in {"bank_statement", "official_statement"}:
        return "ready_for_review" if statement_balance or manual_balance else "missing_statement"
    if truth_source in {"platform_snapshot", "ibkr_flex"}:
        return "ready_for_review" if platform_balance else "missing_platform"
    if truth_source == "manual":
        return "ready_for_review" if manual_balance or platform_balance else "missing_manual"
    return "ready_for_review" if statement_balance or platform_balance or manual_balance else "missing_data"


def _status_label(status: str) -> str:
    return {
        "ready_for_review": "待核准",
        "in_transit": "在途",
        "missing_statement": "缺账单",
        "missing_platform": "缺平台数据",
        "missing_manual": "缺手动确认",
        "missing_data": "缺数据",
        "approved": "已核准",
        "missing_deferred": "缺数据暂缓",
    }.get(status, status)


def _pick_primary_balance(
    *,
    truth_source: str,
    account_type: str = "",
    reconcile_method: str = "",
    statement_balance: dict[str, Any] | None,
    platform_balance: dict[str, Any] | None,
    manual_balance: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if truth_source in {"bank_statement", "official_statement"} and statement_balance and manual_balance:
        statement_as_of = str(statement_balance.get("as_of") or "")
        manual_as_of = str(manual_balance.get("as_of") or "")
        return statement_balance if statement_as_of >= manual_as_of else manual_balance
    platform_is_primary = (
        truth_source in {"platform_snapshot", "ibkr_flex"}
        or account_type in {"brokerage", "fund_platform", "cash_management"}
        or reconcile_method == "position_nav"
    )
    if platform_is_primary and platform_balance:
        if not manual_balance:
            return platform_balance
        platform_as_of = str(platform_balance.get("as_of") or "")
        manual_as_of = str(manual_balance.get("as_of") or "")
        if not manual_as_of or platform_as_of > manual_as_of:
            return platform_balance
    if manual_balance:
        return manual_balance
    if truth_source in {"bank_statement", "official_statement"} and statement_balance:
        return statement_balance
    if truth_source in {"platform_snapshot", "ibkr_flex"} and platform_balance:
        return platform_balance
    return statement_balance or platform_balance


def _build_accounts_overview(
    account_cfg: dict[str, Any],
    bank_cfg: dict[str, Any],
    db_accounts: dict[str, dict[str, Any]],
    statement_balances: dict[str, list[dict[str, Any]]],
    holding_balances: dict[str, list[dict[str, Any]]],
    transfer_rollups: dict[str, dict[str, list[dict[str, Any]]]],
    latest_reviews: dict[str, dict[str, Any]],
    fx_rates_by_base: dict[str, dict[str, Decimal]],
    canonical_balances: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    defaults = account_cfg.get("defaults") or {}
    accounts = account_cfg.get("accounts") or {}
    excluded_accounts = _excluded_accounts(account_cfg)
    merged_codes = set(accounts) | set(db_accounts) | set(statement_balances) | set(holding_balances)
    result: list[dict[str, Any]] = []

    for code in sorted(merged_codes):
        if code in excluded_accounts:
            continue
        cfg = dict(accounts.get(code) or {})
        db = db_accounts.get(code) or {}
        if (
            cfg.get("active") is False
            or cfg.get("exclude_from_reconcile") is True
            or (db.get("active") is False and cfg.get("active") is not True)
        ):
            continue
        account_type = str(cfg.get("account_type") or ("liability" if db.get("is_asset") is False else "funding"))
        base_currency = str(cfg.get("base_currency") or db.get("base_currency") or defaults.get("base_currency") or "CNY")
        truth_source = str(cfg.get("truth_source") or "manual")
        owner_ratio = Decimal(str(cfg.get("owner_ratio", defaults.get("owner_ratio", 1))))
        include_in_nav = bool(cfg.get("include_in_nav", defaults.get("include_in_nav", True)))

        statement_items = statement_balances.get(code, [])
        statement_balance = None
        if statement_items:
            latest = statement_items[-1]
            statement_balance = _money(latest.get("balance"), base_currency, latest.get("balance_as_of"), "statement")

        platform_items = holding_balances.get(code, [])
        platform_balance = _primary_platform_balance(platform_items, base_currency, fx_rates_by_base)
        manual_balance = _manual_balance(cfg, base_currency)
        statement_balance_gross = statement_balance
        platform_balance_gross = platform_balance
        manual_balance_gross = manual_balance
        statement_balance = _apply_owner_ratio_to_money(statement_balance, owner_ratio)
        platform_balance = _apply_owner_ratio_to_money(platform_balance, owner_ratio)
        manual_balance = _apply_owner_ratio_to_money(manual_balance, owner_ratio)
        primary_balance = _pick_primary_balance(
            truth_source=truth_source,
            account_type=account_type,
            reconcile_method=str(cfg.get("reconcile_method") or "balance"),
            statement_balance=statement_balance,
            platform_balance=platform_balance,
            manual_balance=manual_balance,
        )
        status = _reconcile_status(
            account_type=account_type,
            truth_source=truth_source,
            statement_balance=statement_balance,
            platform_balance=platform_balance,
            manual_balance=manual_balance,
        )
        reconcile_group = _reconcile_group(account_type)
        group_meta = RECONCILE_GROUPS[reconcile_group]
        root_meta = _account_root_meta(account_cfg, code, cfg, db)

        account = {
                "account_code": code,
                "display_name": cfg.get("display_name") or db.get("display_name") or code,
                "parent_account_code": cfg.get("parent_account_code"),
                **root_meta,
                "account_type": account_type,
                "reconcile_group": reconcile_group,
                "reconcile_group_label": group_meta["label"],
                "reconcile_group_order": group_meta["order"],
                "account_nature_label": cfg.get("nature_label") or ACCOUNT_NATURE_LABELS.get(code, "账户"),
                "institution": cfg.get("institution"),
                "base_currency": base_currency,
                "owner_ratio": owner_ratio,
                "include_in_nav": include_in_nav,
                "truth_source": truth_source,
                "truth_source_label": TRUTH_SOURCE_LABELS.get(truth_source, truth_source),
                "balance_basis_label": _balance_basis_label(truth_source, account_type),
                "reconcile_method": cfg.get("reconcile_method") or "balance",
                "status": status,
                "status_label": _status_label(status),
                "review_hint": _review_hint(status, truth_source, account_type),
                "role": cfg.get("role"),
                "capital_base": cfg.get("capital_base"),
                "personal_capital": cfg.get("personal_capital"),
                "funding_source": cfg.get("funding_source"),
                "primary_balance": primary_balance,
                "statement_balance": statement_balance,
                "statement_balance_gross": statement_balance_gross,
                "statement_profiles": statement_items,
                "platform_balance": platform_balance,
                "platform_balance_gross": platform_balance_gross,
                "platform_balances": platform_items,
                "manual_balance": manual_balance,
                "manual_balance_gross": manual_balance_gross,
                "transfer_in": transfer_rollups.get(code, {}).get("in", []),
                "transfer_out": transfer_rollups.get(code, {}).get("out", []),
                "active": bool(db.get("active", True)),
            }
        if canonical_balances is not None and code == "ibkr" and truth_source == "ibkr_flex":
            _bind_daily_close_primary(account, canonical_balances.get(code) or {},
                                     selected_platform=primary_balance is not None and primary_balance is platform_balance)
        result.append(_apply_review(account, latest_reviews.get(code)))

    status_order = {
        "ready_for_review": 10,
        "missing_statement": 20,
        "missing_platform": 20,
        "missing_manual": 20,
        "missing_data": 20,
        "in_transit": 30,
        "approved": 80,
        "missing_deferred": 90,
    }
    return sorted(
        result,
        key=lambda r: (
            r.get("account_root_order", 999),
            r.get("account_root_code") or "",
            status_order.get(str(r.get("status")), 50),
            r.get("institution") or "",
            r["account_code"],
        ),
    )


def _overview_payload() -> dict[str, Any]:
    from domain.brokers.ibkr import ibkr_daily_close_read
    from domain.investments import portfolio_state

    account_cfg = _load_accounts_config()
    bank_cfg = _load_bank_config()
    canonical_balances = None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            db_accounts = _fetch_db_accounts(cur)
            statement_balances = _fetch_statement_balances(cur)
            holding_balances = _fetch_holding_balances(cur)
            fx_rates_by_base = _fetch_latest_fx_rates_by_base(cur)
            transfer_rollups = _fetch_transfer_rollups(cur)
            latest_reviews = _fetch_latest_reviews(cur)
            if ibkr_daily_close_read.configured():
                canonical_balances = {}
                for row in portfolio_state.load_account_balances(cur, "CNY", account_cfg):
                    code = row["account_code"]
                    if code in canonical_balances:
                        canonical_balances[code] = {"daily_close": {"status": "unconfirmed",
                                                                  "reasons": ["duplicate_canonical_account"]}}
                    else:
                        canonical_balances[code] = row

    accounts = _build_accounts_overview(
        account_cfg,
        bank_cfg,
        db_accounts,
        statement_balances,
        holding_balances,
        transfer_rollups,
        latest_reviews,
        fx_rates_by_base,
        canonical_balances=canonical_balances,
    )
    summary = {
        "total_accounts": len(accounts),
        "ready_for_review": sum(1 for a in accounts if a["status"] == "ready_for_review"),
        "in_transit": sum(1 for a in accounts if a["status"] == "in_transit"),
        "missing_data": sum(
            1
            for a in accounts
            if a["status"] in {"missing_statement", "missing_platform", "missing_manual", "missing_data"}
        ),
        "approved": sum(1 for a in accounts if a["status"] == "approved"),
        "missing_deferred": sum(1 for a in accounts if a["status"] == "missing_deferred"),
        "stale_review": sum(1 for a in accounts if a.get("review_status") == "stale"),
        "include_in_nav": sum(1 for a in accounts if a.get("include_in_nav")),
    }
    return {
        "as_of": datetime.utcnow(),
        "summary": summary,
        "accounts": accounts,
        "account_relationships": bank_cfg.get("account_relationships", []) or [],
        "account_entities": bank_cfg.get("account_entities", {}) or {},
    }


@router.get("/accounts/overview")
def accounts_overview():
    """Return account master data and current reconciliation inputs."""
    return _norm(_overview_payload())


@router.post("/accounts/{account_code}/review")
def review_account(account_code: str, req: AccountReviewRequest):
    """Persist the user's decision for the current account reconciliation state."""
    action = (req.action or "").strip()
    if action not in REVIEW_ACTIONS:
        raise HTTPException(status_code=400, detail="Unsupported account review action")

    payload = _overview_payload()
    account = next((a for a in payload["accounts"] if a["account_code"] == account_code), None)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account {account_code} is not in reconciliation scope")

    status = str(account.get("status") or "")
    primary = account.get("primary_balance") or {}
    if action == "approve":
        if status != "ready_for_review":
            raise HTTPException(status_code=400, detail="Only ready-for-review accounts can be approved")
        if primary.get("amount") in (None, ""):
            raise HTTPException(status_code=400, detail="Account has no balance to approve")
    elif action == "defer_missing" and not status.startswith("missing"):
        raise HTTPException(status_code=400, detail="Only missing-data accounts can be deferred")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _ensure_review_table(cur)
            cur.execute(
                """
                INSERT INTO pams_reconciliation_case (
                    case_type, relation_code, target_ref_table, target_ref_id,
                    amount, currency, case_date, status, review_action,
                    reviewed_by, reviewed_at, note, raw_json
                )
                VALUES (
                    'account_review', %s, 'pams_accounts', %s,
                    %s, %s, COALESCE(%s::date, CURRENT_DATE), %s, %s,
                    'user', now(), %s,
                    jsonb_build_object(
                        'account_code', %s,
                        'review_action', %s,
                        'source_status', %s,
                        'review_fingerprint', %s,
                        'reviewed_balance', %s,
                        'currency', %s,
                        'balance_as_of', %s,
                        'reviewed_by', 'user',
                        'reviewed_at', now()
                    )
                )
                """,
                (
                    account_code,
                    account_code,
                    primary.get("amount") or 0,
                    primary.get("currency") or account.get("base_currency"),
                    primary.get("as_of"),
                    status,
                    action,
                    req.note,
                    account_code,
                    action,
                    status,
                    _review_fingerprint(account),
                    primary.get("amount"),
                    primary.get("currency") or account.get("base_currency"),
                    primary.get("as_of"),
                ),
            )
        conn.commit()

    return _norm(_overview_payload())
