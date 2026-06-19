"""Portfolio summary read adapters.

These functions preserve legacy payload shapes while moving read-only summary
queries out of ``main.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_holdings_summary_payload(
    cur,
    *,
    base_currency: str,
    view_as_of,
    owner_ratio_sql: str,
    owner_ratio_params: tuple[Any, ...] | list[Any],
) -> dict[str, Any]:
    """Return the legacy /api/holdings/summary payload."""
    cur.execute("SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    as_of = row["as_of"] if row else None

    if not as_of:
        return {"as_of": None, "base_currency": base_currency, "total": {}, "by_category": []}

    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest_position AS (
            SELECT
                hs.account_code,
                COALESCE(NULLIF(hs.symbol, ''), hs.name) AS position_key,
                MAX(hs.as_of) AS as_of
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
            WHERE hs.as_of <= %s
            GROUP BY hs.account_code, COALESCE(NULLIF(hs.symbol, ''), hs.name)
        ), h AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN latest_position lp
              ON lp.account_code = hs.account_code
             AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
             AND lp.as_of = hs.as_of
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            major_category,
            COUNT(*) as holdings_count,
            ROUND(SUM(h.market_value * fx.rate * {owner_ratio_sql})::numeric, 2) as total_value_base,
            ROUND(SUM(COALESCE(h.cost, 0) * fx.rate * {owner_ratio_sql})::numeric, 2) as total_cost_base,
            ROUND(SUM((h.market_value - COALESCE(h.cost, 0)) * fx.rate * {owner_ratio_sql})::numeric, 2) as total_pnl_base
        FROM h
        JOIN fx ON fx.quote_currency = h.currency
        GROUP BY major_category
        ORDER BY total_value_base DESC
        """,
        (view_as_of, view_as_of, base_currency, *owner_ratio_params, *owner_ratio_params, *owner_ratio_params),
    )
    by_category = cur.fetchall()

    total_value = sum(float(row["total_value_base"]) for row in by_category)
    total_cost = sum(float(row["total_cost_base"]) for row in by_category)
    total_pnl = sum(float(row["total_pnl_base"]) for row in by_category)

    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "base_currency": base_currency,
        "total": {
            "value": str(total_value),
            "cost": str(total_cost),
            "pnl": str(total_pnl),
        },
        "by_category": [{key: _norm(value) for key, value in dict(row).items()} for row in by_category],
    }


