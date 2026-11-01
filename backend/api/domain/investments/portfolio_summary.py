"""Portfolio summary read adapters.

These functions preserve legacy payload shapes while moving read-only summary
queries out of ``main.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from domain.investments.holdings_view import LATEST_KNOWN_HOLDINGS_CTE
from domain.investments.portfolio_valuation_state import (
    is_published_valuation_fresh_for_date,
    published_valuation_stale_meta,
)


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _date_key(value: Any) -> str:
    if not value:
        return ""
    text = str(value)[:10]
    return text if len(text) == 10 and text[4:5] == "-" and text[7:8] == "-" else ""


def _summary_fact_as_of(payload: dict[str, Any]) -> str:
    """Return the freshest fact date represented by a summary payload."""
    candidates = [
        payload.get("data_as_of_max"),
        payload.get("as_of"),
        (payload.get("totals") or {}).get("as_of"),
    ]
    keys = [_date_key(value) for value in candidates]
    keys = [value for value in keys if value]
    return max(keys) if keys else ""


def _incomplete_snapshot_accounts(cur, view_as_of):
    cur.execute(f"""WITH {LATEST_KNOWN_HOLDINGS_CTE}
        SELECT DISTINCT account_code,as_of FROM latest_known_holding""", (view_as_of,))
    pairs = {(str(row['account_code']), row['as_of']) for row in cur.fetchall()}
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    scope = load_account_snapshot_coverage(cur, pairs)
    return {account for (account, day), proof in scope.items() if proof.get('status') != 'complete'}


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
        WITH {LATEST_KNOWN_HOLDINGS_CTE}, config AS (
            SELECT %s::text AS base_currency, %s::date AS rate_date
        ), h AS (
            SELECT hs.*, config.base_currency, config.rate_date,
                CASE WHEN hs.currency = config.base_currency THEN 1
                     WHEN fx.rate IS NOT NULL THEN fx.rate
                     WHEN hs.currency = 'CNY' THEN 1 / NULLIF(base_fx.rate, 0)
                     WHEN config.base_currency = 'CNY' THEN source_fx.rate
                     ELSE source_fx.rate / NULLIF(base_fx.rate, 0) END AS fx_rate,
                CASE WHEN hs.currency = config.base_currency THEN config.rate_date
                     WHEN fx.rate IS NOT NULL THEN fx.as_of
                     WHEN hs.currency = 'CNY' THEN base_fx.as_of
                     WHEN config.base_currency = 'CNY' THEN source_fx.as_of
                     WHEN source_fx.as_of IS NOT NULL AND base_fx.as_of IS NOT NULL
                         THEN LEAST(source_fx.as_of, base_fx.as_of)
                     ELSE NULL END AS fx_rate_as_of
            FROM latest_known_holding hs CROSS JOIN config
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = config.base_currency AND quote_currency = hs.currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY' AND quote_currency = hs.currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) source_fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY' AND quote_currency = config.base_currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) base_fx ON TRUE
        )
        SELECT major_category, COUNT(*) AS holdings_count,
            COUNT(*) FILTER (WHERE fx_rate IS NULL) AS fx_missing_count,
            MIN(fx_rate_as_of) FILTER (WHERE currency <> base_currency) AS fx_rate_as_of,
            CASE WHEN COUNT(*) FILTER (WHERE fx_rate IS NULL) > 0 THEN 'unavailable'
                 WHEN BOOL_OR(fx_rate_as_of < rate_date) THEN 'estimated' ELSE 'daily_exact' END AS fx_precision,
            ROUND(SUM(h.market_value * h.fx_rate * {owner_ratio_sql})::numeric, 2) AS known_value_base,
            CASE WHEN COUNT(*) FILTER (WHERE fx_rate IS NULL) > 0 THEN NULL
                 ELSE ROUND(SUM(h.market_value * h.fx_rate * {owner_ratio_sql})::numeric, 2) END AS total_value_base,
            CASE WHEN COUNT(*) FILTER (WHERE h.cost IS NULL OR fx_rate IS NULL) > 0 THEN NULL
                 ELSE ROUND(SUM(h.cost * h.fx_rate * {owner_ratio_sql})::numeric, 2) END AS total_cost_base,
            CASE WHEN COUNT(*) FILTER (WHERE h.cost IS NULL OR fx_rate IS NULL) > 0 THEN NULL
                 ELSE ROUND(SUM((h.market_value - h.cost) * h.fx_rate * {owner_ratio_sql})::numeric, 2) END AS total_pnl_base
        FROM h GROUP BY major_category ORDER BY total_value_base DESC NULLS LAST
        """,
        (view_as_of, base_currency, view_as_of, *owner_ratio_params, *owner_ratio_params,
         *owner_ratio_params, *owner_ratio_params),
    )
    by_category = cur.fetchall()
    missing_fx = any(row.get('fx_missing_count', 0) for row in by_category)
    known_value = sum(float(row.get('known_value_base', row['total_value_base']) or 0) for row in by_category)
    total_value: float | None = None if missing_fx else sum(float(row["total_value_base"]) for row in by_category)
    total_cost = None if any(row['total_cost_base'] is None for row in by_category) else sum(float(row["total_cost_base"]) for row in by_category)
    total_pnl = None if any(row['total_pnl_base'] is None for row in by_category) else sum(float(row["total_pnl_base"]) for row in by_category)
    incomplete = sorted(_incomplete_snapshot_accounts(cur, view_as_of))
    if incomplete:
        total_value = total_cost = total_pnl = None

    result: dict[str, Any] = {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "base_currency": base_currency,
        "total": {
            "value": str(total_value) if total_value is not None else None,
            "cost": str(total_cost) if total_cost is not None else None,
            "pnl": str(total_pnl) if total_pnl is not None else None,
        },
        "by_category": [{key: _norm(value) for key, value in dict(row).items()} for row in by_category],
    }
    if incomplete:
        result.update(snapshot_scope_status='incomplete', snapshot_scope_unavailable_accounts=incomplete,
                      known_value=str(known_value))
    if missing_fx:
        result.update(fx_status='incomplete', known_value=str(known_value))
    elif any(row.get('fx_precision') == 'estimated' for row in by_category):
        result['fx_precision'] = 'estimated'

    return result


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
        WITH {LATEST_KNOWN_HOLDINGS_CTE}, config AS (
            SELECT %s::text AS base_currency, %s::date AS rate_date
        ), h AS (
            SELECT hs.*, config.base_currency, config.rate_date,
                CASE WHEN hs.currency = config.base_currency THEN 1
                     WHEN fx.rate IS NOT NULL THEN fx.rate
                     WHEN hs.currency = 'CNY' THEN 1 / NULLIF(base_fx.rate, 0)
                     WHEN config.base_currency = 'CNY' THEN source_fx.rate
                     ELSE source_fx.rate / NULLIF(base_fx.rate, 0) END AS fx_rate,
                CASE WHEN hs.currency = config.base_currency THEN config.rate_date
                     WHEN fx.rate IS NOT NULL THEN fx.as_of
                     WHEN hs.currency = 'CNY' THEN base_fx.as_of
                     WHEN config.base_currency = 'CNY' THEN source_fx.as_of
                     WHEN source_fx.as_of IS NOT NULL AND base_fx.as_of IS NOT NULL
                         THEN LEAST(source_fx.as_of, base_fx.as_of)
                     ELSE NULL END AS fx_rate_as_of
            FROM latest_known_holding hs CROSS JOIN config
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = config.base_currency AND quote_currency = hs.currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY' AND quote_currency = hs.currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) source_fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate, as_of FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY' AND quote_currency = config.base_currency
                  AND as_of <= config.rate_date AND rate > 0
                  AND rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                  AND NULLIF(BTRIM(source), '') IS NOT NULL
                ORDER BY as_of DESC LIMIT 1
            ) base_fx ON TRUE
        )
        SELECT h.account_code, a.display_name, a.base_currency AS account_currency,
            COUNT(*) AS holdings_count,
            COUNT(*) FILTER (WHERE h.fx_rate IS NULL) AS fx_missing_count,
            MIN(h.fx_rate_as_of) FILTER (WHERE h.currency <> h.base_currency) AS fx_rate_as_of,
            CASE WHEN COUNT(*) FILTER (WHERE h.fx_rate IS NULL) > 0 THEN 'unavailable'
                 WHEN BOOL_OR(h.fx_rate_as_of < h.rate_date) THEN 'estimated' ELSE 'daily_exact' END AS fx_precision,
            ROUND(SUM(h.market_value * h.fx_rate * {owner_ratio_sql})::numeric, 2) AS known_value_base,
            CASE WHEN COUNT(*) FILTER (WHERE h.fx_rate IS NULL) > 0 THEN NULL
                 ELSE ROUND(SUM(h.market_value * h.fx_rate * {owner_ratio_sql})::numeric, 2) END AS total_value_base
        FROM h JOIN pams_accounts a ON a.account_code = h.account_code
        GROUP BY h.account_code, a.display_name, a.base_currency
        ORDER BY total_value_base DESC NULLS LAST
        """,
        (view_as_of, base_currency, view_as_of, *owner_ratio_params, *owner_ratio_params),
    )
    rows = cur.fetchall()
    incomplete = _incomplete_snapshot_accounts(cur, view_as_of)
    items = []
    for raw in rows:
        item = {key: _norm(value) for key, value in dict(raw).items()}
        if str(item.get('account_code')) in incomplete:
            item.update(known_value_base=item.get('known_value_base', item['total_value_base']),
                        total_value_base=None, snapshot_scope_status='incomplete')
        if item.get('fx_missing_count'):
            item['fx_status'] = 'incomplete'
        items.append(item)

    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "base_currency": base_currency,
        "by_account": items,
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
    by_asset: dict[str, dict[str, Any]] = {}
    children: list[dict[str, Any]] = []
    represented_accounts: set[str] = set()
    represented_account_values: dict[str, Decimal] = {}
    missing_fx_accounts: set[str] = set()
    missing_fx_classes: set[str] = set()
    missing_value_accounts: set[str] = set()
    missing_value_classes: set[str] = set()
    daily_statuses: set[str] = set()
    known_value_components = 0
    estimated_fx_classes: set[str] = set()
    unknown_fx_classes: set[str] = set()
    excluded_from_nav = {str(row.get('account_code')) for row in (account_balances or [])
                         if row.get('account_code') and not row.get('include_in_nav', True)}

    for raw in holdings_payload.get("rows") or []:
        row = dict(raw)
        account = row.get("account") or row.get("account_code")
        if str(account or '') in excluded_from_nav or not row.get('include_in_nav', True):
            continue
        if account:
            account = str(account)
            represented_accounts.add(account)
        asset_liability = str(row.get("asset_liability") or "")
        asset_class = "LIABILITY" if asset_liability == "\u8d1f\u503a" else (row.get("major_category") or "UNCLASSIFIED")
        if row.get('fx_precision') == 'estimated':
            estimated_fx_classes.add(str(asset_class))
        elif row.get('fx_precision') == 'unknown':
            unknown_fx_classes.add(str(asset_class))
        raw_market_value = row.get("market_value_cny", row.get("market_value"))
        parsed_market_value = _known_decimal(raw_market_value)
        missing_value = parsed_market_value is None
        if not missing_value:
            known_value_components += 1
        missing_fx = missing_value and str(row.get("currency") or base_currency).upper() != str(base_currency).upper()
        if missing_value:
            if account:
                missing_value_accounts.add(str(account))
            missing_value_classes.add(str(asset_class))
        if missing_fx:
            if account:
                missing_fx_accounts.add(str(account))
            missing_fx_classes.add(str(asset_class))
        market_value = parsed_market_value if parsed_market_value is not None else Decimal(0)
        raw_pnl = row.get("holding_pnl_cny", row.get("holding_pnl"))
        holding_pnl = _known_decimal(raw_pnl)
        daily_value = row.get("daily_total_pnl_cny", row.get("daily_pnl_cny", row.get("daily_pnl")))
        daily_status = row.get("daily_pnl_status", row.get("daily_return_status"))
        daily_known = daily_value not in (None, "") and not row.get("daily_pnl_stale") and daily_status not in {"pending", "unknown", "unavailable"}
        daily_pnl = _known_decimal(daily_value) if daily_known else None
        effective_daily_status = "unknown" if daily_pnl is None else str(daily_status or "provided")
        daily_statuses.add(effective_daily_status)
        if account:
            represented_account_values[account] = represented_account_values.get(account, Decimal("0")) + market_value

        bucket = by_asset.setdefault(
            str(asset_class),
            {"market_value": Decimal("0"), "holding_pnl": Decimal("0"), "daily_pnl": Decimal("0")},
        )
        bucket["market_value"] += market_value
        if holding_pnl is None:
            bucket['holding_pnl'] = None
        elif bucket['holding_pnl'] is not None:
            bucket["holding_pnl"] += holding_pnl
        if daily_pnl is None:
            bucket["daily_pnl"] = None
        elif bucket["daily_pnl"] is not None:
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
                "fx_precision": row.get("fx_precision"),
                "fx_rate_as_of": row.get("fx_rate_as_of"),
                "market_value": None if missing_value else str(market_value),
                "holding_pnl": str(holding_pnl) if holding_pnl is not None else None,
                "daily_pnl": str(daily_pnl) if daily_pnl is not None else None,
                "daily_pnl_status": effective_daily_status,
            }
        )

    for raw in account_balances or []:
        row = dict(raw)
        account = str(row.get("account_code") or "")
        if not account or not row.get("include_in_nav", True):
            continue
        if row.get('fx_status') == 'unavailable':
            missing_fx_accounts.add(account)
            continue
        raw_amount = row.get("amount_cny")
        account_value = _known_decimal(raw_amount)
        if account_value is None:
            missing_value_accounts.add(account)
            continue
        market_value = account_value
        known_value_components += 1
        if account in represented_accounts:
            market_value -= represented_account_values.get(account, Decimal("0"))
        if account in represented_accounts and abs(market_value) <= Decimal("0.0001"):
            continue
        account_type = str(row.get("account_type") or "")
        asset_class = _account_balance_asset_class(account_type, market_value)
        bucket = by_asset.setdefault(
            str(asset_class),
            {"market_value": Decimal("0"), "holding_pnl": Decimal("0"), "daily_pnl": Decimal("0")},
        )
        bucket["market_value"] += market_value
        # A cash or residual balance is not evidence of its period return.
        bucket["holding_pnl"] = None
        bucket["daily_pnl"] = None
        daily_statuses.add("unknown")
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
                "holding_pnl": None,
                "daily_pnl": None,
                "daily_pnl_status": "unknown",
            }
        )

    by_asset_class = [
        {
            "asset_class": asset_class,
            "market_value": None if asset_class in missing_value_classes else str(values["market_value"]),
            "holding_pnl": str(values["holding_pnl"]) if values['holding_pnl'] is not None else None,
            "daily_pnl": str(values["daily_pnl"]) if values["daily_pnl"] is not None else None,
            **({'fx_precision': 'unknown'} if asset_class in unknown_fx_classes else
               {'fx_precision': 'estimated'} if asset_class in estimated_fx_classes else {}),
        }
        for asset_class, values in sorted(
            by_asset.items(),
            key=lambda item: item[1]["market_value"],
            reverse=True,
        )
    ]
    totals = {
        "market_value": sum((values["market_value"] for values in by_asset.values()), Decimal("0")) if by_asset else None,
        "holding_pnl": None if not by_asset or any(values['holding_pnl'] is None for values in by_asset.values()) else sum((values["holding_pnl"] for values in by_asset.values()), Decimal("0")),
        "daily_pnl": None if not by_asset or any(values['daily_pnl'] is None for values in by_asset.values()) else sum((values["daily_pnl"] for values in by_asset.values()), Decimal("0")),
    }
    incomplete = sorted({str(row.get('account_code') or row.get('account'))
                         for row in [*(holdings_payload.get('rows') or []), *(account_balances or [])]
                         if (row.get('account_code') or row.get('account')) and row.get('include_in_nav', True)
                         and str(row.get('account_code') or row.get('account')) not in excluded_from_nav
                         and row.get('snapshot_scope_status') in {'partial', 'unknown'}})
    known_market_value = totals['market_value'] if known_value_components else None
    if incomplete or missing_fx_accounts or missing_value_accounts or missing_value_classes:
        totals.update(market_value=None, holding_pnl=None, daily_pnl=None)

    result = {
        "as_of": holdings_payload.get("as_of"),
        "base_currency": base_currency,
        "totals": {key: str(value) if value is not None else None for key, value in totals.items()},
        "by_asset_class": by_asset_class,
        "children": children,
        "daily_pnl_status": "unknown" if totals["daily_pnl"] is None else
            "estimated" if "estimated" in daily_statuses or "mixed" in daily_statuses else
            "confirmed" if daily_statuses == {"confirmed"} else "provided",
    }
    if missing_value_accounts or missing_value_classes:
        result.update(value_status='incomplete', value_unavailable_accounts=sorted(missing_value_accounts))
    if incomplete:
        result.update(snapshot_scope_status='incomplete', snapshot_scope_unavailable_accounts=incomplete,
                      known_market_value=str(known_market_value) if known_market_value is not None else None)
    if missing_fx_accounts:
        result.update(fx_status='incomplete', fx_unavailable_accounts=sorted(missing_fx_accounts),
                      known_market_value=str(known_market_value) if known_market_value is not None else None)
    elif unknown_fx_classes:
        result['fx_precision'] = 'unknown'
    elif estimated_fx_classes:
        result['fx_precision'] = 'estimated'
    return result


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
    if 'holding_pnl' in totals and totals['holding_pnl'] is None:
        totals.update(current_float_pnl=None, closed_realized_pnl=None)
        out['totals'] = totals
        return out
    current_float_pnl: Decimal | None = _decimal(totals.get("holding_pnl"))
    rows = ((attribution_payload or {}).get("dimensions") or {}).get("holding") or []
    if rows:
        if any((market := _known_decimal(row.get("market_value_cny"))) is None or
               (abs(market) > Decimal("0.0001") and
                _known_decimal(row.get("pnl_cny", row.get("total_pnl_cny", row.get("value_cny")))) is None)
               for row in rows):
            current_float_pnl = None
        else:
            current_float_pnl = sum(
                (_decimal(row.get("pnl_cny", row.get("total_pnl_cny", row.get("value_cny"))))
                 for row in rows if abs(_decimal(row.get("market_value_cny"))) > Decimal("0.0001")),
                Decimal("0"),
            )
    totals["current_float_pnl"] = str(current_float_pnl) if current_float_pnl is not None else None

    if attribution_payload:
        canonical_total = attribution_payload.get("canonical_total_pnl_cny")
        raw_total = attribution_payload.get("total_pnl_cny")
        total_source = canonical_total if canonical_total not in (None, "") else raw_total
    else:
        total_source = None
    if total_source not in (None, ""):
        total_pnl = _decimal(total_source)
        totals["holding_pnl"] = str(total_pnl)
        totals["closed_realized_pnl"] = str(total_pnl - current_float_pnl) if current_float_pnl is not None else None

    out["totals"] = totals
    return out

def apply_published_valuation_state_to_summary(
    payload: dict[str, Any],
    valuation_state: dict[str, Any] | None,
    *, account_balances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use a fresh published portfolio valuation as the authoritative total."""
    if (payload.get('snapshot_scope_status') == 'incomplete' or payload.get('fx_status') == 'incomplete'
        or payload.get('value_status') == 'incomplete' or (payload.get('totals') or {}).get('market_value') is None):
        return {**payload, 'valuation_state': {'source': 'published', 'overlay_applied': False,
                                                'overlay_reason': 'snapshot_scope_incomplete' if payload.get('snapshot_scope_status') == 'incomplete' else 'current_valuation_incomplete'}}
    if not valuation_state or valuation_state.get("net_liquidation_value") in (None, ""):
        return payload
    from domain.investments.portfolio_valuation_state import published_daily_close_compatible
    if not published_daily_close_compatible(valuation_state, account_balances=account_balances):
        return {**payload, "valuation_state": {"source": "published", "overlay_applied": False,
                "overlay_reason": "daily_close_evidence_basis_mismatch",
                "as_of": str(valuation_state.get("valuation_date") or "")}}
    fact_as_of = _summary_fact_as_of(payload)
    if not is_published_valuation_fresh_for_date(valuation_state, fact_as_of):
        out = dict(payload)
        stale_meta = published_valuation_stale_meta(valuation_state, target_as_of=fact_as_of)
        if stale_meta:
            out["valuation_state"] = stale_meta
        return out

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
        "overlay_applied": True,
    }
    return out


def _known_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (ValueError, TypeError, ArithmeticError):
        return None


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
