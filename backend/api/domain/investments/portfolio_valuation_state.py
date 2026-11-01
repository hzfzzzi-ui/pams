"""Published portfolio valuation state service.

This module is the write boundary for the canonical portfolio value used by the
investment pages. It deliberately reuses PortfolioState parts and the NAV bucket
helper so the accounting formula stays in one place:

    net liquidation value = assets - liabilities
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date as Date
from decimal import Decimal
from typing import Any

import psycopg2.extras

from domain.investments.portfolio_state import (
    _load_accounts_config,
    _norm,
    _table_exists,
    get_conn,
    _owner_ratio,
    load_account_balances,
    load_investment_positions,
)
from domain.investments.valuation_nav import current_nav_bucket_from_parts
from domain.investments.valuation_nav import current_nav_account_values_from_parts


STATE_TABLE = "valuation_results"
STATE_COLUMNS = """valuation_date,cycle,base_currency,status,data_completeness,
    net_liquidation_value,assets_cny,liabilities_cny,gross_exposure_cny,investment_value_cny,
    cash_cny,daily_pnl_cny,total_pnl_cny,source_run_id,source_hash,raw_json,published_at,created_at,updated_at"""
VALID_STATUSES = {"draft", "published", "failed"}
CASH_ACCOUNT_TYPES = {
    "benefit",
    "bridge",
    "cash_management",
    "funding",
    "payment",
    "receivable",
    "wallet",
}


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _date_key(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def published_valuation_date_key(row: dict[str, Any] | None) -> str | None:
    """Return the valuation date used for freshness checks."""
    if not row:
        return None
    return _date_key(row.get("valuation_date") or row.get("as_of"))


def is_published_valuation_fresh_for_date(
    row: dict[str, Any] | None,
    target_as_of: Any,
) -> bool:
    """True when a published valuation can safely override a target fact date."""
    state_date = published_valuation_date_key(row)
    if not state_date:
        return False
    target_date = _date_key(target_as_of)
    if not target_date:
        return True
    return state_date >= target_date


def published_valuation_stale_meta(
    row: dict[str, Any] | None,
    *,
    target_as_of: Any,
    reason: str = "stale_valuation_state",
) -> dict[str, Any] | None:
    """Return compact stale-overlay metadata without authorizing an override."""
    meta = published_valuation_state_meta(row)
    if not meta:
        return None
    meta.update(
        {
            "overlay_applied": False,
            "overlay_reason": reason,
            "target_as_of": _date_key(target_as_of) or "",
        }
    )
    return meta


def _state_as_of(
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
) -> str:
    values: list[str | None] = []
    values.extend(_date_key(row.get("as_of")) for row in account_balances)
    values.extend(_date_key(row.get("as_of")) for row in investment_positions)
    valid_values = [value for value in values if value]
    return max(valid_values) if valid_values else Date.today().isoformat()


def _state_hash(payload: dict[str, Any]) -> str:
    stable = {
        key: payload.get(key)
        for key in (
            "valuation_date",
            "cycle",
            "base_currency",
            "data_completeness",
            "net_liquidation_value",
            "assets_cny",
            "liabilities_cny",
            "gross_exposure_cny",
            "investment_value_cny",
            "cash_cny",
            "daily_pnl_cny",
            "total_pnl_cny",
            "raw_json",
        )
    }
    raw = json.dumps(_norm(stable), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_db_row(row: dict[str, Any] | None, *, source: str | None = None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    payload["as_of"] = payload.get("valuation_date")
    if source:
        payload["source"] = source
    return _norm(payload)


def published_nav_bucket_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a NAV bucket using the published valuation-state row fields."""
    if not row:
        return None
    result: dict[str, Any] = {
        "assets": _dec(row.get("assets_cny")),
        "liabilities": _dec(row.get("liabilities_cny")),
        "net_worth": _dec(row.get("net_liquidation_value")),
    }
    raw = row.get("raw_json")
    if isinstance(raw, dict) and isinstance(raw.get("ibkr_daily_close"), dict):
        result["ibkr_daily_close"] = dict(raw["ibkr_daily_close"])
    return result


