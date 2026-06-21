"""Portfolio NAV bridge and accounting diagnostics.

This module intentionally keeps the reconciliation math outside of frontend
components.  It compares the NAV curve with cashflow facts and the canonical
investment return attribution, then leaves any residual visible.
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Any, Callable

import psycopg2.extras

from services.portfolio_state import _load_accounts_config, _owner_ratio, get_conn
from services.portfolio_valuation_state import _build_current_state
from services.return_attribution import build_return_attribution_payload
from services.valuation_nav import build_nav_weekly_payload


MONEY_QUANT = Decimal("0.01")

MAIN_INCOME_SUBCATEGORIES = {
    "工资",
    "奖金",
    "年终奖",
    "小额收入",
    "小额零散收入",
}


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _date(value: Any) -> Date | None:
    if isinstance(value, Date):
        return value
    if value in (None, ""):
        return None
    try:
        return Date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.quantize(MONEY_QUANT))
    if isinstance(value, Date):
        return value.isoformat()
    if isinstance(value, list):
        return [_norm(item) for item in value]
    if isinstance(value, dict):
        return {key: _norm(item) for key, item in value.items()}
    return value


def _owner_ratio_case_sql(account_cfg: dict[str, Any]) -> Callable[[str], tuple[str, list[Any]]]:
    accounts = account_cfg.get("accounts") or {}
    defaults = account_cfg.get("defaults") or {}
    default_owner_ratio = Decimal(str(defaults.get("owner_ratio", 1)))

    def build(alias: str = "h") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for account_code, cfg in accounts.items():
            ratio = Decimal(str((cfg or {}).get("owner_ratio", default_owner_ratio)))
            clauses.append(f"WHEN {alias}.account_code = %s THEN %s")
            params.extend([str(account_code), float(ratio)])
        if not clauses:
            return "%s", [float(default_owner_ratio)]
        return f"(CASE {' '.join(clauses)} ELSE %s END)", [*params, float(default_owner_ratio)]

    return build


def _account_owner_ratio_lookup(account_cfg: dict[str, Any]) -> Callable[[str], Decimal]:
    def lookup(account_code: str) -> Decimal:
        return _owner_ratio(account_cfg, str(account_code or ""))

    return lookup


def build_bridge_reconciliation_from_inputs(
    *,
    beginning_nav_cny: Decimal,
    ending_nav_cny: Decimal,
    external_cashflow_cny: Decimal,
    investment_pnl_cny: Decimal,
    non_investment_adjustments_cny: Decimal = Decimal("0"),
) -> dict[str, Any]:
    """Pure NAV bridge math used by the live service and regression tests."""
    nav_delta = ending_nav_cny - beginning_nav_cny
    nav_residual_before_investment = nav_delta - external_cashflow_cny
    unexplained_residual = (
        nav_residual_before_investment
        - investment_pnl_cny
        - non_investment_adjustments_cny
    )
    return {
        "nav_delta_cny": nav_delta,
        "external_cashflow_cny": external_cashflow_cny,
        "nav_residual_before_investment_cny": nav_residual_before_investment,
        "investment_pnl_cny": investment_pnl_cny,
        "non_investment_adjustments_cny": non_investment_adjustments_cny,
        "unexplained_residual_cny": unexplained_residual,
        "is_reconciled": abs(unexplained_residual) < Decimal("100"),
    }


def _cashflow_breakdown(cur, *, start_date: Date, end_date: Date) -> dict[str, Any]:
    cur.execute(
        """
        SELECT cashflow_type,
               COALESCE(category, '') AS category,
               COALESCE(subcategory, '') AS subcategory,
               COUNT(*) AS count,
               COALESCE(SUM(amount_cny), 0) AS amount_cny
        FROM pams_cashflow_fact
        WHERE event_date >= %s
          AND event_date <= %s
          AND affects_nav IS TRUE
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY cashflow_type, COALESCE(category, ''), COALESCE(subcategory, '')
        ORDER BY cashflow_type, category, subcategory
        """,
        (start_date, end_date),
    )
    buckets = {
        "main_income": {"label": "工资/奖金/小额收入", "amount_cny": Decimal("0"), "count": 0},
        "other_income": {"label": "其他收入", "amount_cny": Decimal("0"), "count": 0},
        "expense": {"label": "消费支出", "amount_cny": Decimal("0"), "count": 0},
        "fees_tax_cash_income": {"label": "费用/税费/现金收益", "amount_cny": Decimal("0"), "count": 0},
        "other_affects_nav": {"label": "其他影响NAV现金流", "amount_cny": Decimal("0"), "count": 0},
    }
    details: list[dict[str, Any]] = []
    for row in cur.fetchall():
        amount = _dec(row.get("amount_cny"))
        count = int(row.get("count") or 0)
        cashflow_type = str(row.get("cashflow_type") or "")
        subcategory = str(row.get("subcategory") or "")
        if cashflow_type == "operating_income" and subcategory in MAIN_INCOME_SUBCATEGORIES:
            key = "main_income"
        elif cashflow_type == "operating_income":
            key = "other_income"
        elif cashflow_type == "operating_expense":
            key = "expense"
        elif cashflow_type in {"fee", "tax", "investment_return_cash"}:
            key = "fees_tax_cash_income"
        else:
            key = "other_affects_nav"
        buckets[key]["amount_cny"] += amount
        buckets[key]["count"] += count
        details.append(
            {
                "bucket": key,
                "cashflow_type": cashflow_type,
                "category": row.get("category"),
                "subcategory": subcategory,
                "count": count,
                "amount_cny": amount,
            }
        )

    external_net = sum((_dec(item["amount_cny"]) for item in buckets.values()), Decimal("0"))
    return {
        "external_net_cny": external_net,
        "buckets": buckets,
        "details": details,
    }


def _trade_cash_amount_sql() -> str:
    return """
        ABS(COALESCE(
            NULLIF(net_cash_cny, 0),
            NULLIF(net_cash * fx_rate_to_cny, 0),
            NULLIF(gross_amount * fx_rate_to_cny, 0),
            gross_amount * fx_rate_to_cny,
            0
        ))
    """


def _funding_closure_checks(cur, *, start_date: Date, end_date: Date, account_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    cur.execute(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(ABS(amount_cny)), 0) AS amount_cny,
               MIN(event_date) AS first_date,
               MAX(event_date) AS last_date
        FROM pams_cashflow_fact
        WHERE event_date >= %s
          AND event_date <= %s
          AND counter_account_code = 'ibkr'
          AND amount_cny < 0
          AND cashflow_type IN ('investment_funding', 'internal_transfer')
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        """,
        (start_date, end_date),
    )
    ibkr_cashflow = cur.fetchone() or {}
    cur.execute(
        f"""
        SELECT COUNT(*) AS count,
               COALESCE(SUM({_trade_cash_amount_sql()}), 0) AS amount_cny,
               MIN(trade_date) AS first_date,
               MAX(trade_date) AS last_date
        FROM pams_investment_trade_fact
        WHERE trade_date >= %s
          AND trade_date <= %s
          AND account_code = 'ibkr'
          AND trade_type = 'deposit'
          AND COALESCE(LOWER(review_status), 'auto') NOT IN ('ignored', 'cancelled', 'canceled', 'void')
        """,
        (start_date, end_date),
    )
    ibkr_trade = cur.fetchone() or {}
    ibkr_gap = _dec(ibkr_cashflow.get("amount_cny")) - _dec(ibkr_trade.get("amount_cny"))
    issues.append(
        {
            "code": "ibkr_funding_closure",
            "label": "IBKR入金双边闭环",
            "status": "ok" if abs(ibkr_gap) < Decimal("100") else "mismatch",
            "cashflow_side_cny": _dec(ibkr_cashflow.get("amount_cny")),
            "investment_trade_side_cny": _dec(ibkr_trade.get("amount_cny")),
            "gap_cny": ibkr_gap,
            "cashflow_count": int(ibkr_cashflow.get("count") or 0),
            "trade_count": int(ibkr_trade.get("count") or 0),
            "period": {
                "cashflow_first": ibkr_cashflow.get("first_date"),
                "cashflow_last": ibkr_cashflow.get("last_date"),
                "trade_first": ibkr_trade.get("first_date"),
                "trade_last": ibkr_trade.get("last_date"),
            },
            "note": "银行侧转券商与IBKR deposit fact必须同时存在，否则只能作为在途或数据缺口。",
        }
    )

    cur.execute(
        f"""
        SELECT COUNT(*) AS count,
               COALESCE(SUM(
                   CASE
                       WHEN trade_type = 'deposit' THEN {_trade_cash_amount_sql()}
                       WHEN trade_type = 'withdraw' THEN -{_trade_cash_amount_sql()}
                       ELSE 0
                   END
               ), 0) AS amount_cny
        FROM pams_investment_trade_fact
        WHERE trade_date <= %s
          AND account_code = 'tonghuashun'
          AND trade_type IN ('deposit', 'withdraw')
          AND COALESCE(LOWER(review_status), 'auto') NOT IN ('ignored', 'cancelled', 'canceled', 'void')
        """,
        (end_date,),
    )
    ths_trade = cur.fetchone() or {}
    cur.execute(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(ABS(amount_cny)), 0) AS amount_cny,
               MIN(event_date) AS first_date,
               MAX(event_date) AS last_date
        FROM pams_cashflow_fact
        WHERE event_date <= %s
          AND account_code = 'ccb_7332'
          AND counter_account_code = 'tonghuashun'
          AND amount_cny < 0
          AND cashflow_type IN ('investment_funding', 'internal_transfer')
          AND (
              counterparty ILIKE '%%沈钰棋%%'
              OR summary ILIKE '%%沈钰棋%%'
              OR description ILIKE '%%沈钰棋%%'
          )
          AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        """,
        (end_date,),
    )
    ths_cashflow = cur.fetchone() or {}
    ths_cfg = (account_cfg.get("accounts") or {}).get("tonghuashun") or {}
    expected_personal = _dec(ths_cfg.get("personal_capital"))
    owner_ratio = _owner_ratio(account_cfg, "tonghuashun")
    actual_deposit_gross = _dec(ths_trade.get("amount_cny"))
    actual_deposit_personal = actual_deposit_gross * owner_ratio
    source_personal = _dec(ths_cashflow.get("amount_cny"))
    ths_gap = actual_deposit_personal - expected_personal if expected_personal else Decimal("0")
    ths_source_gap = source_personal - expected_personal if expected_personal else Decimal("0")
    ths_status = (
        "ok"
        if (
            not expected_personal
            or (
                abs(ths_gap) < Decimal("100")
                and abs(ths_source_gap) < Decimal("100")
            )
        )
        else "mismatch"
    )
    issues.append(
        {
            "code": "tonghuashun_personal_capital",
            "label": "同花顺个人本金口径",
            "status": ths_status,
            "investment_trade_deposit_gross_cny": actual_deposit_gross,
            "investment_trade_deposit_personal_cny": actual_deposit_personal,
            "cashflow_source_personal_cny": source_personal,
            "expected_personal_capital_cny": expected_personal,
            "gap_cny": ths_gap,
            "source_gap_cny": ths_source_gap,
            "trade_count": int(ths_trade.get("count") or 0),
            "source_cashflow_count": int(ths_cashflow.get("count") or 0),
            "source_cashflow_period": {
                "first": ths_cashflow.get("first_date"),
                "last": ths_cashflow.get("last_date"),
            },
            "owner_ratio": str(owner_ratio),
            "note": "账户总资金与个人权益比例必须分开；收益口径应使用个人权益侧。",
        }
    )
    return issues


def _terminal_nav_payload(state: dict[str, Any]) -> tuple[Date, dict[str, Decimal], Decimal]:
    terminal_date = _date(state.get("valuation_date") or state.get("as_of")) or Date.today()
    assets = _dec(state.get("assets_cny"))
    liabilities = _dec(state.get("liabilities_cny"))
    net_worth = _dec(state.get("net_liquidation_value") or state.get("net_worth") or state.get("market_value"))
    if not net_worth and (assets or liabilities):
        net_worth = assets - liabilities
    return terminal_date, {"assets": assets, "liabilities": liabilities, "net_worth": net_worth}, net_worth


def build_equity_bridge_payload(
    *,
    start_date: Date,
    base_currency: str = "CNY",
    overview_payload: dict[str, Any],
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Return a transparent NAV bridge instead of hiding residuals in PnL."""
    base = str(base_currency or "CNY").upper()
    account_cfg = _load_accounts_config()
    with conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            valuation_state = _build_current_state(
                cur,
                base_currency=base,
                cycle="equity_bridge",
                status="draft",
            )
            terminal_date, terminal_nav_bucket, ending_nav = _terminal_nav_payload(valuation_state)
            if start_date > terminal_date:
                start_date = terminal_date
            nav_payload = build_nav_weekly_payload(
                cur,
                start_date=start_date,
                base_currency=base,
                overview_payload=overview_payload,
                owner_ratio_case_sql=_owner_ratio_case_sql(account_cfg),
                account_owner_ratio=_account_owner_ratio_lookup(account_cfg),
                terminal_date=terminal_date,
                terminal_nav_bucket=terminal_nav_bucket,
                interval="daily",
            )
            rows = nav_payload.get("rows") or []
            beginning_nav = _dec(rows[0].get("netWorth")) if rows else Decimal("0")
            ending_nav = _dec(rows[-1].get("netWorth")) if rows else ending_nav
            cashflows = _cashflow_breakdown(cur, start_date=start_date, end_date=terminal_date)
            attribution = build_return_attribution_payload(cur, base_currency=base)
            investment_pnl = _dec(
                attribution.get("canonical_total_pnl_cny")
                or attribution.get("total_pnl_cny")
            )
            bridge = build_bridge_reconciliation_from_inputs(
                beginning_nav_cny=beginning_nav,
                ending_nav_cny=ending_nav,
                external_cashflow_cny=_dec(cashflows["external_net_cny"]),
                investment_pnl_cny=investment_pnl,
            )
            checks = _funding_closure_checks(
                cur,
                start_date=start_date,
                end_date=terminal_date,
                account_cfg=account_cfg,
            )

    payload = {
        "start": start_date,
        "end": terminal_date,
        "base_currency": base,
        "formula": (
            "ending_nav = beginning_nav + external_cashflow "
            "+ canonical_investment_pnl + non_investment_adjustments + unexplained_residual"
        ),
        "beginning_nav_cny": beginning_nav,
        "ending_nav_cny": ending_nav,
        "valuation_state_source": "computed_current",
        "nav_series_method": nav_payload.get("method"),
        "reconciliation": bridge,
        "cashflows": cashflows,
        "investment": {
            "canonical_total_pnl_cny": investment_pnl,
            "raw_total_pnl_cny": _dec(attribution.get("total_pnl_cny")),
            "portfolio_nav_cny": _dec(attribution.get("portfolio_nav_cny")),
            "as_of": attribution.get("as_of"),
            "source": "analysis/return-attribution.canonical_total_pnl_cny",
        },
        "data_checks": checks,
        "quality": {
            "status": "ok" if bridge["is_reconciled"] else "unreconciled",
            "unexplained_residual_cny": bridge["unexplained_residual_cny"],
            "blocking_checks": [item for item in checks if item.get("status") != "ok"],
        },
    }
    return _norm(payload)