def build_holdings_by_account_payload(
    cur,
    *,
    base_currency: str,
    view_as_of,
    owner_ratio_sql: str,
    owner_ratio_params: tuple[Any, ...] | list[Any],
) -> dict[str, Any]:
    """Return the legacy /api/holdings/by-account payload."""
    cur.execute("SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    as_of = row["as_of"] if row else None

    if not as_of:
        return {"as_of": None, "by_account": []}

    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest_position AS (
            SELECT
                hs.account_code,
                COALESCE(NULLIF(hs.symbol, ''), hs.name) AS position_key,
                MAX(hs.as_of) AS as_of
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
            WHERE hs.as_of <= %s
            GROUP BY hs.account_code, COALESCE(NULLIF(hs.symbol, ''), hs.name)
        ), h AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN latest_position lp
              ON lp.account_code = hs.account_code
             AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
             AND lp.as_of = hs.as_of
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            h.account_code,
            a.display_name,
            a.base_currency as account_currency,
            COUNT(*) as holdings_count,
            ROUND(SUM(h.market_value * fx.rate * {owner_ratio_sql})::numeric, 2) as total_value_base
        FROM h
        JOIN fx ON fx.quote_currency = h.currency
        JOIN pams_accounts a ON a.account_code = h.account_code
        GROUP BY h.account_code, a.display_name, a.base_currency
        ORDER BY total_value_base DESC
        """,
        (view_as_of, view_as_of, base_currency, *owner_ratio_params),
    )
    rows = cur.fetchall()

    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "base_currency": base_currency,
        "by_account": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }


def build_summary2_latest_payload(
    cur,
    *,
    base_currency: str,
    view_as_of,
    owner_ratio_sql: str,
    owner_ratio_params: tuple[Any, ...] | list[Any],
    excluded_accounts: list[str],
) -> dict[str, Any]:
    """Return the legacy /summary2/latest payload for the overview page."""
    cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {
            "as_of": None,
            "base_currency": "CNY",
            "totals": {},
            "by_asset_class": [],
            "children": [],
        }

    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest_position AS (
            SELECT
                hs.account_code,
                COALESCE(NULLIF(hs.symbol, ''), hs.name) AS position_key,
                MAX(hs.as_of) AS as_of
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
            WHERE NOT (hs.account_code = ANY(%s::text[]))
              AND hs.as_of <= %s
            GROUP BY hs.account_code, COALESCE(NULLIF(hs.symbol, ''), hs.name)
        ), h AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN latest_position lp
              ON lp.account_code = hs.account_code
             AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
             AND lp.as_of = hs.as_of
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                 ELSE h.major_category END AS asset_class,
            COALESCE(SUM(h.market_value * fx.rate * {owner_ratio_sql}), 0) AS market_value,
            COALESCE(SUM(COALESCE(h.holding_pnl, 0) * fx.rate * {owner_ratio_sql}), 0) AS holding_pnl,
            0 AS daily_pnl
        FROM h
        JOIN fx ON fx.quote_currency = h.currency
        GROUP BY CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                      ELSE h.major_category END
        ORDER BY market_value DESC
        """,
        (view_as_of, excluded_accounts, view_as_of, base_currency, *owner_ratio_params, *owner_ratio_params),
    )
    by_asset_class = cur.fetchall()

    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest_position AS (
            SELECT
                hs.account_code,
                COALESCE(NULLIF(hs.symbol, ''), hs.name) AS position_key,
                MAX(hs.as_of) AS as_of
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
            WHERE NOT (hs.account_code = ANY(%s::text[]))
              AND hs.as_of <= %s
            GROUP BY hs.account_code, COALESCE(NULLIF(hs.symbol, ''), hs.name)
        ), h AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN latest_position lp
              ON lp.account_code = hs.account_code
             AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
             AND lp.as_of = hs.as_of
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                 ELSE h.major_category END AS asset_class,
            h.account_code AS venue,
            h.account_code AS account,
            h.minor_category AS type,
            h.symbol, h.name, h.currency,
            h.market_value * fx.rate * {owner_ratio_sql} AS market_value,
            COALESCE(h.holding_pnl, 0) * fx.rate * {owner_ratio_sql} AS holding_pnl,
            0 AS daily_pnl
        FROM h
        JOIN fx ON fx.quote_currency = h.currency
        ORDER BY CASE WHEN h.asset_liability = '负债' THEN 'LIABILITY'
                      ELSE h.major_category END,
                 market_value DESC
        """,
        (view_as_of, excluded_accounts, view_as_of, base_currency, *owner_ratio_params, *owner_ratio_params),
    )
    children = cur.fetchall()

    totals = {"market_value": 0, "holding_pnl": 0, "daily_pnl": 0}
    for row in by_asset_class:
        totals["market_value"] += float(row["market_value"] or 0)
        totals["holding_pnl"] += float(row["holding_pnl"] or 0)
        totals["daily_pnl"] += float(row["daily_pnl"] or 0)

    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "base_currency": base_currency,
        "totals": {key: str(value) for key, value in totals.items()},
        "by_asset_class": [{key: _norm(value) for key, value in dict(row).items()} for row in by_asset_class],
        "children": [{key: _norm(value) for key, value in dict(row).items()} for row in children],
    }


def build_summary2_from_holdings_latest_payload(
    holdings_payload: dict[str, Any],
    *,
    base_currency: str,
    account_balances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build /summary2/latest from the same rows used by the holdings page.

    The legacy summary SQL predates several holdings-page adjustments: owner
    ratios, realized PnL enrichment, IBKR live marks, FX daily PnL, and stale
    daily-PnL suppression. Reusing the holdings rows keeps the dashboard and
    holdings page on one accounting basis.
    """
    by_asset: dict[str, dict[str, Decimal]] = {}
    children: list[dict[str, Any]] = []
    represented_accounts: set[str] = set()
    represented_account_values: dict[str, Decimal] = {}

    for raw in holdings_payload.get("rows") or []:
        row = dict(raw)
        account = row.get("account") or row.get("account_code")
        if account:
            account = str(account)
            represented_accounts.add(account)
        asset_liability = str(row.get("asset_liability") or "")
        asset_class = "LIABILITY" if asset_liability == "\u8d1f\u503a" else (row.get("major_category") or "UNCLASSIFIED")
        market_value = _decimal(row.get("market_value_cny", row.get("market_value")))
        holding_pnl = _decimal(row.get("holding_pnl_cny", row.get("holding_pnl")))
        daily_pnl = _decimal(
            row.get(
                "daily_total_pnl_cny",
                row.get("daily_pnl_cny", row.get("daily_pnl")),
            )
        )
        if account:
            represented_account_values[account] = represented_account_values.get(account, Decimal("0")) + market_value

        bucket = by_asset.setdefault(
            str(asset_class),
            {"market_value": Decimal("0"), "holding_pnl": Decimal("0"), "daily_pnl": Decimal("0")},
        )
        bucket["market_value"] += market_value
        bucket["holding_pnl"] += holding_pnl
        bucket["daily_pnl"] += daily_pnl

        children.append(
            {
                "asset_class": asset_class,
                "venue": account,
                "account": account,
                "type": row.get("type") or row.get("minor_category"),
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "currency": row.get("currency"),
                "market_value": str(market_value),
                "holding_pnl": str(holding_pnl),
                "daily_pnl": str(daily_pnl),
            }
        )

    for raw in account_balances or []:
        row = dict(raw)
        account = str(row.get("account_code") or "")
        if not account or not row.get("include_in_nav", True):
            continue
        market_value = _decimal(row.get("amount_cny"))
        if account in represented_accounts:
            market_value -= represented_account_values.get(account, Decimal("0"))
        if abs(market_value) <= Decimal("0.0001"):
            continue
        account_type = str(row.get("account_type") or "")
        asset_class = _account_balance_asset_class(account_type, market_value)
        bucket = by_asset.setdefault(
            str(asset_class),
            {"market_value": Decimal("0"), "holding_pnl": Decimal("0"), "daily_pnl": Decimal("0")},
        )
        bucket["market_value"] += market_value
        children.append(
            {
                "asset_class": asset_class,
                "venue": account,
                "account": account,
                "type": account_type,
                "symbol": account.upper(),
                "name": row.get("display_name") or account,
                "currency": row.get("currency") or base_currency,
                "market_value": str(market_value),
                "holding_pnl": "0",
                "daily_pnl": "0",
            }
        )

    by_asset_class = [
        {
            "asset_class": asset_class,
            "market_value": str(values["market_value"]),
            "holding_pnl": str(values["holding_pnl"]),
            "daily_pnl": str(values["daily_pnl"]),
        }
        for asset_class, values in sorted(
            by_asset.items(),
            key=lambda item: item[1]["market_value"],
            reverse=True,
        )
    ]
    totals = {
        "market_value": sum((values["market_value"] for values in by_asset.values()), Decimal("0")),
        "holding_pnl": sum((values["holding_pnl"] for values in by_asset.values()), Decimal("0")),
        "daily_pnl": sum((values["daily_pnl"] for values in by_asset.values()), Decimal("0")),
    }

    return {
        "as_of": holdings_payload.get("as_of"),
        "base_currency": base_currency,
        "totals": {key: str(value) for key, value in totals.items()},
        "by_asset_class": by_asset_class,
        "children": children,
    }


