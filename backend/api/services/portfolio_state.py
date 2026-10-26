"""Unified read-only portfolio state assembly.

This service is the first executable version of the PortfolioState contract in
``docs/PAMS_DATA_CONTRACT.md``.  It intentionally reads existing tables and
configuration without changing current valuation logic, so old endpoints can be
migrated one by one while keeping their response fields stable.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date as Date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import yaml

from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from services.quote_valuation import quote_valuation_cte, snapshot_tables


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml"))
LOCAL_CONFIG_PATH = ROOT / "config" / "accounts.yaml"

PORTFOLIO_STATE_KEYS = [
    "as_of",
    "base_currency",
    "account_balances",
    "investment_positions",
    "investment_transactions",
    "cash_transactions",
    "transfer_matches",
    "fx_rates",
    "valuation_series",
    "risk_snapshot",
    "data_quality",
]

DATA_QUALITY_PAYLOAD_KEYS = [
    "as_of",
    "base_currency",
    "data_quality",
]

ACCOUNT_BALANCE_FIELDS = [
    "account_code",
    "parent_account_code",
    "display_name",
    "account_type",
    "base_currency",
    "amount",
    "currency",
    "amount_cny",
    "as_of",
    "truth_source",
    "review_status",
    "include_in_nav",
]

INVESTMENT_POSITION_FIELDS = [
    "account_code",
    "asset_id",
    "symbol",
    "asset_name",
    "asset_type",
    "quantity",
    "market_value",
    "currency",
    "market_value_cny",
    "cost_cny",
    "holding_pnl_cny",
    "as_of",
    "source_type",
]

INVESTMENT_TRANSACTION_FIELDS = [
    "account_code",
    "trade_date",
    "symbol",
    "name",
    "side",
    "quantity",
    "price",
    "gross_amount",
    "fees",
    "currency",
    "source",
    "source_key",
]

VALUATION_SERIES_FIELDS = [
    "date",
    "assets_cny",
    "liabilities_cny",
    "net_worth_cny",
    "investment_value_cny",
    "investment_cost_cny",
    "investment_pnl_cny",
    "investment_return_rate",
]

RISK_SNAPSHOT_FIELDS = [
    "total_nav",
    "gross_exposure_ratio",
    "negative_cash_ratio",
    "account_exposure",
    "position_exposure",
    "industry_exposure",
    "style_exposure",
    "factor_exposure",
    "stress_loss",
    "rebalance_suggestions",
]

DATA_QUALITY_FIELDS = [
    "coverage",
    "freshness",
    "missing",
]

LIABILITY_ACCOUNT_KINDS = {"credit_card"}
NON_INVESTMENT_ACCOUNT_TYPES = {
    "benefit",
    "bridge",
    "credit_card",
    "funding",
    "liability",
    "payment",
    "receivable",
    "wallet",
}


def get_conn():
    """Create a PostgreSQL connection using PAMS environment variables."""
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


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


def _view_as_of() -> Date:
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _load_accounts_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else LOCAL_CONFIG_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS table_name", (table_name,))
    row = cur.fetchone()
    return bool(row and row.get("table_name"))


def _latest_fx_rates(cur, base_currency: str) -> dict[str, Decimal]:
    if not _table_exists(cur, "pams_fx_rates_daily"):
        return {base_currency.upper(): Decimal("1")}
    cur.execute(
        """
        SELECT DISTINCT ON (quote_currency) quote_currency, rate
        FROM pams_fx_rates_daily
        WHERE base_currency = %s
        ORDER BY quote_currency, as_of DESC
        """,
        (base_currency,),
    )
    rates = {str(r["quote_currency"]).upper(): _dec(r["rate"]) for r in cur.fetchall()}
    rates[base_currency.upper()] = Decimal("1")
    return rates


def _fx_rate(rates: dict[str, Decimal], currency: str, base_currency: str) -> Decimal:
    quote = str(currency or base_currency).upper()
    if quote == base_currency.upper():
        return Decimal("1")
    return rates.get(quote, Decimal("1"))


def _excluded_accounts(account_cfg: dict[str, Any]) -> set[str]:
    defaults = account_cfg.get("defaults") or {}
    values: list[Any] = []
    values.extend(account_cfg.get("excluded_accounts") or [])
    values.extend(defaults.get("excluded_accounts") or [])
    return {str(v).strip() for v in values if str(v).strip()}


def _owner_ratio(account_cfg: dict[str, Any], account_code: str) -> Decimal:
    defaults = account_cfg.get("defaults") or {}
    cfg = (account_cfg.get("accounts") or {}).get(account_code) or {}
    return Decimal(str(cfg.get("owner_ratio", defaults.get("owner_ratio", 1))))


def _account_initial_capital_cny(account_cfg: dict[str, Any], account_code: str) -> Decimal | None:
    cfg = (account_cfg.get("accounts") or {}).get(account_code) or {}
    if cfg.get("capital_base") not in (None, ""):
        return Decimal(str(cfg["capital_base"]))
    if cfg.get("personal_capital") not in (None, ""):
        owner_ratio = _owner_ratio(account_cfg, account_code)
        personal_capital = Decimal(str(cfg["personal_capital"]))
        return personal_capital / owner_ratio if owner_ratio else personal_capital
    return None


def _is_investment_account(account_cfg: dict[str, Any], account_code: str) -> bool:
    cfg = (account_cfg.get("accounts") or {}).get(account_code) or {}
    if cfg.get("include_in_investments") is not None:
        return bool(cfg.get("include_in_investments"))
    account_type = str(cfg.get("account_type") or "").lower()
    if account_type in NON_INVESTMENT_ACCOUNT_TYPES:
        return False
    return True


def _net_statement_balance(value: Any, account_kind: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    amount = _dec(value)
    if account_kind in LIABILITY_ACCOUNT_KINDS:
        return -abs(amount)
    return amount


def _statement_balances(cur) -> dict[str, dict[str, Any]]:
    if not (
        _table_exists(cur, "pams_bank_statement_imports")
        and _table_exists(cur, "pams_bank_statement_rows")
    ):
        return {}

    balances: dict[str, dict[str, Any]] = {}
    cur.execute(
        """
        WITH latest_import AS (
            SELECT DISTINCT ON (profile_code)
                   profile_code, account_code, account_kind, statement_end,
                   statement_balance, imported_at
            FROM pams_bank_statement_imports
            ORDER BY profile_code, imported_at DESC, id DESC
        ),
        latest_balance_row AS (
            SELECT DISTINCT ON (profile_code)
                   profile_code, transaction_date, balance
            FROM pams_bank_statement_rows
            WHERE balance IS NOT NULL
            ORDER BY profile_code, transaction_date DESC, id DESC
        )
        SELECT i.profile_code, i.account_code, i.account_kind,
               i.statement_balance, i.statement_end, i.imported_at,
               r.transaction_date AS latest_balance_date,
               r.balance AS latest_balance
        FROM latest_import i
        LEFT JOIN latest_balance_row r ON r.profile_code = i.profile_code
        ORDER BY i.account_code, i.profile_code
        """
    )
    rows = cur.fetchall()

    if _table_exists(cur, "pams_cashflow_fact"):
        cur.execute(
            """
            WITH fact_balance AS (
                SELECT DISTINCT ON (f.account_code)
                       f.account_code,
                       CASE
                           WHEN COALESCE(a.account_type, '') = 'liability' OR a.is_asset IS FALSE THEN 'credit_card'
                           ELSE COALESCE(a.account_type, '')
                       END AS account_kind,
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
            SELECT account_code AS profile_code,
                   account_code,
                   account_kind,
                   NULL::numeric AS statement_balance,
                   latest_balance_date AS statement_end,
                   NULL::timestamp AS imported_at,
                   latest_balance_date,
                   latest_balance
            FROM fact_balance
            ORDER BY account_code
            """
        )
        rows = [*rows, *cur.fetchall()]

    for row in rows:
        amount = _net_statement_balance(
            row.get("latest_balance") if row.get("latest_balance") is not None else row.get("statement_balance"),
            row.get("account_kind"),
        )
        if amount is None:
            continue
        account_code = str(row["account_code"])
        as_of = row.get("latest_balance_date") or row.get("statement_end")
        current = balances.get(account_code)
        if current and str(current.get("as_of") or "") >= str(as_of or ""):
            continue
        balances[account_code] = {
            "amount": amount,
            "as_of": as_of,
            "source": "statement",
        }
    return balances


def _platform_balances(cur, base_currency: str, fx_rates: dict[str, Decimal]) -> dict[str, dict[str, Any]]:
    tables = snapshot_tables(cur)
    if not tables.get("table_name"):
        return {}
    quote_overlay = quote_valuation_cte(bool(tables.get("quote_available")))
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, source_holdings AS (
            SELECT s.* FROM pams_holdings_snapshot s JOIN latest_account la
              ON la.account_code = s.account_code AND la.account_as_of = s.as_of
        ), {quote_overlay}
        SELECT h.account_code, h.currency, h.as_of,
               SUM(CASE WHEN h.asset_liability = '负债' THEN -ABS(h.market_value) ELSE h.market_value END) AS market_value
        FROM h
        GROUP BY h.account_code, h.currency, h.as_of
        ORDER BY h.account_code, h.currency
        """,
        (_view_as_of(),),
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        account_code = str(row["account_code"])
        currency = str(row.get("currency") or base_currency).upper()
        amount_cny = _dec(row.get("market_value")) * _fx_rate(fx_rates, currency, base_currency)
        item = grouped.setdefault(
            account_code,
            {"amount": Decimal("0"), "currency": base_currency, "as_of": row.get("as_of"), "source": "platform_snapshot"},
        )
        item["amount"] += amount_cny
        item["as_of"] = max(item.get("as_of"), row.get("as_of")) if item.get("as_of") and row.get("as_of") else row.get("as_of")
    return grouped


def _manual_balance(cfg: dict[str, Any], base_currency: str) -> dict[str, Any] | None:
    raw = cfg.get("manual_balance") or {}
    if not raw:
        return None
    return {
        "amount": _dec(raw.get("amount")),
        "currency": str(raw.get("currency") or base_currency).upper(),
        "as_of": raw.get("as_of"),
        "source": raw.get("source") or "manual",
    }


def _rolled_forward_account_balance(
    cur,
    *,
    account_cfg: dict[str, Any],
    account_code: str,
    manual_balance: dict[str, Any] | None,
    owner_ratio: Decimal,
) -> dict[str, Any] | None:
    """Use the shared balance basis when positions omit brokerage cash."""
    if account_code != "tonghuashun" or not manual_balance:
        return None

    try:
        from services.account_balance import load_account_balance_basis

        manual_currency = str(manual_balance.get("currency") or "CNY").upper()
        if manual_currency != "CNY":
            return None
        manual_amount = _dec(manual_balance.get("amount"))
        # Keep legacy capital validation and its failure fallback.
        _account_initial_capital_cny(account_cfg, account_code)
        payload = load_account_balance_basis(
            cur,
            account_code=account_code,
            owner_ratio=owner_ratio,
            net_liquidation_gross=manual_amount,
            net_liquidation=manual_amount * owner_ratio,
            net_liquidation_as_of=manual_balance.get("as_of"),
        )
    except Exception:
        return None

    if not payload or payload.get("net_personal") in (None, ""):
        return None
    net_value = _dec(payload.get("net_personal"))
    as_of = payload.get("as_of")
    as_of = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    return {
        "amount": net_value,
        "currency": "CNY",
        "amount_cny": net_value,
        "as_of": as_of or manual_balance.get("as_of"),
        "source": payload.get("net_liquidation_source") or "rolled_forward_account_performance",
    }


def _apply_daily_close_balance(cur, row: dict[str, Any], *, base_currency: str, owner_ratio: Decimal,
                              selected_source: str | None = None) -> None:
    from services import ibkr_daily_close_read as daily_close
    from services.valuation_nav import daily_close_account_identity, daily_close_nav_bucket, daily_close_value_metadata

    if (row["account_code"] != "ibkr" or row.get("truth_source") != "ibkr_flex"
            or selected_source not in {None, "platform_snapshot"} or not daily_close.configured()):
        return
    try:
        day = Date.fromisoformat(str(row["as_of"])[:10])
    except (TypeError, ValueError):
        row["daily_close"] = {"status": "unconfirmed", "reasons": ["missing_account_date"]}
        return
    close = daily_close.read_close(as_of=day)
    if close.get("status") != "ok" or close.get("as_of") != day.isoformat():
        row["daily_close"] = {"status": "unconfirmed", "as_of": day.isoformat(),
                              "reasons": close.get("reasons") or ["closing_scope_mismatch"]}
        return
    identity = daily_close_account_identity(cur, source_account_id=close["account_id"], as_of=day)
    if identity["status"] != "ok":
        row["daily_close"] = {"status": "unconfirmed", "as_of": day.isoformat(),
                              "reasons": identity["reasons"], "identity_evidence": identity}
        return
    fx = daily_close.historical_fx(cur, currency=close["base_currency"], base_currency=base_currency, as_of=day)
    rate = fx.get("rate") if fx.get("status") == "ok" else None
    bucket = daily_close_nav_bucket(close, rate=rate, owner_ratio=owner_ratio)
    if bucket is None or rate is None:
        row["daily_close"] = {"status": "unconfirmed", "as_of": day.isoformat(),
                              "fx_evidence": {k: str(v) if isinstance(v, Decimal) else v for k, v in fx.items()},
                              "reasons": fx.get("reasons") if rate is None else ["missing_gross_nav_proof"]}
        return
    metadata = daily_close_value_metadata(close, rate=rate, owner_ratio=owner_ratio,
                                         legacy_value=_dec(row["amount_cny"]), fx_evidence=fx)
    metadata.update({"valuation_currency": base_currency, "assets_cny": str(bucket["assets"]),
                     "liabilities_cny": str(bucket["liabilities"]), "identity_evidence": identity})
    row.update(amount=bucket["net_worth"], currency=base_currency, amount_cny=bucket["net_worth"],
               daily_close=metadata)


def load_account_balances(cur, base_currency: str, account_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Load account-level balances selected from manual, statement, or platform sources."""
    accounts = account_cfg.get("accounts") or {}
    defaults = account_cfg.get("defaults") or {}
    excluded = _excluded_accounts(account_cfg)
    fx_rates = _latest_fx_rates(cur, base_currency)
    statement = _statement_balances(cur)
    platform = _platform_balances(cur, base_currency, fx_rates)
    codes = sorted((set(accounts) | set(statement) | set(platform)) - excluded)
    rows: list[dict[str, Any]] = []

    for code in codes:
        cfg = accounts.get(code) or {}
        if cfg.get("active") is False or cfg.get("exclude_from_reconcile") is True:
            continue
        account_type = str(cfg.get("account_type") or "funding")
        account_base = str(cfg.get("base_currency") or defaults.get("base_currency") or base_currency).upper()
        truth_source = str(cfg.get("truth_source") or "manual")
        include_in_nav = bool(cfg.get("include_in_nav", defaults.get("include_in_nav", True)))
        owner_ratio = _owner_ratio(account_cfg, code)

        manual = _manual_balance(cfg, account_base)
        platform_selected = platform.get(code)
        selected = None
        if truth_source in {"bank_statement", "official_statement"} and code in statement:
            statement_selected = {**statement[code], "currency": account_base}
            if manual and str(manual.get("as_of") or "") > str(statement_selected.get("as_of") or ""):
                selected = manual
            else:
                selected = statement_selected
        elif (
            platform_selected
            and (truth_source in {"platform_snapshot", "ibkr_flex"} or account_type in {"brokerage", "fund_platform"})
            and (
                not manual
                or str(platform_selected.get("as_of") or "") >= str(manual.get("as_of") or "")
            )
        ):
            selected = platform_selected
        elif manual:
            selected = manual
        else:
            selected = statement.get(code) or platform.get(code)
            if selected and "currency" not in selected:
                selected = {**selected, "currency": account_base}

        if not selected:
            amount = Decimal("0")
            currency = account_base
            amount_cny = Decimal("0")
            as_of = None
            review_status = "missing_data"
        else:
            amount = _dec(selected.get("amount"))
            currency = str(selected.get("currency") or account_base).upper()
            if account_type == "liability":
                amount = -abs(amount)
            amount *= owner_ratio
            amount_cny = amount * _fx_rate(fx_rates, currency, base_currency)
            as_of = selected.get("as_of")
            review_status = "ready_for_review"
            rolled = _rolled_forward_account_balance(
                cur,
                account_cfg=account_cfg,
                account_code=code,
                manual_balance=manual,
                owner_ratio=owner_ratio,
            )
            if rolled:
                amount = _dec(rolled.get("amount"))
                currency = str(rolled.get("currency") or base_currency).upper()
                amount_cny = _dec(rolled.get("amount_cny"))
                as_of = rolled.get("as_of")

        row: dict[str, Any] = {
            "account_code": code,
            "parent_account_code": cfg.get("parent_account_code"),
            "display_name": cfg.get("display_name") or code,
            "account_type": account_type,
            "base_currency": account_base,
            "amount": amount,
            "currency": currency,
            "amount_cny": amount_cny,
            "as_of": as_of,
            "truth_source": truth_source,
            "review_status": review_status,
            "include_in_nav": include_in_nav,
        }
        _apply_daily_close_balance(cur, row, base_currency=base_currency, owner_ratio=owner_ratio,
                                  selected_source=selected.get("source") if selected else None)
        rows.append(row)
    return rows


def _asset_type(row: dict[str, Any]) -> str:
    if str(row.get("asset_liability") or "") == "负债":
        return "liability"
    major = str(row.get("major_category") or "")
    minor = str(row.get("minor_category") or "")
    if "美股" in major:
        return "us_stock"
    if "A股" in major and "基金" in major:
        return "a_share_fund"
    if "A股" in major:
        return "a_share_stock"
    if "基金" in major:
        return "fund"
    if "贵金属" in major or "黄金" in major or "黄金" in minor:
        return "precious_metal"
    if "现金" in major or "现金" in minor or "公积金" in major or "公积金" in minor or "医保" in major or "医保" in minor:
        return "cash"
    if "ESOP" in major or "股份" in minor:
        return "esop"
    return "manual_asset"


BROKERAGE_SNAPSHOT_CASH_TYPES = {"brokerage", "fund_platform"}
CASH_CARRY_FORWARD_LOOKBACK_DAYS = 14


def _is_cash_like_holding(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    name = str(row.get("name") or "")
    asset_liability = str(row.get("asset_liability") or "")
    major = str(row.get("major_category") or "")
    minor = str(row.get("minor_category") or "")
    return (
        symbol.startswith("CASH_")
        or name.startswith("Cash ")
        or asset_liability == "负债"
        or "现金" in major
        or "现金" in minor
        or "借款" in major
        or "借款" in minor
    )


def _cash_like_carry_forward_rows(
    cur,
    *,
    account_cfg: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    excluded_accounts: set[str],
) -> list[dict[str, Any]]:
    """Carry forward broker cash/liability rows when a partial market refresh omits them."""
    accounts = account_cfg.get("accounts") or {}
    account_as_of: dict[str, Any] = {}
    account_has_rows: set[str] = set()
    account_has_cash_like: set[str] = set()

    for row in selected_rows:
        account_code = str(row.get("account_code") or "")
        if not account_code:
            continue
        account_has_rows.add(account_code)
        row_as_of = row.get("as_of")
        current_as_of = account_as_of.get(account_code)
        if row_as_of and (not current_as_of or str(row_as_of) > str(current_as_of)):
            account_as_of[account_code] = row_as_of
        if _is_cash_like_holding(row):
            account_has_cash_like.add(account_code)

    targets: list[tuple[str, Any]] = []
    for account_code in sorted(account_has_rows - account_has_cash_like):
        if account_code in excluded_accounts or not _is_investment_account(account_cfg, account_code):
            continue
        cfg = accounts.get(account_code) or {}
        account_type = str(cfg.get("account_type") or "").lower()
        if account_type not in BROKERAGE_SNAPSHOT_CASH_TYPES:
            continue
        selected_as_of = account_as_of.get(account_code) or _view_as_of()
        targets.append((account_code, selected_as_of))

    if not targets:
        return []

    values_sql = ", ".join(["(%s::text, %s::date)"] * len(targets))
    params: list[Any] = []
    for account_code, selected_as_of in targets:
        params.extend([account_code, selected_as_of])

    cur.execute(
        f"""
        WITH targets(account_code, selected_as_of) AS (
            VALUES {values_sql}
        )
        SELECT DISTINCT ON (
               h.account_code,
               COALESCE(NULLIF(h.symbol, ''), h.name),
               h.currency,
               h.asset_liability
        )
               h.*
        FROM pams_holdings_snapshot h
        JOIN targets t ON t.account_code = h.account_code
        WHERE h.as_of < t.selected_as_of
          AND h.as_of >= t.selected_as_of - INTERVAL '{CASH_CARRY_FORWARD_LOOKBACK_DAYS} days'
          AND (
              UPPER(COALESCE(h.symbol, '')) LIKE 'CASH\\_%%' ESCAPE '\\'
              OR h.name LIKE 'Cash %%'
          )
        ORDER BY h.account_code,
                 COALESCE(NULLIF(h.symbol, ''), h.name),
                 h.currency,
                 h.asset_liability,
                 h.as_of DESC,
                 h.id DESC
        """,
        params,
    )
    carried: list[dict[str, Any]] = []
    for row in cur.fetchall():
        item = dict(row)
        item["__cash_carry_forward"] = True
        carried.append(item)
    return carried


def load_investment_positions(
    cur,
    base_currency: str,
    account_cfg: dict[str, Any],
    account_balances: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load latest holding rows normalized to investment position contract fields."""
    tables = snapshot_tables(cur)
    if not tables.get("table_name"):
        return []
    excluded = _excluded_accounts(account_cfg)
    fx_rates = _latest_fx_rates(cur, base_currency)
    quote_overlay = quote_valuation_cte(bool(tables.get("quote_available")))
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, source_holdings AS (
            SELECT s.* FROM pams_holdings_snapshot s JOIN latest_account la
              ON la.account_code = s.account_code AND la.account_as_of = s.as_of
        ), {quote_overlay}
        SELECT h.* FROM h
        WHERE NOT (h.account_code = ANY(%s::text[]))
        ORDER BY h.account_code, h.major_category, h.market_value DESC NULLS LAST
        """,
        (_view_as_of(), sorted(excluded)),
    )
    snapshot_rows = [dict(row) for row in cur.fetchall()]
    carried_cash_rows = _cash_like_carry_forward_rows(
        cur,
        account_cfg=account_cfg,
        selected_rows=snapshot_rows,
        excluded_accounts=excluded,
    )
    cash_carry_forward_accounts = {
        str(row.get("account_code") or "") for row in carried_cash_rows if row.get("account_code")
    }
    snapshot_rows.extend(carried_cash_rows)
    rows: list[dict[str, Any]] = []
    account_position_value_cny: dict[str, Decimal] = {}
    account_position_as_of: dict[str, Any] = {}
    for item in snapshot_rows:
        account_code = str(item.get("account_code") or "")
        if not _is_investment_account(account_cfg, account_code):
            continue
        owner_ratio = _owner_ratio(account_cfg, account_code)
        currency = str(item.get("currency") or base_currency).upper()
        rate = _fx_rate(fx_rates, currency, base_currency)
        market_value = _dec(item.get("market_value")) * owner_ratio
        cost = _dec(item.get("cost")) * owner_ratio
        pnl = _dec(item.get("holding_pnl")) * owner_ratio
        symbol = item.get("symbol")
        rows.append(
            {
                "account_code": account_code,
                "asset_id": symbol or item.get("name") or account_code,
                "symbol": symbol,
                "asset_name": item.get("name") or symbol or account_code,
                "asset_type": _asset_type(item),
                "quantity": item.get("quantity"),
                "market_value": market_value,
                "currency": currency,
                "market_value_cny": market_value * rate,
                "cost_cny": cost * rate,
                "holding_pnl_cny": pnl * rate,
                "as_of": item.get("as_of"),
                "source_type": "holdings_snapshot_cash_carry_forward"
                if item.get("__cash_carry_forward")
                else "holdings_snapshot",
            }
        )
        account_position_value_cny[account_code] = (
            account_position_value_cny.get(account_code, Decimal("0")) + market_value * rate
        )
        row_as_of = item.get("as_of")
        current_as_of = account_position_as_of.get(account_code)
        if row_as_of and (not current_as_of or str(row_as_of) > str(current_as_of)):
            account_position_as_of[account_code] = row_as_of
    rows.extend(
        _synthetic_broker_cash_positions(
            base_currency=base_currency,
            account_cfg=account_cfg,
            fx_rates=fx_rates,
            account_position_value_cny=account_position_value_cny,
            account_position_as_of=account_position_as_of,
            account_balances=account_balances,
            skip_accounts=cash_carry_forward_accounts,
        )
    )
    return rows


def _synthetic_broker_cash_positions(
    *,
    base_currency: str,
    account_cfg: dict[str, Any],
    fx_rates: dict[str, Decimal],
    account_position_value_cny: dict[str, Decimal],
    account_position_as_of: dict[str, Any] | None = None,
    account_balances: list[dict[str, Any]] | None = None,
    skip_accounts: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Represent account-level broker cash when snapshots only list securities.

    Some brokerage snapshots, notably Tonghuashun, provide stock positions while
    the account overview carries the net liquidation value including cash. The
    residual belongs in the investment position stream as cash/active balance so
    analysis and risk views do not silently lose it.
    """
    accounts = account_cfg.get("accounts") or {}
    defaults = account_cfg.get("defaults") or {}
    balances_by_account = {
        str(row.get("account_code")): row
        for row in (account_balances or [])
        if row.get("include_in_nav", True) and row.get("amount_cny") not in (None, "")
    }
    out: list[dict[str, Any]] = []
    skip_accounts = skip_accounts or set()
    for account_code, cfg in accounts.items():
        if account_code in skip_accounts:
            continue
        if not _is_investment_account(account_cfg, account_code):
            continue
        account_type = str(cfg.get("account_type") or "").lower()
        if account_type not in {"brokerage", "fund_platform"}:
            continue
        positions_as_of = (account_position_as_of or {}).get(account_code)
        balance = balances_by_account.get(account_code)
        # Account accruals and the FX bridge are not brokerage cash positions.
        if account_code == "ibkr" and balance and (balance.get("daily_close") or {}).get("status") == "ok":
            continue
        source_as_of = balance.get("as_of") if balance else None
        if balance:
            currency = str(balance.get("currency") or cfg.get("base_currency") or base_currency).upper()
            rate = _fx_rate(fx_rates, currency, base_currency)
            net_value_cny = _dec(balance.get("amount_cny"))
        else:
            manual = _manual_balance(
                cfg,
                str(cfg.get("base_currency") or defaults.get("base_currency") or base_currency).upper(),
            )
            if not manual:
                continue
            source_as_of = manual.get("as_of")
            if positions_as_of and str(source_as_of or "") < str(positions_as_of):
                continue
            owner_ratio = _owner_ratio(account_cfg, account_code)
            currency = str(manual.get("currency") or cfg.get("base_currency") or base_currency).upper()
            rate = _fx_rate(fx_rates, currency, base_currency)
            net_value_cny = _dec(manual.get("amount")) * owner_ratio * rate
        if balance and positions_as_of and str(source_as_of or "") < str(positions_as_of):
            continue
        position_value_cny = account_position_value_cny.get(account_code, Decimal("0"))
        cash_cny = net_value_cny - position_value_cny
        if abs(cash_cny) < Decimal("0.005"):
            continue
        cash_native = cash_cny / rate if rate else cash_cny
        display_name = cfg.get("display_name") or account_code
        out.append(
            {
                "account_code": account_code,
                "asset_id": f"{account_code}:CASH_{currency}",
                "symbol": f"CASH_{currency}",
                "asset_name": f"{display_name} \u73b0\u91d1/\u6d3b\u671f",
                "asset_type": "cash",
                "quantity": cash_native,
                "market_value": cash_native,
                "currency": currency,
                "market_value_cny": cash_cny,
                "cost_cny": cash_cny,
                "holding_pnl_cny": Decimal("0"),
                "as_of": source_as_of,
                "source_type": "synthetic_broker_cash",
            }
        )
    return out


def load_investment_transactions(cur, base_currency: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Load normalized investment transactions used by return and cost calculations."""
    if _table_exists(cur, "pams_investment_trade_fact"):
        fx_rates = _latest_fx_rates(cur, base_currency)
        cur.execute(
            """
            SELECT account_code,
                   trade_date,
                   symbol,
                   name,
                   COALESCE(side, trade_type) AS side,
                   quantity,
                   price,
                   gross_amount,
                   (COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0)) AS fees,
                   currency,
                   COALESCE(source_table, 'pams_investment_trade_fact') AS source,
                   COALESCE(source_key, trade_id::text) AS source_key,
                   trade_type,
                   net_cash,
                   net_cash_cny,
                   realized_pnl_cny
            FROM pams_investment_trade_fact
            WHERE COALESCE(review_status, '') <> 'deleted'
            ORDER BY trade_date DESC, trade_time DESC NULLS LAST, trade_id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            currency = str(item.get("currency") or base_currency).upper()
            item["gross_amount_cny"] = _dec(item.get("gross_amount")) * _fx_rate(fx_rates, currency, base_currency)
            rows.append(item)
        return rows

    if not _table_exists(cur, "pams_investment_transactions"):
        return []
    fx_rates = _latest_fx_rates(cur, base_currency)
    cur.execute(
        """
        SELECT account_code, trade_date, symbol, name, side, quantity, price,
               gross_amount, fees, currency, source, source_key
        FROM pams_investment_transactions
        ORDER BY trade_date DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = []
    for row in cur.fetchall():
        item = dict(row)
        currency = str(item.get("currency") or base_currency).upper()
        item["gross_amount_cny"] = _dec(item.get("gross_amount")) * _fx_rate(fx_rates, currency, base_currency)
        rows.append(item)
    return rows


def load_cash_transactions(cur, limit: int = 5000) -> list[dict[str, Any]]:
    """Load bank/payment statement rows as cash ledger inputs."""
    if not _table_exists(cur, "pams_bank_statement_rows"):
        return []
    cur.execute(
        """
        SELECT id, account_code, transaction_date, amount, currency, balance,
               category, subcategory, summary, description, counterparty, source_uid
        FROM pams_bank_statement_rows
        ORDER BY transaction_date DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def load_transfer_matches(cur, limit: int = 5000) -> list[dict[str, Any]]:
    """Load matched internal transfers across cash and investment accounts."""
    if not _table_exists(cur, "pams_bank_transfer_matches"):
        return []
    cur.execute(
        """
        SELECT id, match_key, match_type, relation_code, from_account_code,
               to_account_code, amount, currency, match_date, confidence, status
        FROM pams_bank_transfer_matches
        ORDER BY match_date DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def load_fx_rates(cur, base_currency: str) -> list[dict[str, Any]]:
    """Load the latest FX rates used to convert contract fields into base currency."""
    if not _table_exists(cur, "pams_fx_rates_daily"):
        return [{"base_currency": base_currency, "quote_currency": base_currency, "rate": Decimal("1"), "as_of": None}]
    cur.execute(
        """
        SELECT DISTINCT ON (base_currency, quote_currency)
               base_currency, quote_currency, rate, as_of, source
        FROM pams_fx_rates_daily
        WHERE base_currency = %s
        ORDER BY base_currency, quote_currency, as_of DESC
        """,
        (base_currency,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not any(str(r.get("quote_currency")).upper() == base_currency.upper() for r in rows):
        rows.append({"base_currency": base_currency, "quote_currency": base_currency, "rate": Decimal("1"), "as_of": None, "source": "identity"})
    return rows


def load_valuation_series(cur, limit: int = 370, base_currency: str = "CNY") -> list[dict[str, Any]]:
    """Load available valuation rows and project them to the valuation series contract."""
    if _table_exists(cur, "pams_portfolio_valuation_state"):
        cur.execute(
            """
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY valuation_date
                           ORDER BY
                               CASE WHEN status = 'published' THEN 0 ELSE 1 END,
                               published_at DESC NULLS LAST,
                               updated_at DESC NULLS LAST
                       ) AS rn
                FROM pams_portfolio_valuation_state
                WHERE base_currency = %s
            )
            SELECT valuation_date,
                   assets_cny,
                   liabilities_cny,
                   net_liquidation_value,
                   investment_value_cny,
                   total_pnl_cny
            FROM ranked
            WHERE rn = 1
            ORDER BY valuation_date DESC
            LIMIT %s
            """,
            (base_currency, limit),
        )
        raw_rows = list(reversed([dict(r) for r in cur.fetchall()]))
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            investment_value = raw.get("investment_value_cny")
            investment_pnl = raw.get("total_pnl_cny")
            investment_cost = None
            return_rate = None
            if investment_value is not None and investment_pnl is not None:
                investment_cost = _dec(investment_value) - _dec(investment_pnl)
                if abs(investment_cost) >= Decimal("0.01"):
                    return_rate = _dec(investment_pnl) / investment_cost
            rows.append(
                {
                    "date": raw.get("valuation_date"),
                    "assets_cny": raw.get("assets_cny"),
                    "liabilities_cny": raw.get("liabilities_cny"),
                    "net_worth_cny": raw.get("net_liquidation_value"),
                    "investment_value_cny": investment_value,
                    "investment_cost_cny": investment_cost,
                    "investment_pnl_cny": investment_pnl,
                    "investment_return_rate": return_rate,
                }
            )
        return rows

    if not _table_exists(cur, "pams_nav_daily"):
        return []
    cur.execute(
        """
        SELECT *
        FROM pams_nav_daily
        ORDER BY as_of DESC
        LIMIT %s
        """,
        (limit,),
    )
    raw_rows = list(reversed([dict(r) for r in cur.fetchall()]))
    rows = []
    for raw in raw_rows:
        net_worth = raw.get("net_worth_cny", raw.get("total_market_value"))
        rows.append(
            {
                "date": raw.get("date", raw.get("as_of")),
                "assets_cny": raw.get("assets_cny", net_worth),
                "liabilities_cny": raw.get("liabilities_cny", Decimal("0")),
                "net_worth_cny": net_worth,
                "investment_value_cny": raw.get("investment_value_cny"),
                "investment_cost_cny": raw.get("investment_cost_cny"),
                "investment_pnl_cny": raw.get("investment_pnl_cny"),
                "investment_return_rate": raw.get("investment_return_rate"),
            }
        )
    return rows


def build_risk_snapshot(
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a lightweight risk snapshot from normalized state fields."""
    total_nav = sum(
        (_dec(a.get("amount_cny")) for a in account_balances if a.get("include_in_nav")),
        Decimal("0"),
    )
    gross_exposure = sum(
        (abs(_dec(p.get("market_value_cny"))) for p in investment_positions if p.get("asset_type") != "liability"),
        Decimal("0"),
    )
    negative_cash = sum(
        (_dec(a.get("amount_cny")) for a in account_balances if _dec(a.get("amount_cny")) < 0),
        Decimal("0"),
    )
    denominator = total_nav if total_nav else Decimal("1")
    return {
        "total_nav": total_nav,
        "gross_exposure_ratio": gross_exposure / denominator,
        "negative_cash_ratio": abs(negative_cash) / denominator,
        "account_exposure": [],
        "position_exposure": [],
        "industry_exposure": [],
        "style_exposure": [],
        "factor_exposure": {},
        "stress_loss": {},
        "rebalance_suggestions": [],
    }


def _row_as_of(row: dict[str, Any]) -> Any:
    """Return the first date-like field that can describe row freshness."""
    for key in ("as_of", "date", "trade_date", "transaction_date", "match_date", "imported_at"):
        value = row.get(key)
        if value:
            return value
    return None


def _freshness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize date coverage for one PortfolioState component."""
    values = [str(_row_as_of(row))[:10] for row in rows if _row_as_of(row)]
    return {
        "latest_as_of": max(values) if values else None,
        "oldest_as_of": min(values) if values else None,
        "missing_as_of_count": max(len(rows) - len(values), 0),
    }


def _quality_check(
    check_id: str,
    *,
    status: str,
    severity: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "severity": severity,
        "message": message,
        "details": details or {},
    }


def _quality_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    error_count = sum(1 for check in checks if check.get("severity") == "error" and check.get("status") != "ok")
    warning_count = sum(1 for check in checks if check.get("severity") == "warning" and check.get("status") != "ok")
    if error_count:
        status = "error"
    elif warning_count:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "check_count": len(checks),
        "error_count": error_count,
        "warning_count": warning_count,
    }


def _quality_checks(
    *,
    coverage: dict[str, dict[str, Any]],
    freshness: dict[str, dict[str, Any]],
    missing_accounts: list[str],
    positions_missing_as_of: list[str],
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
    fx_rates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    missing_components = [name for name, item in coverage.items() if not item.get("has_data")]
    checks.append(
        _quality_check(
            "component_coverage",
            status="attention" if missing_components else "ok",
            severity="warning",
            message="PortfolioState components should have rows before pages rely on them.",
            details={"missing_components": missing_components},
        )
    )

    checks.append(
        _quality_check(
            "account_balance_coverage",
            status="attention" if missing_accounts else "ok",
            severity="warning",
            message="Accounts marked missing_data need statement or manual balance confirmation.",
            details={"missing_accounts": missing_accounts},
        )
    )

    checks.append(
        _quality_check(
            "position_freshness",
            status="attention" if positions_missing_as_of else "ok",
            severity="warning",
            message="Investment positions should carry an as_of date.",
            details={"positions_missing_as_of": positions_missing_as_of[:20]},
        )
    )

    position_latest = (freshness.get("investment_positions") or {}).get("latest_as_of")
    valuation_latest = (freshness.get("valuation_series") or {}).get("latest_as_of")
    valuation_stale = bool(position_latest and valuation_latest and valuation_latest < position_latest)
    checks.append(
        _quality_check(
            "valuation_series_freshness",
            status="attention" if valuation_stale else "ok",
            severity="warning",
            message="Valuation series should not lag current investment positions.",
            details={
                "investment_positions_latest_as_of": position_latest,
                "valuation_series_latest_as_of": valuation_latest,
            },
        )
    )

    base_currency = "CNY"
    required_currencies = {
        str(row.get("currency") or row.get("base_currency") or base_currency).upper()
        for row in [*account_balances, *investment_positions]
        if row.get("currency") or row.get("base_currency")
    }
    quoted_currencies = {str(row.get("quote_currency") or "").upper() for row in fx_rates}
    missing_fx = sorted(c for c in required_currencies if c and c != base_currency and c not in quoted_currencies)
    checks.append(
        _quality_check(
            "fx_rate_coverage",
            status="attention" if missing_fx else "ok",
            severity="error",
            message="All non-CNY balances and positions need a current FX rate.",
            details={"missing_currencies": missing_fx},
        )
    )

    return checks


def build_data_quality(
    *,
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
    investment_transactions: list[dict[str, Any]],
    cash_transactions: list[dict[str, Any]],
    transfer_matches: list[dict[str, Any]],
    fx_rates: list[dict[str, Any]],
    valuation_series: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build read-only coverage and freshness metadata for PortfolioState consumers."""
    components = {
        "account_balances": account_balances,
        "investment_positions": investment_positions,
        "investment_transactions": investment_transactions,
        "cash_transactions": cash_transactions,
        "transfer_matches": transfer_matches,
        "fx_rates": fx_rates,
        "valuation_series": valuation_series,
    }
    missing_accounts = [
        str(row.get("account_code"))
        for row in account_balances
        if str(row.get("review_status") or "") == "missing_data"
    ]
    positions_missing_as_of = [
        str(row.get("asset_id") or row.get("symbol") or row.get("asset_name") or "")
        for row in investment_positions
        if not row.get("as_of")
    ]
    coverage = {
        name: {
            "rows": len(rows),
            "has_data": bool(rows),
        }
        for name, rows in components.items()
    }
    freshness = {name: _freshness(rows) for name, rows in components.items()}
    checks = _quality_checks(
        coverage=coverage,
        freshness=freshness,
        missing_accounts=missing_accounts,
        positions_missing_as_of=positions_missing_as_of,
        account_balances=account_balances,
        investment_positions=investment_positions,
        fx_rates=fx_rates,
    )
    return {
        "coverage": coverage,
        "freshness": freshness,
        "missing": {
            "account_balances_missing_data": missing_accounts,
            "investment_positions_missing_as_of": positions_missing_as_of,
            "components_without_rows": [name for name, rows in components.items() if not rows],
        },
        "checks": checks,
        "summary": _quality_summary(checks),
    }


def build_portfolio_state_from_parts(
    *,
    as_of: Date | str | None,
    base_currency: str,
    account_balances: list[dict[str, Any]] | None = None,
    investment_positions: list[dict[str, Any]] | None = None,
    investment_transactions: list[dict[str, Any]] | None = None,
    cash_transactions: list[dict[str, Any]] | None = None,
    transfer_matches: list[dict[str, Any]] | None = None,
    fx_rates: list[dict[str, Any]] | None = None,
    valuation_series: list[dict[str, Any]] | None = None,
    risk_snapshot: dict[str, Any] | None = None,
    data_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a JSON-serializable PortfolioState from normalized components."""
    balances = account_balances or []
    positions = investment_positions or []
    state = {
        "as_of": as_of or _view_as_of(),
        "base_currency": str(base_currency or "CNY").upper(),
        "account_balances": balances,
        "investment_positions": positions,
        "investment_transactions": investment_transactions or [],
        "cash_transactions": cash_transactions or [],
        "transfer_matches": transfer_matches or [],
        "fx_rates": fx_rates or [],
        "valuation_series": valuation_series or [],
        "risk_snapshot": risk_snapshot or build_risk_snapshot(balances, positions),
        "data_quality": data_quality or build_data_quality(
            account_balances=balances,
            investment_positions=positions,
            investment_transactions=investment_transactions or [],
            cash_transactions=cash_transactions or [],
            transfer_matches=transfer_matches or [],
            fx_rates=fx_rates or [],
            valuation_series=valuation_series or [],
        ),
    }
    return _norm(state)


def build_data_quality_payload_from_parts(
    *,
    as_of: Date | str | None,
    base_currency: str,
    account_balances: list[dict[str, Any]] | None = None,
    investment_positions: list[dict[str, Any]] | None = None,
    investment_transactions: list[dict[str, Any]] | None = None,
    cash_transactions: list[dict[str, Any]] | None = None,
    transfer_matches: list[dict[str, Any]] | None = None,
    fx_rates: list[dict[str, Any]] | None = None,
    valuation_series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the lightweight read-only data-quality payload."""
    return _norm(
        {
            "as_of": as_of or _view_as_of(),
            "base_currency": str(base_currency or "CNY").upper(),
            "data_quality": build_data_quality(
                account_balances=account_balances or [],
                investment_positions=investment_positions or [],
                investment_transactions=investment_transactions or [],
                cash_transactions=cash_transactions or [],
                transfer_matches=transfer_matches or [],
                fx_rates=fx_rates or [],
                valuation_series=valuation_series or [],
            ),
        }
    )


def load_portfolio_state_parts(
    *,
    base_currency: str = "CNY",
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Read source tables once and return normalized PortfolioState component parts."""
    base = str(base_currency or "CNY").upper()
    account_cfg = _load_accounts_config()
    with conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            account_balances = load_account_balances(cur, base, account_cfg)
            investment_positions = load_investment_positions(
                cur,
                base,
                account_cfg,
                account_balances=account_balances,
            )
            investment_transactions = load_investment_transactions(cur, base)
            cash_transactions = load_cash_transactions(cur)
            transfer_matches = load_transfer_matches(cur)
            fx_rates = load_fx_rates(cur, base)
            valuation_series = load_valuation_series(cur, base_currency=base)

    as_of_values = []
    for row in account_balances:
        if row.get("as_of"):
            as_of_values.append(str(row["as_of"])[:10])
    for row in investment_positions:
        if row.get("as_of"):
            as_of_values.append(str(row["as_of"])[:10])
    return {
        "as_of": max(as_of_values) if as_of_values else _view_as_of(),
        "base_currency": base,
        "account_balances": account_balances,
        "investment_positions": investment_positions,
        "investment_transactions": investment_transactions,
        "cash_transactions": cash_transactions,
        "transfer_matches": transfer_matches,
        "fx_rates": fx_rates,
        "valuation_series": valuation_series,
    }


def build_portfolio_state(
    *,
    base_currency: str = "CNY",
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Read existing PAMS sources and return the unified PortfolioState payload."""
    parts = load_portfolio_state_parts(base_currency=base_currency, conn_factory=conn_factory)
    return build_portfolio_state_from_parts(
        **parts,
    )


def build_portfolio_data_quality(
    *,
    base_currency: str = "CNY",
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Read existing PAMS sources and return only PortfolioState data-quality metadata."""
    parts = load_portfolio_state_parts(base_currency=base_currency, conn_factory=conn_factory)
    return build_data_quality_payload_from_parts(**parts)
