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

from services.portfolio_state import _excluded_accounts, _load_accounts_config, _owner_ratio, get_conn
from services.portfolio_valuation_state import _build_current_state
from services.return_attribution import build_return_attribution_payload
from services.valuation_nav import build_nav_weekly_payload, current_nav_account_values_from_parts


MONEY_QUANT = Decimal("0.01")

# ``excluded_accounts`` in accounts.yaml is used by holdings/NAV snapshots and
# includes parent ledgers such as ``wechat``.  Cashflow facts can still use
# those parent ledgers for real period movements, so the bridge only filters
# confirmed legacy aliases that would otherwise double count old statement
# imports.
BRIDGE_CASHFLOW_LEGACY_EXCLUDED_ACCOUNTS = {
    "binance",
    "ccb_cny",
    "cgb_cny",
    "guangfa",
    "jianhang",
    "jiekuan",
    "loan",
}

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


def _bridge_cashflow_excluded_accounts(account_cfg: dict[str, Any]) -> set[str]:
    configured = _excluded_accounts(account_cfg)
    return configured.intersection(BRIDGE_CASHFLOW_LEGACY_EXCLUDED_ACCOUNTS)


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


def period_investment_effect_from_inputs(
    *,
    beginning_nav_cny: Decimal,
    ending_nav_cny: Decimal,
    external_cashflow_cny: Decimal,
) -> Decimal:
    """Return the period valuation effect required by the NAV bridge.

    Current holding PnL is a cumulative cost-basis metric.  A NAV bridge needs
    period contribution since the selected start date, so this value is kept
    separate and compared with return attribution instead of mixing period and
    cumulative metrics in one residual.
    """
    return ending_nav_cny - beginning_nav_cny - external_cashflow_cny


def _amount_tone(amount: Decimal, *, residual: bool = False) -> str:
    if abs(amount) < Decimal("0.01"):
        return "neutral"
    if residual:
        return "warning" if abs(amount) >= Decimal("100") else "ok"
    return "positive" if amount > 0 else "negative"


def _share_of_delta(amount: Decimal, nav_delta: Decimal) -> Decimal:
    if abs(nav_delta) < Decimal("0.01"):
        return Decimal("0")
    return amount / nav_delta


def build_nav_bridge_steps_from_inputs(
    *,
    beginning_nav_cny: Decimal,
    external_cashflow_cny: Decimal,
    period_investment_effect_cny: Decimal,
    unexplained_residual_cny: Decimal,
    ending_nav_cny: Decimal,
) -> list[dict[str, Any]]:
    """Build additive NAV bridge steps that can be checked without UI math."""
    running = beginning_nav_cny
    steps = [
        {
            "key": "beginning_nav",
            "label": "Beginning NAV",
            "amount_cny": beginning_nav_cny,
            "running_nav_cny": running,
            "formula": "period opening net asset value",
        },
    ]
    for key, label, amount, formula in [
        (
            "external_cashflow",
            "External cashflow",
            external_cashflow_cny,
            "income - expenses; internal transfers are excluded",
        ),
        (
            "period_investment_effect",
            "Investment and valuation effect",
            period_investment_effect_cny,
            "ending NAV - beginning NAV - external cashflow",
        ),
        (
            "unexplained_residual",
            "Residual",
            unexplained_residual_cny,
            "remaining account or valuation calibration gap",
        ),
    ]:
        running += amount
        steps.append(
            {
                "key": key,
                "label": label,
                "amount_cny": amount,
                "running_nav_cny": running,
                "formula": formula,
            }
        )
    steps.append(
        {
            "key": "ending_nav",
            "label": "Ending NAV",
            "amount_cny": ending_nav_cny,
            "running_nav_cny": running,
            "formula": "beginning NAV + external cashflow + investment effect + residual",
            "is_balanced": abs(running - ending_nav_cny) < Decimal("0.01"),
        }
    )
    return steps