def apply_return_attribution_pnl_to_summary(
    payload: dict[str, Any],
    attribution_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Use return attribution as the cumulative PnL basis for summary2.

    Current holdings snapshots only contain open-position floating PnL. Return
    attribution also includes zero-value closed positions, so it is the better
    source for the user-facing cumulative total PnL while still exposing the
    current floating PnL separately.
    """
    out = dict(payload)
    totals = dict(out.get("totals") or {})
    current_float_pnl = _decimal(totals.get("holding_pnl"))
    rows = ((attribution_payload or {}).get("dimensions") or {}).get("holding") or []
    if rows:
        current_float_pnl = sum(
            (
                _decimal(row.get("pnl_cny", row.get("total_pnl_cny", row.get("value_cny"))))
                for row in rows
                if abs(_decimal(row.get("market_value_cny"))) > Decimal("0.0001")
            ),
            Decimal("0"),
        )
    totals["current_float_pnl"] = str(current_float_pnl)

    if attribution_payload and attribution_payload.get("total_pnl_cny") not in (None, ""):
        total_pnl = _decimal(attribution_payload.get("total_pnl_cny"))
        totals["holding_pnl"] = str(total_pnl)
        totals["closed_realized_pnl"] = str(total_pnl - current_float_pnl)

    out["totals"] = totals
    return out

def apply_published_valuation_state_to_summary(
    payload: dict[str, Any],
    valuation_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Use published portfolio valuation as the authoritative summary total."""
    if not valuation_state or valuation_state.get("net_liquidation_value") in (None, ""):
        return payload
    out = dict(payload)
    totals = dict(out.get("totals") or {})
    totals["market_value"] = str(_decimal(valuation_state.get("net_liquidation_value")))
    out["totals"] = totals
    out["as_of"] = str(valuation_state.get("valuation_date") or valuation_state.get("as_of") or out.get("as_of"))
    out["valuation_state"] = {
        "source": "published",
        "as_of": str(valuation_state.get("valuation_date") or valuation_state.get("as_of") or ""),
        "cycle": valuation_state.get("cycle"),
        "status": valuation_state.get("status"),
        "data_completeness": valuation_state.get("data_completeness"),
        "net_liquidation_value": str(_decimal(valuation_state.get("net_liquidation_value"))),
        "assets_cny": str(_decimal(valuation_state.get("assets_cny"))),
        "liabilities_cny": str(_decimal(valuation_state.get("liabilities_cny"))),
        "cash_cny": str(_decimal(valuation_state.get("cash_cny"))),
        "source_hash": valuation_state.get("source_hash"),
    }
    return out


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _account_balance_asset_class(account_type: str, amount_cny: Decimal) -> str:
    text = str(account_type or "").lower()
    if amount_cny < 0 or text in {"liability", "credit_card"}:
        return "LIABILITY"
    if text in {"benefit"}:
        return "\u516c\u79ef\u91d1"
    if text in {"receivable"}:
        return "\u5e94\u6536\u6b3e"
    return "\u73b0\u91d1"