def published_valuation_state_meta(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return compact JSON metadata for downstream endpoints."""
    if not row:
        return None
    return _norm(
        {
            "valuation_date": row.get("valuation_date"),
            "as_of": row.get("valuation_date"),
            "cycle": row.get("cycle"),
            "base_currency": row.get("base_currency"),
            "status": row.get("status"),
            "data_completeness": row.get("data_completeness"),
            "net_liquidation_value": row.get("net_liquidation_value"),
            "assets_cny": row.get("assets_cny"),
            "liabilities_cny": row.get("liabilities_cny"),
            "cash_cny": row.get("cash_cny"),
            "gross_exposure_cny": row.get("gross_exposure_cny"),
            "source_hash": row.get("source_hash"),
            "published_at": row.get("published_at"),
            "source": "published",
        }
    )


def apply_published_valuation_state_to_return_series_payload(
    payload: dict[str, Any],
    valuation_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay the published net liquidation value onto the terminal return row.

    Return-series rows still own PnL and TWR calculations. The published state
    only replaces the terminal portfolio value denominator so analysis charts do
    not show a different current portfolio value from summary/NAV/risk pages.
    """
    meta = published_valuation_state_meta(valuation_state)
    if not meta or meta.get("net_liquidation_value") in (None, ""):
        return payload
    rows = [dict(row) for row in (payload.get("rows") or [])]
    if not rows:
        return payload
    state_date = str(meta.get("as_of") or meta.get("valuation_date") or "")[:10]
    latest = rows[-1]
    latest_date = str(latest.get("date") or latest.get("asOfDate") or "")[:10]
    if state_date and latest_date and state_date != latest_date:
        out = dict(payload)
        out["valuation_state"] = {**meta, "overlay_applied": False, "overlay_reason": "date_mismatch"}
        return _norm(out)

    latest["market_value"] = meta["net_liquidation_value"]
    latest["net_liquidation_value"] = meta["net_liquidation_value"]
    latest["assets_cny"] = meta["assets_cny"]
    latest["liabilities_cny"] = meta["liabilities_cny"]
    latest["valuation_state_source"] = "published"
    rows[-1] = latest
    out = dict(payload)
    out["rows"] = rows
    out["valuation_state"] = {**meta, "overlay_applied": True}
    return _norm(out)


def apply_summary_pnl_to_return_series_payload(
    payload: dict[str, Any],
    summary_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay the terminal return row with summary2's cumulative PnL basis.

    The investment return chart and the holdings page should speak the same
    accounting language at the current endpoint: total PnL is the user-facing
    cumulative holdings PnL from summary2, current_float_pnl is open-position
    floating PnL, and closed_realized_pnl is the closed/realized remainder.
    """
    if not summary_payload:
        return payload
    totals = dict(summary_payload.get("totals") or {})
    summary_total_pnl = totals.get("holding_pnl")
    if summary_total_pnl in (None, ""):
        return payload

    rows = [dict(row) for row in (payload.get("rows") or [])]
    if not rows:
        return payload

    latest = rows[-1]
    summary_date = str(summary_payload.get("as_of") or "")[:10]
    latest_date = str(latest.get("date") or latest.get("asOfDate") or "")[:10]
    meta = {
        "source": "summary2.latest",
        "as_of": summary_date,
        "overlay_applied": False,
    }
    if summary_date and latest_date and summary_date != latest_date:
        out = dict(payload)
        out["return_pnl_state"] = {**meta, "overlay_reason": "date_mismatch"}
        return _norm(out)

    total_pnl = _dec(summary_total_pnl)
    current_float_pnl = _dec(totals.get("current_float_pnl"))
    closed_realized_pnl = _dec(totals.get("closed_realized_pnl"))
    market_value = totals.get("market_value")
    cost = _dec(latest.get("cost"))

    latest["total_pnl"] = total_pnl
    latest["position_pnl"] = current_float_pnl
    latest["realized_income"] = closed_realized_pnl
    if market_value not in (None, ""):
        latest["market_value"] = _dec(market_value)
        latest["net_liquidation_value"] = _dec(market_value)
    if cost > Decimal("0.0001"):
        latest["return_rate"] = total_pnl / cost
    latest["return_pnl_source"] = "summary2.latest"
    rows[-1] = latest

    out = dict(payload)
    out["rows"] = rows
    out["return_pnl_state"] = {
        **meta,
        "overlay_applied": True,
        "total_pnl": total_pnl,
        "current_float_pnl": current_float_pnl,
        "closed_realized_pnl": closed_realized_pnl,
    }
    return _norm(out)


def apply_holdings_category_pnl_to_section_return_series_payload(
    payload: dict[str, Any],
    holdings_payload: dict[str, Any] | None,
    *,
    source: str = "holdings.by_category",
) -> dict[str, Any]:
    """Overlay a section return payload with current category holdings PnL.

    Section return charts can be sourced from historical snapshots or platform
    reported anchors. At the terminal point, the user-facing section total must
    still reconcile to the holdings table: open-position float PnL plus closed
    realized PnL, divided by the same rows' cost basis.
    """
    if not holdings_payload:
        return payload
    holding_rows = [dict(row) for row in (holdings_payload.get("rows") or [])]
    rows = [dict(row) for row in (payload.get("rows") or [])]
    if not holding_rows or not rows:
        return payload

    latest = rows[-1]
    holdings_date = str(holdings_payload.get("as_of") or "")[:10]
    latest_date = str(latest.get("date") or latest.get("asOfDate") or "")[:10]
    meta = {
        "source": source,
        "as_of": holdings_date,
        "overlay_applied": False,
    }
    try:
        Date.fromisoformat(holdings_date)
        Date.fromisoformat(latest_date)
    except (TypeError, ValueError):
        out = dict(payload)
        out["section_pnl_state"] = {**meta, "overlay_reason": "date_unverified"}
        return _norm(out)
    if holdings_date and latest_date and holdings_date != latest_date:
        out = dict(payload)
        out["section_pnl_state"] = {**meta, "overlay_reason": "date_mismatch"}
        return _norm(out)

    def known(value):
        if value in (None, ""):
            return None
        try:
            number = Decimal(str(value))
            return number if number.is_finite() else None
        except (TypeError, ValueError, ArithmeticError):
            return None

    daily_values = [known(holding.get("daily_total_pnl_cny", holding.get("daily_pnl_cny", holding.get("daily_pnl"))))
                    for holding in holding_rows]
    daily_pnl = sum(daily_values, Decimal(0)) if all(value is not None for value in daily_values) else None
    values = [known(holding.get('market_value_cny', holding.get('market_value'))) for holding in holding_rows]
    if (any(value is None for value in values) or
        any(known(holding.get('cost_cny', holding.get('cost'))) is None or
            known(holding.get('holding_pnl_cny', holding.get('holding_pnl'))) is None for holding in holding_rows)):
        out = dict(payload)
        out['section_pnl_state'] = {**meta, 'overlay_reason': 'holding_value_cost_or_pnl_unknown'}
        out['summary'] = {**(payload.get('summary') or {}), 'total_return': None,
                          'total_return_rate': None, 'annualized_return_rate': None}
        out['rows'] = rows
        out['rows'][-1] = {**rows[-1], **{key: None for key in ('cost', 'total_pnl', 'position_pnl',
                            'realized_income', 'return_rate', 'display_return_rate',
                            'time_weighted_return_rate', 'capital_return_rate')}}
        out['rows'][-1]['market_value'] = sum(values, Decimal(0)) if all(value is not None for value in values) else None
        out['rows'][-1]['daily_return_amount'] = daily_pnl
        out['rows'][-1]['daily_return_rate'] = None
        return _norm(out)

    total_value = Decimal("0")
    total_cost = Decimal("0")
    total_pnl = Decimal("0")
    current_float_pnl = Decimal("0")
    for holding in holding_rows:
        market_value = _dec(holding.get("market_value_cny", holding.get("market_value")))
        cost = _dec(holding.get("cost_cny", holding.get("cost")))
        pnl = _dec(holding.get("holding_pnl_cny", holding.get("holding_pnl")))
        total_value += market_value
        total_cost += cost
        total_pnl += pnl
        if not holding.get("is_closed"):
            current_float_pnl += market_value - cost

    realized_pnl = total_pnl - current_float_pnl
    return_rate = total_pnl / total_cost if total_cost > Decimal("0.0001") else None
    # The holdings return is a current simple cost ratio. It does not establish
    # the historical series' flow-adjusted basis, so neither old points nor an
    # annualized/daily rate can be derived by scaling the original curve.
    terminal_basis_unchanged = (
        return_rate is not None and daily_pnl is not None and
        all(known(latest.get(key)) == expected for key, expected in (
            ('market_value', total_value), ('cost', total_cost), ('total_pnl', total_pnl),
            ('return_rate', return_rate), ('daily_return_amount', daily_pnl),
        ))
    )
    latest = rows[-1]
    latest["market_value"] = total_value
    latest["total_pnl"] = total_pnl
    latest["position_pnl"] = current_float_pnl
    latest["realized_income"] = realized_pnl
    latest["cost"] = total_cost
    latest["return_rate"] = return_rate
    latest["display_return_rate"] = return_rate
    latest["daily_return_amount"] = daily_pnl
    if not terminal_basis_unchanged:
        latest["daily_return_rate"] = None
        latest["time_weighted_return_rate"] = None
        latest["capital_return_rate"] = None
    latest["return_pnl_source"] = source
    rows[-1] = latest

    summary = dict(payload.get("summary") or {})
    summary["total_return"] = total_pnl
    summary["total_return_rate"] = return_rate
    if not terminal_basis_unchanged:
        summary["annualized_return_rate"] = None

    out = dict(payload)
    out["summary"] = summary
    out["rows"] = rows
    out["section_pnl_state"] = {
        **meta,
        "overlay_applied": True,
        "total_value": total_value,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "current_float_pnl": current_float_pnl,
        "closed_realized_pnl": realized_pnl,
        "return_rate": return_rate,
        "return_rate_basis": "current_holding_pnl_over_cost",
        "historical_values_recalibrated": False,
        "daily_and_annualized_rate_reason": ("original_rates_retained_basis_unverified" if terminal_basis_unchanged
                                             else "historical_basis_and_cash_flows_unverified"),
        "row_count": len(holding_rows),
    }
    return _norm(out)


def build_portfolio_valuation_state_from_parts(
    *,
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
    base_currency: str = "CNY",
    cycle: str = "manual_publish",
    status: str = "draft",
    as_of: str | Date | None = None,
) -> dict[str, Any]:
    """Build one canonical valuation state from PortfolioState components."""
    status_value = str(status or "draft")
    if status_value not in VALID_STATUSES:
        raise ValueError(f"Invalid portfolio valuation state status: {status_value}")

    base = str(base_currency or "CNY").upper()
    valuation_date = _date_key(as_of) or _state_as_of(account_balances, investment_positions)
    represented_accounts = {
        str(row.get("account_code") or "")
        for row in investment_positions
        if row.get("account_code") and row.get('include_in_nav', True)
    }
    nav_bucket = current_nav_bucket_from_parts(
        account_balances=account_balances,
        investment_positions=investment_positions,
    )
    account_values_cny = current_nav_account_values_from_parts(
        account_balances=account_balances,
        investment_positions=investment_positions,
    )

    gross_exposure = Decimal("0")
    investment_value = Decimal("0")
    cash_value = Decimal("0")
    total_pnl = Decimal("0")
    missing_pnl_positions = []
    position_counts = {"cash": 0, "liability": 0, "investment": 0}
    for row in investment_positions:
        if not row.get('include_in_nav', True):
            continue
        value = _dec(row.get("market_value_cny"))
        asset_type = str(row.get("asset_type") or "")
        if asset_type == "cash":
            cash_value += value
            position_counts["cash"] += 1
        elif asset_type == "liability" or value < 0:
            position_counts["liability"] += 1
        else:
            investment_value += value
            gross_exposure += abs(value)
            position_counts["investment"] += 1
        if row.get('holding_pnl_cny') is None:
            missing_pnl_positions.append(str(row.get('asset_id') or row.get('symbol') or row.get('asset_name') or ''))
        else:
            total_pnl += _dec(row['holding_pnl_cny'])

    unrepresented_accounts = []
    for row in account_balances:
        account_code = str(row.get("account_code") or "")
        if not row.get("include_in_nav", True) or account_code in represented_accounts:
            continue
        amount = _dec(row.get("amount_cny"))
        unrepresented_accounts.append(account_code)
        if amount > 0 and str(row.get("account_type") or "").lower() in CASH_ACCOUNT_TYPES:
            cash_value += amount

    missing_accounts = [
        str(row.get("account_code") or "")
        for row in account_balances
        if str(row.get("review_status") or "") == "missing_data"
    ]
    missing_positions = [
        str(row.get("asset_id") or row.get("symbol") or row.get("asset_name") or "")
        for row in investment_positions
        if not row.get("as_of")
    ]
    incomplete_scope = sorted({str(row.get('account_code')) for row in [*account_balances, *investment_positions]
                               if row.get('account_code') and row.get('include_in_nav', True)
                               and (row.get('snapshot_scope_status') in {'partial', 'unknown'} or row.get('fx_status') == 'unavailable')})
    has_nav_members = any(row.get("include_in_nav", True) for row in [*account_balances, *investment_positions])
    data_completeness = "complete" if has_nav_members and not missing_accounts and not missing_positions and not missing_pnl_positions and not incomplete_scope else "partial"
    raw_json = {
        "formula": "net_liquidation_value = assets_cny - liabilities_cny",
        "source": "portfolio_state_parts/current_nav_bucket_from_parts",
        "accounts_count": len(account_balances),
        "positions_count": len(investment_positions),
        "position_counts": position_counts,
        "represented_accounts": sorted(represented_accounts),
        "unrepresented_accounts": sorted(unrepresented_accounts),
        "missing_accounts": missing_accounts,
        "missing_positions": missing_positions,
        "missing_pnl_positions": missing_pnl_positions,
        "snapshot_scope_unavailable_accounts": incomplete_scope,
        "nav_bucket": {
            "assets": nav_bucket["assets"],
            "liabilities": nav_bucket["liabilities"],
            "net_worth": nav_bucket["net_worth"],
        },
        "account_values_cny": account_values_cny,
    }
    from domain.brokers.ibkr import ibkr_daily_close_read as daily_close
    if daily_close.configured():
        from domain.investments.valuation_nav import current_daily_close_accounts
        applied = current_daily_close_accounts(account_balances, investment_positions)
        raw_json["ibkr_daily_close"] = {
            **applied["ibkr"]["daily_close"], "net_worth_cny": str(applied["ibkr"]["amount_cny"])
        } if "ibkr" in applied else {
            "status": "unconfirmed", "reasons": ["confirmed_account_close_not_applied"]}
        if "ibkr" not in applied:
            data_completeness = "partial"
    state = {
        "valuation_date": valuation_date,
        "cycle": str(cycle or "manual_publish"),
        "base_currency": base,
        "status": status_value,
        "data_completeness": data_completeness,
        "net_liquidation_value": nav_bucket["net_worth"] if has_nav_members and not incomplete_scope else None,
        "assets_cny": nav_bucket["assets"] if has_nav_members and not incomplete_scope else None,
        "liabilities_cny": nav_bucket["liabilities"] if has_nav_members and not incomplete_scope else None,
        "gross_exposure_cny": gross_exposure,
        "investment_value_cny": investment_value,
        "cash_cny": cash_value,
        "daily_pnl_cny": None,
        "total_pnl_cny": total_pnl if not missing_pnl_positions else None,
        "source_run_id": None,
        "source_hash": None,
        "raw_json": raw_json,
        "published_at": None,
    }
    state["source_hash"] = _state_hash(state)
    return state


def _verified_portfolio_daily_pnl(cur, *, state, account_cfg, account_balances, investment_positions):
    """Aggregate only same-period, independently verified account returns."""
    if state.get("base_currency") != "CNY" or state.get("net_liquidation_value") is None:
        return None
    members = [row for row in [*account_balances, *investment_positions] if row.get("include_in_nav", True)]
    account_values = state["raw_json"]["account_values_cny"]
    accounts = {str(row.get("account_code") or "") for row in members}
    if not accounts or "" in accounts or set(account_values) != accounts:
        return None
    try:
        if abs(sum((Decimal(str(value)) for value in account_values.values()), Decimal(0))
               - Decimal(str(state["net_liquidation_value"]))) > Decimal("0.01"):
            return None
        cur.execute("""SELECT to_regclass('public.account_snapshot_version') AS versions,
            to_regclass('public.source_batches') AS batches,
            to_regclass('public.source_items') AS items,
            to_regclass('public.source_files') AS files""")
        available = cur.fetchone()
        if not available or not all(available.get(key) for key in ('versions','batches','items','files')):
            return None
        end = Date.fromisoformat(str(state["valuation_date"]))
        if any(_date_key(row.get("as_of")) != end.isoformat() for row in members):
            return None
        from domain.accounts.flow_coverage import latest_verified_period
        from domain.accounts.official_flow_coverage import IBKRSourceConflict, latest_verified_ibkr_period
        proofs: list[dict[str, Any]] = []
        total = Decimal(0)
        for code in sorted(accounts):
            ratio = _owner_ratio(account_cfg, code)
            personal = Decimal(str(account_values[code]))
            if not ratio.is_finite() or ratio <= 0 or not personal.is_finite():
                return None
            gross = personal / ratio
            try:
                proof = latest_verified_ibkr_period(cur, account_code=code, end=end, ending_gross_cny=gross,
                                                    raise_on_conflict=True)
            except IBKRSourceConflict:
                return None
            if not proof:
                proof = latest_verified_period(cur, account_code=code, end=end, ending_gross_cny=gross)
            if not proof or abs(proof["closing_gross_cny"] - gross) > Decimal("0.01"):
                return None
            if proofs and proof["start"] != proofs[0]["proof"]["start"]:
                return None
            pnl = (gross - proof["opening_gross_cny"] - proof["external_flow_gross_cny"]) * ratio
            if not pnl.is_finite():
                return None
            total += pnl
            proofs.append({"account_code":code, "owner_ratio":ratio, "daily_pnl_cny":pnl, "proof":proof})
        return total, proofs
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None


def _build_current_state(
    cur,
    *,
    base_currency: str,
    cycle: str,
    status: str,
) -> dict[str, Any]:
    account_cfg = _load_accounts_config()
    account_balances = load_account_balances(cur, base_currency, account_cfg)
    investment_positions = load_investment_positions(
        cur,
        base_currency,
        account_cfg,
        account_balances=account_balances,
    )
    state = build_portfolio_valuation_state_from_parts(
        account_balances=account_balances,
        investment_positions=investment_positions,
        base_currency=base_currency,
        cycle=cycle,
        status=status,
    )
    verified = _verified_portfolio_daily_pnl(cur, state=state, account_cfg=account_cfg,
        account_balances=account_balances, investment_positions=investment_positions)
    if verified:
        state["daily_pnl_cny"], state["raw_json"]["daily_pnl_evidence"] = verified
    else:
        state["raw_json"]["daily_pnl_evidence"] = None
    state["source_hash"] = _state_hash(state)
    return state


def latest_published_portfolio_valuation_state(
    cur,
    *,
    base_currency: str = "CNY",
) -> dict[str, Any] | None:
    """Return the latest published valuation state row if the table is ready."""
    if not _table_exists(cur, STATE_TABLE):
        return None
    cur.execute(
        f"""
        SELECT {STATE_COLUMNS}
        FROM {STATE_TABLE}
        WHERE result_type = 'portfolio_published' AND base_currency = %s
          AND status = 'published'
        ORDER BY valuation_date DESC, published_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """,
        (str(base_currency or "CNY").upper(),),
    )
    row = cur.fetchone()
    if row:
        row = dict(row)
        # Older published records used a hard-coded zero. It is not evidence.
        if not (row.get("raw_json") or {}).get("daily_pnl_evidence"):
            row["daily_pnl_cny"] = None
    return row


def published_daily_close_compatible(row: dict[str, Any] | None, *,
                                     account_balances: list[dict[str, Any]] | None = None) -> bool:
    """A legacy NAV projection cannot overwrite an opt-in confirmed close.

    This gate applies to NAV consumers only. Holding-return projection semantics
    remain independent and must not be disabled as a side effect.
    """
    from domain.brokers.ibkr import ibkr_daily_close_read as daily

    if not daily.configured():
        return True
    evidence = daily.load_evidence()
    if not row or evidence.get("status") != "ok":
        return False
    raw = row.get("raw_json")
    if not isinstance(raw, dict):
        return False
    proof = raw.get("ibkr_daily_close")
    source_matches = bool(isinstance(proof, dict) and proof.get("status") == "ok"
        and proof.get("nav_basis") == "official_base_nav_at_internal_historical_fx"
        and proof.get("source_sha256") == evidence["sha256"]
        and proof.get("source_account_id") == evidence["account_id"]
        and proof.get("as_of") == evidence["as_of"].isoformat())
    if not source_matches:
        return False
    assert isinstance(proof, dict)
    current = [balance for balance in (account_balances or [])
               if balance.get("account_code") == "ibkr" and balance.get("include_in_nav", True)]
    if len(current) != 1:
        return False
    actual = current[0].get("daily_close") or {}
    if (actual.get("status") != "ok" or str(current[0].get("as_of"))[:10] != actual.get("as_of")):
        return False
    for field in ("source_sha256", "source_account_id", "as_of", "nav_basis", "valuation_currency", "fx_evidence"):
        if field not in proof or field not in actual or proof[field] != actual[field]:
            return False
    # XML identity alone does not bind mutable internal FX or ownership settings.
    for field in ("owner_ratio", "fx_rate", "assets_cny", "liabilities_cny", "net_worth_cny"):
        expected = current[0].get("amount_cny") if field == "net_worth_cny" else actual.get(field)
        left, right = _exact_amount(proof.get(field)), _exact_amount(expected)
        if left is None or right is None or left != right:
            return False
    return True


def _exact_amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def publish_current_portfolio_valuation_state(
    cur,
    *,
    base_currency: str = "CNY",
    cycle: str = "manual_publish",
) -> dict[str, Any]:
    """Compute and upsert the current valuation state as a published row."""
    if not _table_exists(cur, STATE_TABLE):
        raise RuntimeError(f"{STATE_TABLE} is missing; apply database schema first")

    state = _build_current_state(
        cur,
        base_currency=str(base_currency or "CNY").upper(),
        cycle=cycle,
        status="published",
    )
    if state['net_liquidation_value'] is None:
        raise ValueError('portfolio valuation cannot be published while account snapshot scope is incomplete')
    cur.execute(
        f"""
        INSERT INTO {STATE_TABLE} (
            result_type, valuation_date, cycle, base_currency, status, data_completeness,
            net_liquidation_value, assets_cny, liabilities_cny,
            gross_exposure_cny, investment_value_cny, cash_cny,
            daily_pnl_cny, total_pnl_cny, source_run_id, source_hash,
            raw_json, published_at, updated_at
        )
        VALUES (
            'portfolio_published', %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, now(), now()
        )
        ON CONFLICT (valuation_date, cycle, base_currency) WHERE result_type='portfolio_published'
        DO UPDATE SET
            status = EXCLUDED.status,
            data_completeness = EXCLUDED.data_completeness,
            net_liquidation_value = EXCLUDED.net_liquidation_value,
            assets_cny = EXCLUDED.assets_cny,
            liabilities_cny = EXCLUDED.liabilities_cny,
            gross_exposure_cny = EXCLUDED.gross_exposure_cny,
            investment_value_cny = EXCLUDED.investment_value_cny,
            cash_cny = EXCLUDED.cash_cny,
            daily_pnl_cny = EXCLUDED.daily_pnl_cny,
            total_pnl_cny = EXCLUDED.total_pnl_cny,
            source_run_id = EXCLUDED.source_run_id,
            source_hash = EXCLUDED.source_hash,
            raw_json = EXCLUDED.raw_json,
            published_at = EXCLUDED.published_at,
            updated_at = now()
        RETURNING {STATE_COLUMNS}
        """,
        (
            state["valuation_date"],
            state["cycle"],
            state["base_currency"],
            state["status"],
            state["data_completeness"],
            state["net_liquidation_value"],
            state["assets_cny"],
            state["liabilities_cny"],
            state["gross_exposure_cny"],
            state["investment_value_cny"],
            state["cash_cny"],
            state["daily_pnl_cny"],
            state["total_pnl_cny"],
            state["source_run_id"],
            state["source_hash"],
            psycopg2.extras.Json(_norm(state["raw_json"])),
        ),
    )
    return cur.fetchone()


def build_portfolio_valuation_state_payload(
    *,
    base_currency: str = "CNY",
    cycle: str = "manual_publish",
    refresh: bool = False,
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Return the latest fresh published valuation state, or a computed read-only fallback."""
    base = str(base_currency or "CNY").upper()
    with conn_factory() as conn:
        if hasattr(conn, "set_session"):
            conn.set_session(isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            table_ready = _table_exists(cur, STATE_TABLE)
            row: dict[str, Any] | None
            if refresh:
                row = publish_current_portfolio_valuation_state(cur, base_currency=base, cycle=cycle)
                return {"table_ready": table_ready, **(_normalize_db_row(row, source="published") or {})}

            state = _build_current_state(cur, base_currency=base, cycle=cycle, status="draft")
            row = latest_published_portfolio_valuation_state(cur, base_currency=base)
            if row:
                state_date = _date_key(state.get("valuation_date"))
                published_date = published_valuation_date_key(row)
                published_hash = row.get("source_hash")
                current_hash = state.get("source_hash")
                if (
                    published_date
                    and state_date
                    and published_date >= state_date
                    and published_hash == current_hash
                ):
                    return {"table_ready": table_ready, **(_normalize_db_row(row, source="published") or {})}
                stale_meta = published_valuation_stale_meta(
                    row,
                    target_as_of=state.get("valuation_date"),
                    reason="stale_or_changed_valuation_state",
                )
                return {
                    "table_ready": table_ready,
                    "source": "computed_current",
                    "published_state": stale_meta,
                    **_norm(state),
                    "as_of": state["valuation_date"],
                }

            return {"table_ready": table_ready, "source": "computed_unpublished", **_norm(state), "as_of": state["valuation_date"]}


def publish_portfolio_valuation_state_payload(
    *,
    base_currency: str = "CNY",
    cycle: str = "manual_publish",
    conn_factory: Callable[[], Any] = get_conn,
) -> dict[str, Any]:
    """Publish and return the current canonical valuation state."""
    base = str(base_currency or "CNY").upper()
    with conn_factory() as conn:
        if hasattr(conn, "set_session"):
            conn.set_session(isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = publish_current_portfolio_valuation_state(cur, base_currency=base, cycle=cycle)
            return {"table_ready": True, **(_normalize_db_row(row, source="published") or {})}