def _terminal_account_items(terminal_account_values: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "account_code": str(account_code),
            "amount_cny": _dec(amount),
        }
        for account_code, amount in (terminal_account_values or {}).items()
    ]
    rows.sort(key=lambda item: abs(item["amount_cny"]), reverse=True)
    return rows


def _bridge_quality(
    *,
    bridge: dict[str, Any],
    investment_attribution_gap_cny: Decimal,
    checks: list[dict[str, Any]],
    tolerance_cny: Decimal = Decimal("100"),
) -> dict[str, Any]:
    residual = _dec(bridge.get("unexplained_residual_cny"))
    blocking_checks = [item for item in checks if item.get("status") != "ok"]
    investment_gap_issue = abs(investment_attribution_gap_cny) >= tolerance_cny
    if investment_gap_issue:
        blocking_checks.append(
            {
                "code": "investment_attribution_gap",
                "label": "Investment attribution gap",
                "status": "mismatch",
                "gap_cny": investment_attribution_gap_cny,
                "note": "NAV period investment effect is not fully explained by return attribution.",
            }
        )
    residual_issue = abs(residual) >= tolerance_cny
    status = "ok" if not residual_issue and not blocking_checks else "unreconciled"
    return {
        "status": status,
        "unexplained_residual_cny": residual,
        "investment_attribution_gap_cny": investment_attribution_gap_cny,
        "investment_attribution_explained": not investment_gap_issue,
        "blocking_checks": blocking_checks,
    }


def build_nav_growth_explanation_from_inputs(
    *,
    beginning_nav_cny: Decimal,
    ending_nav_cny: Decimal,
    cashflows: dict[str, Any],
    period_investment_effect_cny: Decimal,
    attribution_pnl_cny: Decimal,
    bridge: dict[str, Any],
    attribution_current_float_pnl_cny: Decimal | None = None,
    attribution_closed_realized_pnl_cny: Decimal | None = None,
    attribution_reconciliation_pnl_cny: Decimal | None = None,
) -> dict[str, Any]:
    """Build UI-ready NAV growth explanation without changing bridge math."""
    nav_delta = ending_nav_cny - beginning_nav_cny
    external_cashflow = _dec(cashflows.get("external_net_cny"))
    residual = _dec(bridge.get("unexplained_residual_cny"))
    attribution_gap = period_investment_effect_cny - attribution_pnl_cny
    current_float_pnl = (
        _dec(attribution_current_float_pnl_cny)
        if attribution_current_float_pnl_cny is not None
        else attribution_pnl_cny
    )
    closed_realized_pnl = (
        _dec(attribution_closed_realized_pnl_cny)
        if attribution_closed_realized_pnl_cny is not None
        else attribution_pnl_cny - current_float_pnl
    )
    account_reconciliation_pnl = _dec(attribution_reconciliation_pnl_cny)
    actual_closed_realized_pnl = closed_realized_pnl - account_reconciliation_pnl
    current_float_with_reconciliation = current_float_pnl + account_reconciliation_pnl
    cumulative_period_gap = attribution_pnl_cny - period_investment_effect_cny
    float_period_gap = current_float_with_reconciliation - period_investment_effect_cny

    drivers = [
        {
            "key": "external_cashflow",
            "label": "外部现金流",
            "amount_cny": external_cashflow,
            "share_of_delta": _share_of_delta(external_cashflow, nav_delta),
            "tone": _amount_tone(external_cashflow),
            "formula": "工资/奖金/小额收入 - 消费支出 - 费用税费 + 其他现金流",
        },
        {
            "key": "period_investment_effect",
            "label": "投资/估值影响",
            "amount_cny": period_investment_effect_cny,
            "share_of_delta": _share_of_delta(period_investment_effect_cny, nav_delta),
            "tone": _amount_tone(period_investment_effect_cny),
            "formula": "期末NAV - 期初NAV - 外部现金流",
        },
        {
            "key": "unexplained_residual",
            "label": "总账残差",
            "amount_cny": residual,
            "share_of_delta": _share_of_delta(residual, nav_delta),
            "tone": _amount_tone(residual, residual=True),
            "formula": "NAV增值 - 外部现金流 - 投资/估值影响",
        },
    ]

    investment_breakdown = [
        {
            "key": "attributed_investment_pnl",
            "label": "已归因标的盈亏",
            "amount_cny": attribution_pnl_cny,
            "tone": _amount_tone(attribution_pnl_cny),
            "formula": "投资页收益归因的当前统一口径",
        },
        {
            "key": "investment_attribution_gap",
            "label": "待解释估值/账户差额",
            "amount_cny": attribution_gap,
            "tone": "warning" if abs(attribution_gap) >= Decimal("100") else "neutral",
            "formula": "投资/估值影响 - 已归因标的盈亏",
        },
    ]

    investment_breakdown = [
        {
            "key": "period_investment_effect",
            "label": "NAV期间投资增值",
            "amount_cny": period_investment_effect_cny,
            "tone": _amount_tone(period_investment_effect_cny),
            "formula": "期末NAV - 期初NAV - 外部现金流；这是解释NAV增值的主口径",
        },
        {
            "key": "attributed_investment_pnl",
            "label": "持仓累计总盈亏",
            "amount_cny": attribution_pnl_cny,
            "tone": _amount_tone(attribution_pnl_cny),
            "formula": "持仓页口径：当前浮盈 + 已清仓/已实现盈亏；用于对照，不直接作为NAV期间增值",
        },
        {
            "key": "closed_realized_pnl",
            "label": "其中：已实现/清仓盈亏",
            "amount_cny": actual_closed_realized_pnl,
            "tone": _amount_tone(actual_closed_realized_pnl),
            "formula": "持仓累计总盈亏中已实现和清仓部分；已剔除账户口径校准，不等同于期末浮盈",
        },
    ]
    if abs(account_reconciliation_pnl) >= Decimal("0.01"):
        investment_breakdown.append(
            {
                "key": "account_reconciliation_pnl",
                "label": "其中：账户口径校准",
                "amount_cny": account_reconciliation_pnl,
                "tone": _amount_tone(account_reconciliation_pnl),
                "formula": "账户级目标口径与持仓明细合计的校准差；用于对齐账户净值，不属于清仓盈亏",
            }
        )
    investment_breakdown.append(
        {
            "key": "float_period_gap",
            "label": "其中：期末浮盈与期间口径差",
            "amount_cny": float_period_gap,
            "tone": "warning" if abs(float_period_gap) >= Decimal("100") else "neutral",
            "formula": "当前浮盈 + 账户口径校准 - NAV期间投资增值；剩余主要来自期初已持仓浮盈/浮亏、舍入和估值时点",
        }
    )

    bucket_labels = {
        "main_income": "工资/奖金/小额收入",
        "other_income": "其他收入",
        "expense": "消费支出",
        "fees_tax_cash_income": "费用税费/现金收益",
        "other_affects_nav": "其他NAV现金流",
        "investment_realized_cash": "投资现金实现",
    }
    cashflow_items = []
    for key, item in (cashflows.get("buckets") or {}).items():
        amount = _dec(item.get("amount_cny"))
        if abs(amount) < Decimal("0.01") and int(item.get("count") or 0) == 0:
            continue
        cashflow_items.append(
            {
                "key": key,
                "label": bucket_labels.get(key, str(item.get("label") or key)),
                "amount_cny": amount,
                "count": int(item.get("count") or 0),
                "excluded_from_external_cashflow": bool(item.get("excluded_from_external_cashflow")),
                "tone": _amount_tone(amount),
            }
        )

    return {
        "title": "年初至今NAV增值解释",
        "summary": {
            "beginning_nav_cny": beginning_nav_cny,
            "ending_nav_cny": ending_nav_cny,
            "nav_delta_cny": nav_delta,
            "nav_delta_rate": nav_delta / beginning_nav_cny if abs(beginning_nav_cny) >= Decimal("0.01") else Decimal("0"),
            "external_cashflow_cny": external_cashflow,
            "period_investment_effect_cny": period_investment_effect_cny,
            "attributed_investment_pnl_cny": attribution_pnl_cny,
            "investment_attribution_gap_cny": attribution_gap,
            "cumulative_period_gap_cny": cumulative_period_gap,
            "current_float_pnl_cny": current_float_pnl,
            "closed_realized_pnl_cny": actual_closed_realized_pnl,
            "account_reconciliation_pnl_cny": account_reconciliation_pnl,
            "current_float_with_reconciliation_cny": current_float_with_reconciliation,
            "float_period_gap_cny": float_period_gap,
            "unexplained_residual_cny": residual,
        },
        "drivers": drivers,
        "investment_breakdown": investment_breakdown,
        "cashflow_items": cashflow_items,
        "is_reconciled": bool(bridge.get("is_reconciled")),
    }


def _cashflow_breakdown_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = {
        "main_income": {"label": "工资/奖金/小额收入", "amount_cny": Decimal("0"), "count": 0},
        "other_income": {"label": "其他收入", "amount_cny": Decimal("0"), "count": 0},
        "expense": {"label": "消费支出", "amount_cny": Decimal("0"), "count": 0},
        "fees_tax_cash_income": {"label": "费用/税费/现金收益", "amount_cny": Decimal("0"), "count": 0},
        "other_affects_nav": {"label": "其他影响NAV现金流", "amount_cny": Decimal("0"), "count": 0},
        "investment_realized_cash": {
            "label": "已由投资收益解释的现金实现",
            "amount_cny": Decimal("0"),
            "count": 0,
            "excluded_from_external_cashflow": True,
        },
    }
    details: list[dict[str, Any]] = []
    for row in rows:
        amount = _dec(row.get("amount_cny"))
        count = int(row.get("count") or 0)
        cashflow_type = str(row.get("cashflow_type") or "")
        subcategory = str(row.get("subcategory") or "")
        is_linked_investment_return = bool(row.get("is_linked_investment_return"))
        if is_linked_investment_return:
            key = "investment_realized_cash"
        elif cashflow_type == "operating_income" and subcategory in MAIN_INCOME_SUBCATEGORIES:
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
                "excluded_from_external_cashflow": is_linked_investment_return,
            }
        )

    external_net = sum(
        (
            _dec(item["amount_cny"])
            for item in buckets.values()
            if not item.get("excluded_from_external_cashflow")
        ),
        Decimal("0"),
    )
    return {
        "external_net_cny": external_net,
        "buckets": buckets,
        "details": details,
    }


def _attribution_pnl_basis(attribution: dict[str, Any], total_pnl: Decimal) -> tuple[Decimal, Decimal]:
    """Split cumulative attribution PnL into open floating and realized/closed parts."""
    holding_rows = ((attribution or {}).get("dimensions") or {}).get("holding") or []
    current_float_pnl = Decimal("0")
    for row in holding_rows:
        if abs(_dec(row.get("market_value_cny"))) <= Decimal("0.0001"):
            continue
        current_float_pnl += _dec(row.get("pnl_cny") or row.get("total_pnl_cny") or row.get("value_cny"))
    return current_float_pnl, total_pnl - current_float_pnl


def _attribution_reconciliation_pnl(attribution: dict[str, Any]) -> Decimal:
    """Return account-level PnL calibration that is not a closed trade."""
    reconciliation = ((attribution or {}).get("quality") or {}).get("return_reconciliation") or {}
    total = Decimal("0")
    for section in reconciliation.get("sections") or []:
        total += _dec(section.get("pnl_delta_cny"))
    return total


def _cashflow_breakdown(
    cur,
    *,
    start_date: Date,
    end_date: Date,
    excluded_accounts: set[str] | None = None,
) -> dict[str, Any]:
    excluded = sorted(str(item) for item in (excluded_accounts or set()) if item)
    cur.execute(
        """
        WITH linked_investment_return_cashflows AS (
            SELECT DISTINCT cashflow_id
            FROM pams_investment_trade_fact
            WHERE cashflow_id IS NOT NULL
              AND trade_type IN ('dividend', 'interest', 'coupon')
              AND COALESCE(LOWER(review_status), 'auto') NOT IN ('ignored', 'cancelled', 'canceled', 'void')
        )
        SELECT cf.cashflow_type,
               COALESCE(cf.category, '') AS category,
               COALESCE(cf.subcategory, '') AS subcategory,
               (linked.cashflow_id IS NOT NULL) AS is_linked_investment_return,
               COUNT(*) AS count,
               COALESCE(SUM(cf.amount_cny), 0) AS amount_cny
        FROM pams_cashflow_fact cf
        LEFT JOIN linked_investment_return_cashflows linked
               ON linked.cashflow_id = cf.cashflow_id
        WHERE cf.event_date >= %s
          AND cf.event_date <= %s
          AND NOT (cf.account_code = ANY(%s::text[]))
          AND cf.affects_nav IS TRUE
          AND COALESCE(LOWER(cf.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        GROUP BY cf.cashflow_type,
                 COALESCE(cf.category, ''),
                 COALESCE(cf.subcategory, ''),
                 (linked.cashflow_id IS NOT NULL)
        ORDER BY cf.cashflow_type, category, subcategory
        """,
        (start_date, end_date, excluded),
    )
    return _cashflow_breakdown_from_rows(list(cur.fetchall()))


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
            raw_json = valuation_state.get("raw_json") or {}
            terminal_account_values = raw_json.get("account_values_cny") or {}
            terminal_date, terminal_nav_bucket, ending_nav = _terminal_nav_payload(valuation_state)
            if start_date > terminal_date:
                start_date = terminal_date
            if not terminal_account_values:
                account_cfg_for_values = _load_accounts_config()
                # Defensive fallback for old valuation states built before
                # account_values_cny existed in raw_json.
                from services.portfolio_state import load_account_balances, load_investment_positions

                account_balances = load_account_balances(cur, base, account_cfg_for_values)
                investment_positions = load_investment_positions(
                    cur,
                    base,
                    account_cfg_for_values,
                    account_balances=account_balances,
                )
                terminal_account_values = current_nav_account_values_from_parts(
                    account_balances=account_balances,
                    investment_positions=investment_positions,
                )
            nav_payload = build_nav_weekly_payload(
                cur,
                start_date=start_date,
                base_currency=base,
                overview_payload=overview_payload,
                owner_ratio_case_sql=_owner_ratio_case_sql(account_cfg),
                account_owner_ratio=_account_owner_ratio_lookup(account_cfg),
                terminal_date=terminal_date,
                terminal_nav_bucket=terminal_nav_bucket,
                terminal_account_values_cny=terminal_account_values,
                interval="daily",
            )
            rows = nav_payload.get("rows") or []
            beginning_nav = _dec(rows[0].get("netWorth")) if rows else Decimal("0")
            ending_nav = _dec(rows[-1].get("netWorth")) if rows else ending_nav
            cashflows = _cashflow_breakdown(
                cur,
                start_date=start_date,
                end_date=terminal_date,
                excluded_accounts=_bridge_cashflow_excluded_accounts(account_cfg),
            )
            attribution = build_return_attribution_payload(cur, base_currency=base)
            attribution_pnl = _dec(
                attribution.get("canonical_total_pnl_cny")
                or attribution.get("total_pnl_cny")
            )
            attribution_current_float_pnl, attribution_closed_realized_pnl = _attribution_pnl_basis(
                attribution,
                attribution_pnl,
            )
            attribution_reconciliation_pnl = _attribution_reconciliation_pnl(attribution)
            attribution_effect_float_pnl = attribution_current_float_pnl + attribution_reconciliation_pnl
            attribution_actual_closed_pnl = attribution_closed_realized_pnl - attribution_reconciliation_pnl
            period_investment_effect = period_investment_effect_from_inputs(
                beginning_nav_cny=beginning_nav,
                ending_nav_cny=ending_nav,
                external_cashflow_cny=_dec(cashflows["external_net_cny"]),
            )
            bridge = build_bridge_reconciliation_from_inputs(
                beginning_nav_cny=beginning_nav,
                ending_nav_cny=ending_nav,
                external_cashflow_cny=_dec(cashflows["external_net_cny"]),
                investment_pnl_cny=period_investment_effect,
            )
            checks = _funding_closure_checks(
                cur,
                start_date=start_date,
                end_date=terminal_date,
                account_cfg=account_cfg,
            )

    growth_explanation = build_nav_growth_explanation_from_inputs(
        beginning_nav_cny=beginning_nav,
        ending_nav_cny=ending_nav,
        cashflows=cashflows,
        period_investment_effect_cny=period_investment_effect,
        attribution_pnl_cny=attribution_pnl,
        attribution_current_float_pnl_cny=attribution_current_float_pnl,
        attribution_closed_realized_pnl_cny=attribution_closed_realized_pnl,
        attribution_reconciliation_pnl_cny=attribution_reconciliation_pnl,
        bridge=bridge,
    )
    investment_attribution_gap = period_investment_effect - attribution_pnl
    payload = {
        "start": start_date,
        "end": terminal_date,
        "base_currency": base,
        "formula": (
            "ending_nav = beginning_nav + external_cashflow "
            "+ period_investment_effect + non_investment_adjustments + unexplained_residual"
        ),
        "beginning_nav_cny": beginning_nav,
        "ending_nav_cny": ending_nav,
        "valuation_state_source": "computed_current",
        "nav_series_method": nav_payload.get("method"),
        "reconciliation": bridge,
        "reconciliation_steps": build_nav_bridge_steps_from_inputs(
            beginning_nav_cny=beginning_nav,
            external_cashflow_cny=_dec(cashflows["external_net_cny"]),
            period_investment_effect_cny=period_investment_effect,
            unexplained_residual_cny=_dec(bridge["unexplained_residual_cny"]),
            ending_nav_cny=ending_nav,
        ),
        "terminal_account_values": _terminal_account_items(terminal_account_values),
        "cashflows": cashflows,
        "investment": {
            "period_investment_effect_cny": period_investment_effect,
            "canonical_total_pnl_cny": attribution_pnl,
            "raw_total_pnl_cny": _dec(attribution.get("total_pnl_cny")),
            "current_float_pnl_cny": attribution_current_float_pnl,
            "account_reconciliation_pnl_cny": attribution_reconciliation_pnl,
            "current_float_with_reconciliation_cny": attribution_effect_float_pnl,
            "closed_realized_pnl_cny": attribution_actual_closed_pnl,
            "cumulative_period_gap_cny": attribution_pnl - period_investment_effect,
            "float_period_gap_cny": attribution_effect_float_pnl - period_investment_effect,
            "attribution_gap_cny": period_investment_effect - attribution_pnl,
            "portfolio_nav_cny": _dec(attribution.get("portfolio_nav_cny")),
            "as_of": attribution.get("as_of"),
            "source": "nav_bridge.period_effect_vs_analysis.return-attribution",
        },
        "data_checks": checks,
        "quality": {
            **_bridge_quality(
                bridge=bridge,
                investment_attribution_gap_cny=investment_attribution_gap,
                checks=checks,
            )
        },
        "growth_explanation": growth_explanation,
    }
    return _norm(payload)
