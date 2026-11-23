"""NAV and valuation read-service boundary.

Batch 2c starts with this module as a staging area for moving NAV helpers out of
``main.py``.  The first rule is compatibility: callers must keep the old API
response shape until the frontend has explicitly migrated.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date as Date, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any

from domain.accounts.balance_reference import historical_account_balance, load_bound_balance_references
from domain.accounts.credit_card_debt import uses_table_net_reference
from domain.accounts.dated_valuation import AccountValuation, posted_card_valuations, statement_closings, statement_valuations
from domain.accounts.position_valuation import fund_account_observations, uses_fund_account_valuation
from domain.accounts.position_reference import supports_dated_position_reference
from domain.accounts.balance_snapshot import SOURCE as VERIFIED_BANK_BALANCE_SOURCE
from domain.investments.cash_fx_reference import preceding_weekend_dates, supports_cash_reference, weekend_cash_references
from domain.investments.nav_period_comparison import (
    DisplayContributions, apply_terminal_breakdown, comparison_scope, period_comparisons, position_member,
    terminal_comparison_basis,
)
from domain.investments.portfolio_state import _owner_ratio
from domain.market.month_end_marks import is_month_end, month_end_fx
from domain.market.valuation_fx import fx_rate_at, load_fx_timelines
from domain.market.quote_valuation import GOLD_CLOSE_SOURCE, gold_close_join, gold_market_value

ANCHOR_NAV_DATE = Date(2026, 5, 31)

class ValuationEvidenceSeries(dict):
    """Values plus dates where an input is missing; consumers must honor the latter."""
    def __init__(self, values=(), *, unknown_dates=(), estimated_dates=(), missing_price_dates=(), missing_fx_dates=None, cash_scope_unverified=False):
        super().__init__(values)
        self.unknown_dates = set(unknown_dates)
        self.estimated_dates = set(estimated_dates)
        self.missing_price_dates = set(missing_price_dates)
        self.missing_fx_dates = None if missing_fx_dates is None else set(missing_fx_dates)
        self.cash_scope_unverified = cash_scope_unverified
        self.unknown_accounts_by_date: dict[Date, list[str]] = {}
        self.available_accounts_by_date: dict[Date, list[str]] = {}
        self.estimated_accounts_by_date: dict[Date, list[str]] = {}
        self.account_values_by_date: dict[Date, dict[str, Decimal]] = {}
        self.preview_by_date: dict[Date, Decimal] = {}
        self.preview_performance_by_date: dict[Date, dict[str, Decimal]] = {}
        self.preview_source_through: Date | None = None


def series_dates(start_date: Date, end_date: Date, interval: str = "weekly") -> list[Date]:
    """Build a stable date grid and always include the terminal anchor date."""
    step_days = 7 if interval == "weekly" else 1
    dates: list[Date] = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=step_days)
    if not dates or dates[-1] != end_date:
        dates.append(end_date)
    return dates


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value).replace(",", "").strip())


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def account_anchor_amount(account: dict[str, Any]) -> tuple[Decimal, str] | None:
    """Return the native terminal balance selected by the account reconciliation center."""
    balance = account.get("primary_balance") or {}
    if balance.get("amount") in (None, ""):
        return None
    currency = str(balance.get("currency") or account.get("base_currency") or "CNY").upper()
    return _dec(balance.get("amount")), currency


def account_anchor_date(account: dict[str, Any], terminal_date: Date) -> Date:
    balance = account.get("primary_balance") or {}
    raw = balance.get("as_of") or account.get("as_of") or terminal_date
    if isinstance(raw, Date):
        return raw
    try:
        return Date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return terminal_date


def account_anchor_value_cny(
    account: dict[str, Any] | None,
    *,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
    as_of: Date,
    base_currency: str,
) -> Decimal | None:
    """Return an account-center anchor converted to the requested base currency."""
    if not account:
        return None
    anchor = account_anchor_amount(account)
    if anchor is None:
        return None
    amount, currency = anchor
    rate = fx_rate_at(fx_timelines, currency, as_of, base_currency)
    return amount * rate if rate is not None else None


def scale_series_to_terminal_anchor(
    series: dict[Date, Decimal],
    *,
    terminal_date: Date,
    target_value: Decimal | None,
) -> ValuationEvidenceSeries:
    """Scale a reconstructed curve so the terminal value matches a confirmed account anchor."""
    adjusted = ValuationEvidenceSeries(series, unknown_dates=getattr(series, "unknown_dates", ()),
                                       estimated_dates=getattr(series, "estimated_dates", ()),
                                       missing_price_dates=getattr(series, "missing_price_dates", ()),
                                       cash_scope_unverified=getattr(series, "cash_scope_unverified", False))
    if target_value is None:
        return adjusted
    terminal_value = series.get(terminal_date, Decimal("0"))
    if terminal_date in adjusted.unknown_dates:
        return adjusted
    if abs(terminal_value) <= Decimal("0.0001"):
        adjusted[terminal_date] = target_value
        return adjusted
    scale = target_value / terminal_value
    return ValuationEvidenceSeries(((date, value * scale) for date, value in adjusted.items()),
                                   unknown_dates=adjusted.unknown_dates, estimated_dates=adjusted.estimated_dates,
                                   missing_price_dates=adjusted.missing_price_dates,
                                   cash_scope_unverified=adjusted.cash_scope_unverified)


def add_nav_amount(bucket: dict[str, Decimal], amount_cny: Decimal) -> None:
    """Add one signed CNY account value to asset/liability/net NAV buckets."""
    if amount_cny < 0:
        bucket["liabilities"] += abs(amount_cny)
    else:
        bucket["assets"] += amount_cny
    bucket["net_worth"] += amount_cny


def daily_close_nav_bucket(
    close: dict[str, Any], *, rate: Decimal | None, owner_ratio: Decimal,
) -> dict[str, Decimal] | None:
    """Convert only a proven gross long/short close, never split net NAV by sign."""
    if close.get("status") != "ok" or rate is None:
        return None
    try:
        proof = close["component_proof"]
        gross = proof["subcomponent_checks"]["total_long_short"]
        nav, long, short = (Decimal(str(value)) for value in (close["nav_base"], gross["long"], gross["short"]))
        tolerance = Decimal(str(proof["tolerance"]))
        if not all(v.is_finite() for v in (nav, long, short, rate, owner_ratio, tolerance)):
            return None
        with localcontext() as context:
            context.prec = 160
            if (rate <= 0 or owner_ratio < 0 or tolerance < 0 or long < 0 or short > 0
                    or abs(long + short - nav) > tolerance):
                return None
            factor = rate * owner_ratio
            return {"assets": long * factor, "liabilities": -short * factor, "net_worth": nav * factor}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def daily_close_account_identity(cur, *, source_account_id: str, as_of: Date) -> dict[str, Any]:
    """Verify a separately reviewed account mapping without claiming authentication."""
    from uuid import UUID, uuid5
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    from domain.imports.ibkr_holding_source import _read_source_report, _extract

    def unknown(reason: str) -> dict[str, Any]:
        return {"status": "unconfirmed", "reasons": [reason], "authentication": "unknown"}

    cur.execute("SELECT to_regclass('public.source_batches') IS NOT NULL AS batches, "
                "to_regclass('public.source_items') IS NOT NULL AS items, "
                "to_regclass('public.source_files') IS NOT NULL AS files")
    present = cur.fetchone()
    if not present or not all(present.get(key) for key in ('batches', 'items', 'files')):
        return unknown('missing_native_account_identity')
    cur.execute("""SELECT b.*, i.source_item_id AS scope_id, i.file_id AS scope_file_id,
        i.origin_file_id AS scope_origin_file_id, i.raw_fields AS scope_raw,
        i.parsed_fields AS scope_parsed, i.review_status AS scope_status
        FROM source_batches b JOIN source_items i ON i.batch_id=b.batch_id AND i.source_uid='scope'
        WHERE b.account_code='ibkr' AND b.business_type='holding' AND b.institution='ibkr'
          AND b.workflow='ibkr_holding_review' AND b.status='published' AND b.date_from=%s AND b.date_to=%s
          AND i.parsed_fields ? 'account_binding_review'""", (as_of, as_of))
    candidates = [dict(row) for row in cur.fetchall()]
    if not candidates:
        return unknown('missing_native_account_identity')
    accounts = set()
    for row in candidates:
        try:
            raw, parsed = row['scope_raw'], row['scope_parsed']
            decision = parsed['account_binding_review']
            account = raw['source_account_id']
            if (row['scope_status'] != 'confirmed' or not parsed.get('scope_review') or
                not isinstance(account, str) or not account or
                str(row['scope_id']) != str(uuid5(UUID(str(row['batch_id'])), 'scope')) or
                decision != {'account_code': 'ibkr', 'source_account_id': account,
                    'business_date': as_of.isoformat(), 'source_sha256': raw['source_sha256'],
                    'note': decision.get('note'), 'authentication': 'unknown'} or
                not isinstance(decision.get('note'), str) or not decision['note'].strip() or
                raw.get('business_date') != as_of.isoformat() or
                not _reviewed_snapshot(cur, row, str(row['batch_id']), locking=False, snapshot_cache={})):
                return unknown('account_binding_evidence_mismatch')
            scope = {'file_id': row['scope_file_id'], 'origin_file_id': row['scope_origin_file_id'], 'raw_fields': raw}
            content = _read_source_report(cur, row, scope, locking=False)
            _, statement, _ = _extract(content, source_account_id=account, business_date=as_of)
            if statement != raw.get('statement_attributes'):
                return unknown('account_binding_evidence_mismatch')
            accounts.add(account)
        except (KeyError, TypeError, ValueError):
            return unknown('account_binding_evidence_mismatch')
    if len(accounts) != 1:
        return unknown('ambiguous_native_account_identity')
    if source_account_id not in accounts:
        return unknown('native_account_identity_mismatch')
    return {'status': 'ok', 'basis': 'explicit_reviewed_source_account_binding',
            'as_of': as_of.isoformat(), 'authentication': 'unknown'}


def daily_close_value_metadata(
    close: dict[str, Any], *, rate: Decimal, owner_ratio: Decimal, legacy_value: Decimal,
    fx_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep the native-FX bridge separate from the accrual/other NAV adjustment."""
    with localcontext() as context:
        context.prec = 160
        factor = rate * owner_ratio
        cash_positions = (close["cash_base"] + close["position_value_base"]) * factor
        components = (close["interest_accrual_base"] + close["dividend_accrual_base"]
                      + close["other_nav_components_base"]) * factor
        corrected = close["nav_base"] * factor
        result: dict[str, Any] = {"status": "ok", "as_of": close["as_of"], "source_sha256": close["source_sha256"],
                "source_account_id": close["account_id"],
                "requested_as_of": close["as_of"], "carried": False,
                "nav_basis": "official_base_nav_at_internal_historical_fx",
                "source_base_currency": close["base_currency"], "fx_rate": str(rate),
                "owned_nav_base": str(close["nav_base"] * owner_ratio),
                "owner_ratio": str(owner_ratio), "legacy_native_value_cny": str(legacy_value),
                "cash_and_positions_at_base_fx_cny": str(cash_positions),
                "native_fx_vs_base_fx_bridge_cny": str(legacy_value - cash_positions),
                "bridge_basis": "legacy_native_valuation_minus_official_cash_positions_includes_any_universe_difference",
                "accrual_and_other_components_cny": str(components),
                "nav_adjustment_cny": str(corrected - legacy_value)}
        if fx_evidence is not None:
            result["fx_evidence"] = {k: str(v) if isinstance(v, Decimal) else v for k, v in fx_evidence.items()}
        return result


def current_daily_close_accounts(
    account_balances: list[dict[str, Any]], investment_positions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result = {}
    for row in account_balances:
        metadata = row.get("daily_close") or {}
        code = str(row.get("account_code") or "")
        day = metadata.get("as_of")
        if (code != "ibkr" or metadata.get("status") != "ok" or not day
                or str(row.get("as_of"))[:10] != day or not row.get("include_in_nav", True)):
            continue
        if any(str(position.get("as_of"))[:10] != day for position in investment_positions
               if str(position.get("account_code") or "") == code):
            continue
        result[code] = row
    return result


def signed_account_native_value(account: dict[str, Any], value: Decimal) -> Decimal:
    """Normalize statement balances to NAV sign conventions."""
    if str(account.get("account_type") or "") == "liability":
        return -abs(value)
    return value


def build_weekly_nav_rows(
    *,
    dates: list[Date],
    bank_series: dict[Date, dict[str, Any]],
    snapshot_series: dict[Date, Decimal],
    a_share_series: dict[Date, Decimal],
    ibkr_values: dict[Date, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine account, investment, and broker NAV components into dated rows."""
    rows: list[dict[str, Any]] = []
    for sample_date in dates:
        bucket = {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")}
        bank_bucket = bank_series.get(sample_date) or {}
        bucket["assets"] += bank_bucket.get("assets", Decimal("0"))
        bucket["liabilities"] += bank_bucket.get("liabilities", Decimal("0"))
        bucket["net_worth"] += bank_bucket.get("net_worth", Decimal("0"))
        for amount in (
            snapshot_series.get(sample_date, Decimal("0")),
            a_share_series.get(sample_date, Decimal("0")),
        ):
            add_nav_amount(bucket, amount)
        ibkr_bucket = ibkr_values.get(sample_date) or {}
        missing_accounts = list(bank_bucket.get("unavailable_accounts") or [])
        missing_accounts.extend(getattr(snapshot_series, "unknown_accounts_by_date", {}).get(sample_date, ()))
        if sample_date in getattr(a_share_series, "unknown_dates", set()):
            missing_accounts.append("tonghuashun")
        if ibkr_bucket.get("history_evidence") == "unavailable":
            missing_accounts.append("ibkr")
        available = set(bank_bucket.get("available_accounts") or ())
        contributions = dict(bank_bucket.get('account_values') or {})
        contributions.update(getattr(snapshot_series, 'account_values_by_date', {}).get(sample_date, {}))
        available_net = bank_bucket.get("net_worth", Decimal("0"))
        snapshot_accounts = getattr(snapshot_series, "available_accounts_by_date", {}).get(sample_date, ())
        if snapshot_accounts:
            available.update(snapshot_accounts)
            available_net += snapshot_series[sample_date]
        if sample_date in a_share_series and "tonghuashun" not in missing_accounts:
            available.add("tonghuashun")
            available_net += a_share_series[sample_date]
            contributions['tonghuashun'] = a_share_series[sample_date]
        preview = getattr(a_share_series, "preview_by_date", {}).get(sample_date)
        if preview is not None and "tonghuashun" in missing_accounts:
            available.add("tonghuashun")
            available_net += preview
            contributions['tonghuashun'] = preview
        if ibkr_bucket.get("net_worth") is not None and "ibkr" not in missing_accounts:
            available.add("ibkr")
            available_net += ibkr_bucket["net_worth"]
            contributions['ibkr'] = ibkr_bucket['net_worth']
        partial = ({"available_net_worth": available_net, "available_accounts": sorted(available)}
                   if available else {})
        partial['_account_contributions'] = contributions
        if bank_bucket.get('latest_known_components'):
            partial['latest_known_components'] = dict(bank_bucket['latest_known_components'])
        basis = dict(bank_bucket.get('_comparison_basis', {}))
        basis.update({code:'complete_positions' for code in snapshot_accounts})
        if 'tonghuashun' in available:
            basis['tonghuashun'] = ('broker_ledger' if 'tonghuashun' in missing_accounts else 'complete_positions')
        if 'ibkr' in available:
            proof = ibkr_bucket.get('daily_close') or {}
            basis['ibkr'] = proof.get('nav_basis') or ibkr_bucket.get('history_evidence') or 'broker_net'
        partial['_comparison_basis'] = basis
        partial['_dated_cash_accounts'] = set(bank_bucket.get('_dated_cash_accounts', ()))
        if preview is not None and "tonghuashun" in missing_accounts:
            partial["tonghuashun_source_through"] = getattr(a_share_series, "preview_source_through", None)
        carried_ibkr = ("ibkr" in available and ibkr_bucket.get("daily_close", {}).get("status") == "last_valid")
        estimated_snapshots = getattr(snapshot_series, "estimated_accounts_by_date", {}).get(sample_date, ())
        if missing_accounts:
            estimated_available = available.intersection(bank_bucket.get("estimated_accounts") or ())
            estimated_available.update(estimated_snapshots)
            if carried_ibkr:
                estimated_available.add("ibkr")
            if preview is not None and "tonghuashun" in missing_accounts:
                estimated_available.add("tonghuashun")
            rows.append({"date": sample_date, **partial, "assets": None, "liabilities": None,
                         "net_worth": None, "unavailable_accounts": sorted(set(missing_accounts)),
                         **({"valuation_quality": "estimated", "estimated_accounts": sorted(estimated_available)}
                            if estimated_available else {}),
                         **({"source_evidence_unavailable_accounts": sorted(set(bank_bucket['source_evidence_unavailable_accounts']))}
                            if bank_bucket.get('source_evidence_unavailable_accounts') else {}),
                         **({"ibkr_history_evidence": "unavailable"} if "ibkr" in missing_accounts else {}),
                         **({"ibkr_daily_close": ibkr_bucket["daily_close"]} if "daily_close" in ibkr_bucket else {})})
            continue
        bucket["assets"] += ibkr_bucket.get("assets", Decimal("0"))
        bucket["liabilities"] += ibkr_bucket.get("liabilities", Decimal("0"))
        bucket["net_worth"] += ibkr_bucket.get("net_worth", Decimal("0"))
        row: dict[str, Any] = {"date": sample_date, **bucket, **partial}
        estimated = set(bank_bucket.get('estimated_accounts') or ())
        estimated.update(estimated_snapshots)
        if carried_ibkr:
            estimated.add('ibkr')
        if sample_date in getattr(a_share_series, 'estimated_dates', set()):
            estimated.add('tonghuashun')
        if estimated:
            row.update(valuation_quality='estimated', estimated_accounts=sorted(estimated))
        if "daily_close" in ibkr_bucket:
            row["ibkr_daily_close"] = ibkr_bucket["daily_close"]
        rows.append(row)
    return rows


def current_nav_bucket_from_parts(
    *,
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the current NAV bucket from the canonical PortfolioState parts."""
    excluded_accounts = {str(row.get("account_code")) for row in account_balances
                         if row.get("account_code") and not row.get("include_in_nav", True)}
    investment_positions = [row for row in investment_positions
                            if row.get("include_in_nav", True)
                            and str(row.get("account_code") or "") not in excluded_accounts]
    represented_accounts = {
        str(row.get("account_code") or "")
        for row in investment_positions
        if row.get("account_code")
    }
    bucket: dict[str, Any] = {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")}
    official = current_daily_close_accounts(account_balances, investment_positions)
    incomplete = sorted({str(row.get("account_code")) for row in [*account_balances, *investment_positions]
                         if row.get("account_code") and row.get("include_in_nav", True)
                         and (row.get("snapshot_scope_status") in {"partial", "unknown"} or row.get("fx_status") == "unavailable" or row.get("valuation_status") == "unavailable")
                         and str(row.get("account_code")) not in official})
    if incomplete:
        return {"assets": None, "liabilities": None, "net_worth": None,
                "scope_status": "incomplete", "unavailable_accounts": incomplete}
    for row in investment_positions:
        if str(row.get("account_code") or "") in official:
            continue
        add_nav_amount(bucket, _dec(row.get("market_value_cny")))
    for row in account_balances:
        account_code = str(row.get("account_code") or "")
        if account_code in official:
            proof = official.pop(account_code)["daily_close"]
            bucket["assets"] += _dec(proof["assets_cny"])
            bucket["liabilities"] += _dec(proof["liabilities_cny"])
            bucket["net_worth"] += _dec(row["amount_cny"])
            bucket["ibkr_daily_close"] = {**proof, "net_worth_cny": str(_dec(row["amount_cny"]))}
            continue
        if not row.get("include_in_nav", True) or account_code in represented_accounts:
            continue
        add_nav_amount(bucket, _dec(row.get("amount_cny")))
    return bucket


def current_nav_account_values_from_parts(
    *,
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
) -> dict[str, Decimal]:
    """Return canonical current net value by account using PortfolioState parts."""
    excluded_accounts = {str(row.get("account_code")) for row in account_balances
                         if row.get("account_code") and not row.get("include_in_nav", True)}
    investment_positions = [row for row in investment_positions
                            if row.get("include_in_nav", True)
                            and str(row.get("account_code") or "") not in excluded_accounts]
    represented_accounts = {
        str(row.get("account_code") or "")
        for row in investment_positions
        if row.get("account_code")
    }
    values = DisplayContributions()
    official = current_daily_close_accounts(account_balances, investment_positions)
    incomplete = {str(row.get("account_code")) for row in [*account_balances, *investment_positions]
                  if row.get("account_code") and row.get("include_in_nav", True)
                  and (row.get("snapshot_scope_status") in {"partial", "unknown"} or row.get("fx_status") == "unavailable" or row.get("valuation_status") == "unavailable")
                  and str(row.get("account_code")) not in official}
    for row in investment_positions:
        account_code = str(row.get("account_code") or "")
        if not account_code or account_code in incomplete:
            continue
        if account_code in official:
            continue
        values[account_code] = values.get(account_code, Decimal("0")) + _dec(row.get("market_value_cny"))
        if row.get('snapshot_scope_status') != 'complete' or row.get('market_value_cny') is None:
            values.basis[account_code] = 'terminal_positions_unverified'
        else:
            values.basis.setdefault(account_code, 'complete_positions')
    for row in account_balances:
        account_code = str(row.get("account_code") or "")
        if account_code in incomplete:
            continue
        if account_code in official:
            source = official.pop(account_code)
            values[account_code] = _dec(source["amount_cny"])
            values.basis[account_code] = source['daily_close'].get('nav_basis') or 'broker_net'
            continue
        if not account_code or not row.get("include_in_nav", True) or account_code in represented_accounts:
            continue
        values[account_code] = values.get(account_code, Decimal("0")) + _dec(row.get("amount_cny"))
        values.basis[account_code] = 'complete_positions' if row.get('snapshot_scope_status') == 'complete' else 'cash_balance'
    return values


def latest_nav_display_values(
    *,
    account_balances: list[dict[str, Any]],
    investment_positions: list[dict[str, Any]],
    terminal_date: Date,
    confirmed_values: dict[str, Decimal],
    account_config: dict[str, Any] | None = None,
    cur=None,
) -> dict[str, tuple[Decimal, Date, Date]]:
    """Select dated components that can supplement an incomplete NAV terminal point."""
    balances = {row["account_code"]: row for row in account_balances if row.get("account_code")}
    by_account = defaultdict(list)
    for row in investment_positions:
        code = row.get("account_code")
        if code and row.get("include_in_nav", True) and balances.get(code, {}).get("include_in_nav", True):
            by_account[code].append(row)
    values = DisplayContributions()
    configs = (account_config or {}).get("accounts", {})
    broker_close_accounts = {code for code, row in balances.items()
                             if row.get('daily_close') is not None or configs.get(code, {}).get('portfolio_value_evidence') == 'official_broker_close'}
    def record(code, rows, value):
        dates = [_row_date(row.get("market_price_as_of") or row.get("as_of")) for row in rows]
        if all(day is not None and day <= terminal_date for day in dates) and value.is_finite():
            values[code] = (value, min(dates), max(dates))
            close = rows[0].get('daily_close') or {}
            if close.get('status') in {'ok','last_valid'} and close.get('nav_basis'):
                values.basis[code] = (['prior_official_nav', close['nav_basis']] if max(dates) < terminal_date
                                      else close['nav_basis'])
            elif any(r.get('asset_id') or r.get('symbol') for r in rows):
                values.basis[code] = ['position_members', sorted(position_member(r) for r in rows)]
            else:
                values.basis[code] = 'cash_balance'

    for code, value in confirmed_values.items():
        positions = by_account.get(code)
        balance = balances.get(code, {})
        if code in broker_close_accounts:
            # A broker net asset value includes cash and financing. A
            # portfolio of individually priced securities cannot replace it.
            if code in current_daily_close_accounts([balance], positions or []):
                record(code, [balance], value)
            continue
        rows = positions or [balance]
        amount_key = "market_value_cny" if positions else "amount_cny"
        if any(row.get('currency') not in (None, 'CNY') and row.get(amount_key) != 0
               and _row_date(row.get('fx_rate_as_of')) != terminal_date for row in rows):
            continue
        if all(row.get(amount_key) is not None for row in rows):
            record(code, rows, value)

    config = account_config or {}
    bindings = load_bound_balance_references(cur, configs) if cur is not None else {}
    for code, rows in by_account.items():
        if code in values or code in broker_close_accounts:
            continue
        mode = (configs.get(code) or {}).get("latest_position_evidence")
        supported = all(
            supports_dated_position_reference(row, mode) or (
                row.get("valuation_price_source") == GOLD_CLOSE_SOURCE
                and row.get("snapshot_scope_member") == "recorded"
            )
            for row in rows
        )
        if supported and all(row.get("currency") == "CNY" and row.get("market_value_cny") is not None for row in rows):
            record(code, rows, sum((_dec(row["market_value_cny"]) for row in rows), Decimal(0)))

    for code, balance in balances.items():
        if code in values or code in broker_close_accounts or not balance.get("include_in_nav", True):
            continue
        reference = historical_account_balance(configs.get(code, {}), terminal_date, _owner_ratio(config, code),
                                               account_code=code, bindings=bindings)
        # This path has no dated FX input; an original-currency reference must
        # never be treated as a CNY contribution. The dated projection handles it.
        if reference and reference['balance_currency'] == 'CNY':
            value = Decimal(reference["balance_amount"])
            day = reference["balance_as_of"]
            values[code] = (-value if balance.get("account_type") == "liability" else value, day, day)
            values.basis[code] = 'balance_reference'
    return values


def _add_known_display_values(row, values, *, basis=None):
    """Add missing contributions once to the partial display series."""
    if row.get("net_worth") is not None:
        return
    available = set(row.get("available_accounts", ()))
    additions = sorted(values.keys() - available - set(row.get("source_evidence_unavailable_accounts", ())))
    if not additions:
        return
    row["available_net_worth"] = row.get("available_net_worth", Decimal(0)) + sum(
        (values[code][0] for code in additions), Decimal(0),
    )
    row["available_accounts"] = sorted(available.union(additions))
    row.setdefault('_account_contributions', {}).update({code:values[code][0] for code in additions})
    sources = getattr(values, 'basis', {}) if basis is None else basis
    row.setdefault('_comparison_basis', {}).update({code:sources.get(code, 'display_estimate') for code in additions})
    row["estimated_accounts"] = sorted(set(row.get("estimated_accounts", ())).union(additions))
    row.setdefault("latest_known_components", {}).update({
        code: {"from": values[code][1].isoformat(), "through": values[code][2].isoformat()}
        for code in additions
    })
    row["valuation_quality"] = "estimated"


def _add_dated_account_observations(cur, rows, configs, account_codes, owner_ratio, rate_at, *, unbilled=None):
    """Use whole-account observations before considering incomplete holdings or stale references."""
    if not rows or not account_codes:
        return
    first, last = rows[0]['date'], rows[-1]['date']
    by_day = {row['date']: row for row in rows if row.get('net_worth') is None}
    # Replay intervening changes even for a weekly chart. Only requested samples are emitted.
    days = series_dates(first, last, 'daily')
    for code in sorted(account_codes):
        cfg = configs.get(code, {})
        currency = cfg.get('base_currency', 'CNY')
        points = {}
        if cfg.get('account_type') in ('credit_card', 'liability'):
            native = posted_card_valuations(cur, code, cfg, currency, last)
        elif uses_fund_account_valuation(cfg):
            points = fund_account_observations(cur, code, configs, days)
            native = {day: AccountValuation.from_measurement(day, currency, point, 'net_value')
                      for day, point in points.items() if point.get('net_value') is not None
                      and point.get('net_value_status') in ('confirmed', 'estimated')}
        else:
            continue
        for day in by_day.keys() & native.keys():
            row = by_day[day]
            if code not in row.get('unavailable_accounts', ()) or code in row.get('available_accounts', ()):
                continue
            observation = native[day]
            if observation.basis == 'posted_flow_net' and (unbilled or {}).get((day, code)):
                continue
            amount = observation.personal_value(owner_ratio(code), rate_at(currency, day))
            if amount is not None:
                source_day = observation.observed_on or day
                _add_known_display_values(row, {code: (amount, source_day, source_day)}, basis={code: observation.basis})
                details = row.get('latest_known_components', {}).get(code)
                prices = points.get(day, {}).get('price_dates')
                if details is not None and prices:
                    details['from'] = min(prices.values()).isoformat()
                    details['balance_as_of'] = day.isoformat()
                    details['price_dates'] = {symbol: d.isoformat() for symbol, d in prices.items()}


def _add_month_end_marks(row, observations, timelines, base_currency):
    day = row['date']
    for code, (observation, ratio) in observations.items():
        if code not in row.get('unavailable_accounts', ()) or code in row.get('available_accounts', ()):
            continue
        fixing = month_end_fx(timelines, observation.currency, day, base_currency)
        if not fixing:
            continue
        fx_day, rate = fixing
        amount = observation.personal_value(ratio, rate)
        if amount is None:
            continue
        source_day = observation.observed_on or observation.day
        _add_known_display_values(row, {code: (amount, min(source_day, fx_day), max(source_day, fx_day))},
                                  basis={code: observation.basis})
        details = row.get('latest_known_components', {}).get(code)
        if details is not None:
            details.update(balance_as_of=source_day.isoformat(), fx_as_of=fx_day.isoformat(),
                           currency=observation.currency, native_balance=str(observation.amount), fx_rate=str(rate),
                           fx_age_days=(day - fx_day).days)


def _add_weekend_cash_references(row, bank_series, rate_at):
    if row.get('net_worth') is not None:
        return
    references = weekend_cash_references(row['date'], bank_series, rate_at)
    if not references:
        return
    covered = set(row.get('available_accounts', ())) | set(row.get('source_evidence_unavailable_accounts', ()))
    values = DisplayContributions()
    for code in references.keys() & (set(row.get('unavailable_accounts', ())) - covered):
        info = references[code]
        values[code] = (info['amount'], info['balance_as_of'], info['fx_as_of'])
        values.basis[code] = ['historical_cash_fx', row['date'].isoformat()]
    _add_known_display_values(row, values)
    for code in values:
        row['latest_known_components'][code].update(
            {key:_norm(value) for key,value in references[code].items() if key != 'amount'},
            basis='historical_cash_fx',
        )


def _apply_account_totals(row, values, account_owner_ratio, rate_at):
    """Replace only individually attributable contributions, never guess a subtraction."""
    if not values:
        return
    scopes = [set(v['covered_account_codes']) for v in values]
    for index, value in enumerate(values):
        covered = scopes[index]
        if any(covered & other for j, other in enumerate(scopes) if j != index):
            continue
        available = set(row.get('available_accounts', ()))
        contributions = row.setdefault('_account_contributions', {})
        if (covered & set(row.get('source_evidence_unavailable_accounts', ()))
                or any(code in available and code not in contributions for code in covered)):
            continue
        raw_ratios = [account_owner_ratio(code) for code in covered]
        if any(ratio is None for ratio in raw_ratios):
            continue
        ratios = {Decimal(str(ratio)) for ratio in raw_ratios}
        rate = rate_at(value['currency'], row['date'])
        if len(ratios) != 1 or (rate is None and Decimal(value['net_asset']) != 0):
            continue
        total = AccountValuation(row['date'], value['currency'], Decimal(value['net_asset']), 'account_total')
        amount = total.personal_value(next(iter(ratios)), rate)
        if amount is None:
            continue
        prior = sum((contributions.get(code, Decimal(0)) for code in covered if code in available), Decimal(0))
        conflicting = covered <= available and abs(amount - prior) > Decimal('0.005')
        whole_confirmed = covered.isdisjoint(set(row.get('unavailable_accounts', ())) | set(row.get('estimated_accounts', ())))
        if conflicting and (value.get('confirmed_components') or whole_confirmed):
            row.setdefault('valuation_discrepancies', {})[value['account_code']] = {
                'covered_accounts': sorted(covered), 'contributions': str(prior),
                'reported_total': str(amount), 'difference': str(amount - prior)}
            row['available_net_worth'] = row.get('net_worth') if row.get('net_worth') is not None else row.get('available_net_worth')
            row.update(net_worth=None, assets=None, liabilities=None)
            row['unavailable_accounts'] = sorted(set(row.get('unavailable_accounts', ())) | covered)
            continue
        delta = amount - prior
        if row.get('net_worth') is not None and any(contributions.get(code, Decimal(0)) < 0 for code in covered):
            continue
        if row.get('available_net_worth') is not None:
            row['available_net_worth'] += delta
        elif row.get('net_worth') is None:
            row['available_net_worth'] = delta
        if row.get('net_worth') is not None:
            if row.get('assets') is not None:
                row['assets'] += delta
            row['net_worth'] += delta
        for code in covered:
            contributions.pop(code, None)
            row.setdefault('_comparison_basis', {}).pop(code, None)
            row.get('latest_known_components', {}).pop(code, None)
            row.get('_undated_terminal_accounts', set()).discard(code)
        row['_comparison_basis'][value['account_code']] = ['account_total', sorted(covered)]
        contributions[value['account_code']] = amount
        row['available_accounts'] = sorted((available - covered) | {value['account_code']})
        # A platform net amount alone cannot certify a gross breakdown.
        if delta or len(covered) > 1:
            row['assets'] = row['liabilities'] = None
        # A verified same-day account total proves the net value for its
        # explicitly declared scope even if individual holdings are partial.
        # It does not certify other accounts or permit carrying this value to
        # another day, nor does it establish a gross assets/liabilities split.
        if 'unavailable_accounts' in row:
            row['unavailable_accounts'] = sorted(set(row['unavailable_accounts']) - covered)
        estimated = set(row.get('estimated_accounts', ())) - covered
        if not (value.get('confirmed_empty') or value.get('observed_total')):
            estimated.add(value['account_code'])
        row['estimated_accounts'] = sorted(estimated)
        if estimated:
            row['valuation_quality'] = 'estimated'
        elif row.get('valuation_quality') == 'estimated':
            row.pop('valuation_quality')


def normalize_nav_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize internal NAV keys without changing the legacy API field names."""
    result: dict[str, Any] = {
        "date": _norm(row.get("date")),
        "asOfDate": _norm(row.get("date")),
        "assets": _norm(row.get("assets")),
        "liabilities": _norm(row.get("liabilities")),
        "netWorth": _norm(row.get("net_worth")),
    }
    if '_account_contributions' in row:
        result['account_contributions'] = {code: str(amount) for code, amount in row['_account_contributions'].items()}
    if "ibkr_daily_close" in row:
        result["ibkr_daily_close"] = row["ibkr_daily_close"]
    if "ibkr_history_evidence" in row:
        result["ibkr_history_evidence"] = row["ibkr_history_evidence"]
    if "unavailable_accounts" in row:
        result["unavailable_accounts"] = row["unavailable_accounts"]
    if "scope_status" in row:
        result["scope_status"] = row["scope_status"]
    if row.get("net_worth") is None and "available_net_worth" in row:
        result["availableNetWorth"] = _norm(row["available_net_worth"])
        result["available_accounts"] = row["available_accounts"]
        if row.get("tonghuashun_source_through") is not None:
            result["tonghuashun_source_through"] = _norm(row["tonghuashun_source_through"])
    if row.get("net_worth") is None and row.get("available_net_worth") is None:
        result["valuation_error"] = "market_value_unavailable"
    for field in ('source_evidence_unavailable_accounts', 'valuation_quality', 'estimated_accounts', 'latest_known_components',
                  'personal_loan_coverage', 'valuation_discrepancies'):
        if field in row:
            result[field] = row[field]
    return result


def format_weekly_nav_payload(
    *,
    start_date: Date,
    terminal_date: Date,
    base_currency: str,
    rows: list[dict[str, Any]],
    interval: str = "weekly",
    account_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the legacy /nav/weekly response shape for frontend compatibility."""
    rendered = []
    for row in rows:
        point = normalize_nav_row(row)
        point['comparison_scope'] = comparison_scope(row)
        rendered.append(point)
    return {
        "start": start_date.isoformat(),
        "end": terminal_date.isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": "dated_account_valuation",
        "anchor_date": terminal_date.isoformat(),
        "rows": rendered,
        "period_comparisons": period_comparisons(rendered, terminal_date, rows, account_labels),
    }


class StatementRowsByAccount(dict):
    """Verified cash movements and dated failures to verify the underlying facts."""
    def __init__(self):
        super().__init__()
        self.evidence_problems: dict[str, dict[Date | None, set[str]]] = {}

    def unknown(self, account_code: str, reason: str, day: Date | None = None) -> None:
        self.evidence_problems.setdefault(account_code, {}).setdefault(day, set()).add(reason)


def _statement_rows_by_account(cur, account_codes: list[str], start_date: Date, terminal_date: Date) -> StatementRowsByAccount:
    """Use only independently verifiable published cash facts for NAV backcasts."""
    result = StatementRowsByAccount()
    if not account_codes:
        return result
    cur.execute("""SELECT to_regclass('public.pams_cashflow_fact') IS NOT NULL AS cashflow_fact_exists,
        to_regclass('public.source_items') IS NOT NULL AS item_exists,
        to_regclass('public.source_batches') IS NOT NULL AS batch_exists,
        to_regclass('public.source_records') IS NOT NULL AS redesigned""")
    exists = cur.fetchone() or {}
    if exists.get('redesigned') and exists.get('cashflow_fact_exists') and exists.get('item_exists'):
        from domain.ledger.cashflow_read_v2 import load_cash_rows
        rows = load_cash_rows(cur, start=start_date, end=terminal_date, base_currency='CNY', account_codes=account_codes)
        for item in rows:
            code = item['account_code']
            if item['source_evidence_status'] != 'verified':
                result.unknown(code, item.get('source_issue') or 'source_evidence_unavailable',
                               _row_date(item.get('posting_date') or item.get('transaction_date')))
                continue
            item['statement_balance'] = None
            result.setdefault(code, []).append(item)
        return result
    if not (exists.get('cashflow_fact_exists') and exists.get('item_exists') and exists.get('batch_exists')):
        for code in account_codes:
            result.unknown(code, 'cash_source_schema_unavailable')
        return result
    cur.execute("""SELECT cashflow_id AS id,source_uid,account_code,event_date AS transaction_date,
            amount,currency,category,subcategory,summary,description,counterparty,
            source_table,source_item_id,review_status
        FROM pams_cashflow_fact WHERE account_code=ANY(%s::text[])
          AND event_date >= %s AND event_date <= %s
          AND COALESCE(LOWER(review_status),'') NOT IN ('ignored','cancelled','canceled','void','duplicate')
        ORDER BY account_code,event_date,cashflow_id""", (account_codes,start_date,terminal_date))
    rows = [dict(row) for row in cur.fetchall()]
    from domain.imports.source_fact_evidence import NativeFactVerifier
    verifier = NativeFactVerifier(cur)
    for item in rows:
        code = str(item['account_code'])
        proof = verifier.verify(table='pams_cashflow_fact', fact_id=int(item['id']))
        if proof.get('status') != 'verified':
            result.unknown(code, str(proof.get('reason') or 'source_evidence_unavailable'),
                           _row_date(item.get('transaction_date')))
            continue
        # A transaction row's incidental balance is not a separately approved
        # account balance; preserve it as unavailable for anchor purposes.
        item['statement_balance'] = None
        item['source_evidence_status'] = 'verified'
        result.setdefault(code, []).append(item)
    return result


def _row_date(value: Any) -> Date | None:
    if isinstance(value, Date):
        return value
    try:
        return Date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _statement_balance(row: dict[str, Any]) -> Decimal | None:
    value = row.get("statement_balance")
    if value in (None, ""):
        return None
    return _dec(value)


def _statement_balance_native_value(
    rows: list[dict[str, Any]],
    *,
    sample_date: Date,
    currency: str,
    liability: bool = False,
) -> tuple[Decimal, Date] | None:
    """Return account value from statement running balances when available."""
    ccy = str(currency or "CNY").upper()
    ordered = [
        row
        for row in sorted(rows, key=lambda item: (_row_date(item.get("transaction_date")) or Date.min, item.get("id") or 0))
        if str(row.get("currency") or ccy).upper() == ccy
    ]
    if not ordered:
        return None

    latest_prior: tuple[int, Date, Decimal] | None = None
    first_future: tuple[int, Date, Decimal] | None = None
    latest_balance_date: Date | None = None
    for index, row in enumerate(ordered):
        tx_date = _row_date(row.get("transaction_date"))
        balance = _statement_balance(row)
        if tx_date is None or balance is None:
            continue
        latest_balance_date = tx_date if latest_balance_date is None else max(latest_balance_date, tx_date)
        if tx_date <= sample_date:
            latest_prior = (index, tx_date, balance)
        elif first_future is None:
            first_future = (index, tx_date, balance)

    if latest_prior is not None:
        return latest_prior[2], latest_balance_date or latest_prior[1]
    if first_future is None:
        return None

    future_index, future_date, future_balance = first_future
    reverse_flow = Decimal("0")
    for row in ordered[: future_index + 1]:
        tx_date = _row_date(row.get("transaction_date"))
        if tx_date is not None and sample_date < tx_date <= future_date:
            reverse_flow += _dec(row.get("amount"))
    if liability:
        return future_balance + reverse_flow, latest_balance_date or future_date
    return future_balance - reverse_flow, latest_balance_date or future_date


def _cash_account_at_terminal(account: dict[str, Any], terminal_date: Date, dated_cash=None) -> dict[str, Any]:
    if ((account.get('primary_balance') or {}).get('source') == VERIFIED_BANK_BALANCE_SOURCE
            and account_anchor_date(account, terminal_date) > terminal_date):
        previous = [balance for balance in (account.get('manual_balance'), account.get('statement_balance'))
                    if balance and (_row_date(balance.get('as_of')) or Date.max) <= terminal_date]
        account = {**account, 'primary_balance': max(previous, key=lambda balance: str(balance['as_of']), default=None)}
    if (dated_cash and account.get('account_type') == 'funding'
            and account.get('truth_source') in {'bank_statement', 'official_statement'}
            and (not account_anchor_amount(account) or dated_cash['as_of'] >= account_anchor_date(account, terminal_date))):
        # This anchor is only for reconstruction with dated movements.
        ratio = Decimal(str(account.get('owner_ratio', 1)))
        return {**account, 'primary_balance': {'amount': dated_cash['market_value'] * ratio, 'currency': dated_cash['currency'],
                'as_of': dated_cash['as_of'], 'source': VERIFIED_BANK_BALANCE_SOURCE, 'point_in_time_only': True}}
    return account


def _bank_backcast_series(
    *,
    accounts: list[dict[str, Any]],
    rows_by_account: dict[str, list[dict[str, Any]]],
    dates: list[Date],
    terminal_date: Date,
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
    base_currency: str,
) -> dict[Date, dict[str, Decimal]]:
    """Back-cast cash-like account balances from confirmed terminal balances and statement flows."""
    values: dict[Date, dict[str, Any]] = {d: {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")} for d in dates}
    for account in accounts:
        verified_snapshot = (account.get('primary_balance') or {}).get('source') == VERIFIED_BANK_BALANCE_SOURCE
        anchor = account_anchor_amount(account)
        code = str(account.get("account_code") or "")
        rows = rows_by_account.get(code, [])
        problem_dates = getattr(rows_by_account, 'evidence_problems', {}).get(code, {})
        if anchor is None:
            anchor_currency = str(account.get("base_currency") or "CNY").upper()
            anchor_amount = Decimal("0")
            anchor_date = terminal_date
        else:
            anchor_amount, anchor_currency = anchor
            anchor_date = account_anchor_date(account, terminal_date)
        effective_date = anchor_date
        owner_ratio = Decimal(str(account.get('owner_ratio', 1)))
        native_movements = [
            (row['transaction_date'], _dec(row['amount'])) for row in rows
            if str(row.get('currency') or anchor_currency).upper() == anchor_currency
        ]
        evidence_dates = [day for day, _ in native_movements]
        dated_anchor = _row_date((account.get('primary_balance') or {}).get('as_of') or account.get('as_of'))
        if anchor is not None and dated_anchor:
            evidence_dates.append(anchor_date)
        evidence_start = min(evidence_dates, default=Date.max)
        evidence_end = max(evidence_dates, default=Date.min)
        ordered_dates = sorted(evidence_dates)
        reference_account = anchor_currency != base_currency and supports_cash_reference(account, anchor_currency, rows)

        for sample_date in dates:
            bucket = values[sample_date]
            # An anchored reconstruction can be compared within its dated evidence
            # span. Display may also carry a latest balance past that span.
            if evidence_start <= sample_date <= evidence_end:
                bucket.setdefault('_dated_cash_accounts', set()).add(code)
            start, end = sorted((anchor_date if anchor is not None else sample_date, sample_date))
            # Only failures in the balance's calculation interval can invalidate
            # it. A verified snapshot is independent of movements on its own day.
            if not verified_snapshot or sample_date != anchor_date:
                problems = None in problem_dates or any(
                    (start < day <= end if verified_snapshot else start <= day <= end)
                    for day in problem_dates if day is not None
                )
                if problems:
                    bucket.setdefault('unavailable_accounts', []).append(code)
                    bucket.setdefault('source_evidence_unavailable_accounts', []).append(code)
                    continue
            movements = [amount for day, amount in native_movements if start < day <= end]
            if sample_date != anchor_date and (account.get('primary_balance') or {}).get('point_in_time_only') and not movements:
                bucket.setdefault('unavailable_accounts', []).append(code)
                continue
            flow = sum(movements, Decimal(0)) * owner_ratio
            projected_amount = anchor_amount + (flow if sample_date >= anchor_date else -flow)
            ref_dates = None
            if reference_account and dated_anchor and sample_date.weekday() >= 5:
                through = bisect_right(ordered_dates, sample_date)
                if through:
                    ref_dates = max(ordered_dates[through-1], anchor_date), dated_anchor
            if verified_snapshot:
                rate = fx_rate_at(fx_timelines, anchor_currency, sample_date, base_currency)
                if rate is None:
                    bucket.setdefault('unavailable_accounts', []).append(code)
                    if ref_dates:
                        bucket.setdefault('_unconverted_cash', {})[code] = (anchor_currency, projected_amount, *ref_dates)
                    continue
                account_value = projected_amount * rate
                add_nav_amount(bucket, account_value)
                bucket.setdefault('account_values', {})[code] = account_value
                bucket.setdefault('available_accounts', []).append(code)
                bucket.setdefault('_comparison_basis', {})[code] = 'cash_balance'
                if sample_date != anchor_date:
                    bucket.setdefault('estimated_accounts', []).append(code)
                continue
            statement_value = _statement_balance_native_value(
                rows,
                sample_date=sample_date,
                currency=anchor_currency,
                liability=str(account.get("account_type") or "") == "liability",
            )
            if statement_value is not None:
                statement_native_value, latest_statement_date = statement_value
            else:
                statement_native_value = None
                latest_statement_date = None

            if (
                statement_native_value is not None
                and not (anchor is not None and latest_statement_date is not None and sample_date > latest_statement_date and anchor_date >= latest_statement_date)
            ):
                native_value = statement_native_value
                basis = 'cash_balance'
            elif anchor is None or (sample_date < effective_date and not movements):
                bucket.setdefault('unavailable_accounts', []).append(code)
                continue
            else:
                native_value = projected_amount
                basis = 'cash_anchor'

            signed_native_value = signed_account_native_value(account, native_value)
            rate = fx_rate_at(fx_timelines, anchor_currency, sample_date, base_currency)
            if rate is None:
                bucket.setdefault("unavailable_accounts", []).append(code)
                if ref_dates:
                    bucket.setdefault('_unconverted_cash', {})[code] = (anchor_currency, signed_native_value, *ref_dates)
            else:
                account_value = signed_native_value * rate
                add_nav_amount(bucket, account_value)
                bucket.setdefault("account_values", {})[code] = account_value
                bucket.setdefault("available_accounts", []).append(code)
                bucket.setdefault('_comparison_basis', {})[code] = basis
                # Cash movements can support an estimate away from the anchor.
                # An individual verified movement does not prove full coverage.
                if isinstance(rows_by_account, StatementRowsByAccount) and sample_date != anchor_date:
                    bucket.setdefault('estimated_accounts', []).append(code)
    return values


def _historical_cash_points(cur, accounts, through):
    """Load source-bound cash observations, including dates outside the chart grid."""
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    by_code = {account['account_code']: account for account in accounts}
    if not by_code:
        return {}
    cur.execute("""SELECT DISTINCT h.id,h.account_code,h.as_of,h.currency,h.asset_liability,h.market_value
        FROM pams_holdings_snapshot h JOIN account_snapshot_member m ON m.holding_id=h.id
        JOIN account_snapshot_version v ON v.version_id=m.version_id
        WHERE h.account_code=ANY(%s::text[]) AND h.as_of<=%s AND h.major_category='现金'
          AND v.coverage_status='partial' AND v.scope_attestation->'point_in_time_only'='true'::jsonb""",
        (list(by_code), through))
    rows = cur.fetchall()
    coverage = load_account_snapshot_coverage(cur, {(r['account_code'], r['as_of']) for r in rows})
    candidates = defaultdict(list)
    for row in rows:
        code, day = row['account_code'], row['as_of']
        proof = coverage[code, day]
        if (row['id'] in proof['member_ids'] and row['id'] in proof.get('point_in_time_member_ids', ())
                and row['currency'] == str(by_code[code].get('base_currency') or 'CNY').upper()
                and row['market_value'] is not None):
            complete = (proof['status'] == 'complete' and proof['cash_status'] == 'known'
                        and proof['member_ids'] == {row['id']})
            candidates[code, day].append({**row, 'complete_cash_scope': complete})
    return {key: rows[0] for key, rows in candidates.items() if len(rows) == 1}


def _replace_cash_contribution(bucket, code, value):
    bucket.get('_month_end_marks', {}).pop(code, None)
    bucket.get('_unconverted_cash', {}).pop(code, None)
    old = bucket.setdefault('account_values', {}).pop(code, None)
    if old is not None:
        bucket['liabilities' if old < 0 else 'assets'] -= abs(old)
        bucket['net_worth'] -= old
    if value is None:
        bucket.get('_comparison_basis', {}).pop(code, None)
        for field in ('available_accounts', 'estimated_accounts'):
            if code in bucket.get(field, ()):
                bucket[field].remove(code)
        if code not in bucket.setdefault('unavailable_accounts', []):
            bucket['unavailable_accounts'].append(code)
        return
    add_nav_amount(bucket, value)
    bucket['account_values'][code] = value
    for field in ('available_accounts', 'estimated_accounts'):
        if code not in bucket.setdefault(field, []):
            bucket[field].append(code)


def _cash_statement_accounts(accounts):
    return {a['account_code']: (str(a.get('base_currency') or 'CNY').upper(),
                                a.get('account_type') in ('liability', 'credit_card'))
            for a in accounts}


def _apply_historical_cash_points(cur, *, bank_series, accounts, dates, rate_at, points=None, periods=None, unbilled=None):
    """Reject a later anchor's backcast where it fails to reconcile to a dated balance."""
    if not dates:
        return
    if points is None:
        points = _historical_cash_points(cur, accounts, max(dates))
    if periods is None:
        periods = statement_valuations(cur, _cash_statement_accounts(accounts), dates)
    for account in accounts:
        code = account['account_code']
        ratio = Decimal(str(account.get('owner_ratio', 1)))
        anchor = account_anchor_date(account, max(dates))
        conflict = False
        previous_day = Date.min
        for day in sorted(bank_series):
            if previous_day < anchor <= day:
                conflict = False
            previous_day = day
            bucket = bank_series[day]
            point = points.get((code, day))
            complete_cash = bool(point and point.get('complete_cash_scope'))
            period = periods.get((code, day))
            observation = None
            if point is not None:
                signed = -abs(point['market_value']) if point['asset_liability'] == '负债' else point['market_value']
                observation = AccountValuation(day, point['currency'], signed, 'snapshot')
            contracts = (unbilled or {}).get((day, code), {})
            included = {}
            if period is not None:
                if period.scope == 'billed':
                    included = {member: proof[0] for member, proof in contracts.items() if member[1] == period.currency}
                principal = sum(included.values(), Decimal(0))
                resolved = period.including_unbilled(principal)
                # A point may attest the billed balance or the already-combined account balance.
                if (resolved.amount is None or observation and (observation.currency != period.currency
                        or not period.agrees_with_balance(observation.amount, principal))):
                    _replace_cash_contribution(bucket, code, None)
                    bucket.setdefault('source_evidence_unavailable_accounts', []).append(code)
                    conflict = True
                    continue
                observation = resolved
            if observation is not None:
                if code in bucket.get('source_evidence_unavailable_accounts', ()):
                    continue
                amount = observation.personal_value(ratio, rate_at(observation.currency, day))
                if amount is None:
                    _replace_cash_contribution(bucket, code, None)
                    if is_month_end(day) and (period or complete_cash):
                        bucket.setdefault('_month_end_marks', {})[code] = (observation, ratio)
                    continue
                backcast = bucket.get('account_values', {}).get(code)
                conflict = backcast is not None and backcast != amount
                _replace_cash_contribution(bucket, code, amount)
                bucket.setdefault('_comparison_basis', {})[code] = 'cash_balance'
                bucket.setdefault('_dated_cash_accounts', set()).add(code)
                if period and period.evidence_dates:
                    first, last = period.evidence_dates
                    bucket.setdefault('latest_known_components', {})[code] = {
                        'basis': period.basis, 'from': first.isoformat(), 'through': last.isoformat(),
                        'balance_as_of': day.isoformat(), 'currency': period.currency,
                        'billed_balance': str(period.amount)}
                    bucket['_comparison_basis'][code] = period.basis
                if (period or complete_cash) and code in bucket.get('unavailable_accounts', ()):
                    bucket['unavailable_accounts'].remove(code)
                verified_closing = period and (period.basis == 'statement' or
                                               period.evidence_dates and period.quality == 'confirmed')
                if complete_cash or (verified_closing and not included):
                    bucket['estimated_accounts'].remove(code)
                if included:
                    bucket['_comparison_basis'][code] = ['billed_and_unbilled_debt', sorted(included)]
                    if code not in bucket.setdefault('unavailable_accounts', []):
                        bucket['unavailable_accounts'].append(code)
            elif conflict and day != anchor and code in bucket.get('estimated_accounts', ()):
                _replace_cash_contribution(bucket, code, None)


def _tonghuashun_series_from_trade_fact(
    cur,
    *,
    dates: list[Date],
    account_owner_ratio,
    ownership_config=None,
) -> ValuationEvidenceSeries:
    """Value Tonghuashun A-shares from the current investment trade fact table."""
    values = ValuationEvidenceSeries({d: Decimal("0") for d in dates})
    if not dates:
        return values
    owner_ratio = account_owner_ratio("tonghuashun")
    cur.execute(
        """
        SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS fact_exists,
               to_regclass('public.pams_ohlcv_daily') IS NOT NULL AS px_exists,
               to_regclass('public.source_items') IS NOT NULL AS item_exists,
               to_regclass('public.source_batches') IS NOT NULL AS batch_exists,
               to_regclass('public.source_records') IS NOT NULL AS redesigned
        """
    )
    exists = cur.fetchone()
    if exists and exists.get('redesigned'):
        from domain.investments.nav_trade_source_v2 import project
        return project(cur, dates=dates, owner_ratio=owner_ratio, series_type=ValuationEvidenceSeries,
                       ownership_config=ownership_config)
    if ownership_config is not None:
        values.unknown_dates.update(dates)
        return values
    if not exists or not exists["fact_exists"] or not exists["px_exists"]:
        values.unknown_dates.update(dates)
        if exists and not exists["px_exists"]:
            values.missing_price_dates.update(dates)
        return values

    if exists.get("item_exists") and exists.get("batch_exists"):
        cur.execute("""SELECT scope_attestation FROM source_batches WHERE account_code='tonghuashun'
            AND workflow='trade_snapshot' AND status='published' ORDER BY created_at DESC LIMIT 1""")
        scope = cur.fetchone()
        attestation = (scope or {}).get('scope_attestation') or {}
        if attestation.get('cash_coverage') == 'unknown':
            values.cash_scope_unverified = True
            values.unknown_dates.update(dates)
        source_filter = """((source_item_id IS NULL AND source_table NOT IN
            ('tradingview_csv','source_items','ibkr_flex_trade')) OR EXISTS (
            SELECT 1 FROM source_items si JOIN source_batches sb ON sb.batch_id=si.batch_id
            WHERE si.source_item_id=pams_investment_trade_fact.source_item_id
              AND si.review_status='confirmed' AND sb.status='published'
              AND sb.account_code=pams_investment_trade_fact.account_code))"""
    else:
        source_filter = "source_table NOT IN ('tradingview_csv','source_items','ibkr_flex_trade')"
    # A published status alone does not prove that the immutable fact still
    # matches the reviewed source. Mark every affected day unknown on mismatch.
    if exists.get("item_exists") and exists.get("batch_exists"):
        cur.execute("""SELECT f.trade_id,f.trade_date,f.source_table
            FROM pams_investment_trade_fact f
            WHERE f.account_code='tonghuashun' AND f.trade_date <= %s
              AND LOWER(f.review_status) IN ('auto','reviewed','confirmed')
              AND LOWER(COALESCE(NULLIF(f.side,''),f.trade_type)) IN
                ('deposit','cash_deposit','transfer_in','withdraw','cash_withdrawal','transfer_out','buy','sell','dividend')
              """, (max(dates),))
        candidates = [dict(row) for row in cur.fetchall()]
        native_ids = [int(row['trade_id']) for row in candidates if row['source_table']=='source_items']
        tv_ids = [int(row['trade_id']) for row in candidates if row['source_table']=='tradingview_csv']
        from domain.imports.source_fact_evidence import NativeFactVerifier
        from domain.imports.tradingview_fact_verification import TradingViewFactVerifier
        native = NativeFactVerifier(cur).verify_many(table='pams_investment_trade_fact',fact_ids=native_ids) if native_ids else {}
        tv = TradingViewFactVerifier(cur).verify_many(tv_ids) if tv_ids else {}
        for row in candidates:
            ident, table = int(row['trade_id']), row['source_table']
            verified = (native.get(ident, {}).get('status') == 'verified' if table == 'source_items'
                        else tv.get(ident, False) if table == 'tradingview_csv' else False)
            if not verified:
                values.unknown_dates.update(day for day in dates if day >= row['trade_date'])
    else:
        values.unknown_dates.update(dates)
    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), tx AS (
            SELECT
                trade_date,
                NULLIF(regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g'), '') AS stock_code,
                LOWER(COALESCE(NULLIF(side, ''), trade_type)) AS side,
                ABS(COALESCE(quantity, 0)) AS quantity,
                COALESCE(price, 0) AS price,
                ABS(COALESCE(gross_amount, COALESCE(quantity, 0) * COALESCE(price, 0), 0)) AS gross_amount,
                COALESCE(fee_amount, 0) + COALESCE(tax_amount, 0) AS commission,
                tax_amount,
                net_cash,
                net_cash_cny,
                COALESCE(NULLIF(currency, ''), 'CNY') AS currency,
                CASE WHEN COALESCE(NULLIF(currency, ''), 'CNY') = 'CNY' THEN 1 ELSE (
                    SELECT fx.rate FROM pams_fx_rates_daily fx
                    WHERE fx.base_currency = 'CNY' AND fx.quote_currency = pams_investment_trade_fact.currency
                      AND fx.as_of = pams_investment_trade_fact.trade_date
                      AND fx.rate > 0 AND fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                      AND NULLIF(BTRIM(fx.source), '') IS NOT NULL
                    LIMIT 1
                ) END AS fx_rate_to_cny
            FROM pams_investment_trade_fact
            WHERE account_code = 'tonghuashun'
              AND LOWER(review_status) IN ('auto','reviewed','confirmed')
              AND {source_filter}
              AND LOWER(COALESCE(NULLIF(side, ''), trade_type)) IN (
                  'deposit', 'cash_deposit', 'transfer_in',
                  'withdraw', 'cash_withdrawal', 'transfer_out',
                  'buy', 'sell', 'dividend'
              )
        ), trade_events AS (
            SELECT
                trade_date,
                stock_code,
                CASE
                    WHEN side = 'buy' THEN quantity
                    WHEN side = 'sell' THEN -quantity
                    ELSE 0
                END AS quantity_delta
            FROM tx
            WHERE side IN ('buy', 'sell')
              AND stock_code IS NOT NULL
        ), daily_positions AS (
            SELECT d.date, e.stock_code, SUM(e.quantity_delta) AS quantity
            FROM series_dates d
            JOIN trade_events e ON e.trade_date <= d.date
            GROUP BY d.date, e.stock_code
            HAVING SUM(e.quantity_delta) <> 0
        ), priced_positions AS (
            SELECT
                dp.date,
                dp.quantity,
                COALESCE(px.close, CASE WHEN cm.as_of = dp.date AND cm.current_price > 0
                    THEN cm.current_price END) AS price,
                COALESCE(px.trade_date, CASE WHEN cm.as_of = dp.date AND cm.current_price > 0
                    THEN cm.as_of END) AS price_date
            FROM daily_positions dp
            LEFT JOIN pams_holdings_snapshot cm
              ON cm.account_code = 'tonghuashun'
             AND cm.symbol = dp.stock_code
             AND cm.as_of = (
                 SELECT MAX(as_of)
                 FROM pams_holdings_snapshot
                 WHERE account_code = 'tonghuashun'
                   AND major_category = 'A股'
                   AND minor_category = 'A股'
             )
            LEFT JOIN LATERAL (
                SELECT close, trade_date
                FROM pams_ohlcv_daily o
                WHERE o.symbol = CASE WHEN LEFT(dp.stock_code, 1) = '6'
                                       THEN 'sh' || dp.stock_code
                                       ELSE 'sz' || dp.stock_code
                                  END
                  AND o.market = 'CN_A'
                  AND o.trade_date <= dp.date
                  AND o.close > 0 AND o.close::text NOT IN ('NaN','Infinity','-Infinity')
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) px ON TRUE
        ), equity AS (
            SELECT date, SUM(quantity * price) AS value_cny,
                   BOOL_OR(price IS NULL) AS missing_price,
                   BOOL_OR(price IS NOT NULL AND price_date < date) AS estimated_price
            FROM priced_positions
            GROUP BY date
        ), cash_events AS (
            SELECT trade_date AS event_date,
                   CASE
                       WHEN fx_rate_to_cny IS NULL THEN NULL
                       WHEN currency <> 'CNY' AND net_cash IS NOT NULL AND side IN ('buy','sell','dividend')
                       THEN net_cash * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND net_cash IS NOT NULL AND side IN ('deposit','cash_deposit','transfer_in')
                       THEN ABS(net_cash) * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND net_cash IS NOT NULL AND side IN ('withdraw','cash_withdrawal','transfer_out')
                       THEN -ABS(net_cash) * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND side IN ('buy','sell') AND tax_amount IS NULL THEN NULL
                       WHEN currency <> 'CNY' AND side = 'buy' THEN -(gross_amount + commission) * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND side = 'sell' THEN (gross_amount - commission) * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND side IN ('deposit','cash_deposit','transfer_in','dividend') THEN gross_amount * fx_rate_to_cny
                       WHEN currency <> 'CNY' AND side IN ('withdraw','cash_withdrawal','transfer_out') THEN -gross_amount * fx_rate_to_cny
                       WHEN side IN ('buy', 'sell') AND tax_amount IS NULL AND net_cash_cny IS NOT NULL
                       THEN net_cash_cny
                       WHEN side IN ('buy', 'sell') AND tax_amount IS NULL AND net_cash IS NOT NULL
                       THEN net_cash * fx_rate_to_cny
                       WHEN side IN ('buy', 'sell') AND tax_amount IS NULL THEN NULL
                       WHEN side IN ('deposit', 'cash_deposit', 'transfer_in')
                       THEN COALESCE(NULLIF(net_cash_cny, 0), NULLIF(net_cash * fx_rate_to_cny, 0), NULLIF(gross_amount * fx_rate_to_cny, 0), gross_amount, 0)
                       WHEN side IN ('withdraw', 'cash_withdrawal', 'transfer_out')
                       THEN -ABS(COALESCE(NULLIF(net_cash_cny, 0), NULLIF(net_cash * fx_rate_to_cny, 0), NULLIF(gross_amount * fx_rate_to_cny, 0), gross_amount, 0))
                       WHEN side = 'buy'
                       THEN COALESCE(NULLIF(net_cash_cny, 0), NULLIF(net_cash * fx_rate_to_cny, 0), -(gross_amount + commission) * fx_rate_to_cny)
                       WHEN side = 'sell'
                       THEN COALESCE(NULLIF(net_cash_cny, 0), NULLIF(net_cash * fx_rate_to_cny, 0), (gross_amount - commission) * fx_rate_to_cny)
                       WHEN side = 'dividend'
                       THEN COALESCE(NULLIF(net_cash_cny, 0), NULLIF(net_cash * fx_rate_to_cny, 0), gross_amount * fx_rate_to_cny)
                       ELSE 0
                   END AS cash_delta
            FROM tx
        ), cash AS (
            SELECT d.date, COALESCE(SUM(e.cash_delta), 0) AS cash_cny,
                   COUNT(*) FILTER (WHERE e.event_date IS NOT NULL AND e.cash_delta IS NULL) > 0 AS has_unknown_tax
            FROM series_dates d
            LEFT JOIN cash_events e ON e.event_date <= d.date
            GROUP BY d.date
        )
        SELECT d.date,
               (COALESCE(e.value_cny, 0) + COALESCE(c.cash_cny, 0)) AS gross_value_cny,
               COALESCE(c.has_unknown_tax, FALSE) AS has_unknown_tax,
               COALESCE(e.missing_price, FALSE) AS missing_price,
               COALESCE(e.estimated_price, FALSE) AS estimated_price
        FROM series_dates d
        LEFT JOIN equity e ON e.date = d.date
        LEFT JOIN cash c ON c.date = d.date
        ORDER BY d.date
        """,
        ([d.isoformat() for d in dates],),
    )
    for row in cur.fetchall():
        values[row["date"]] = _dec(row["gross_value_cny"]) * owner_ratio
        if row.get("has_unknown_tax") or row.get("missing_price"):
            values.unknown_dates.add(row["date"])
        if row.get("missing_price"):
            values.missing_price_dates.add(row["date"])
        if row.get("estimated_price"):
            values.estimated_dates.add(row["date"])
    return values


def _tonghuashun_series(cur, *, dates: list[Date], base_currency: str, account_owner_ratio, ownership_config=None) -> ValuationEvidenceSeries:
    """Convert the CNY reconstruction to the requested currency using daily evidence."""
    values = _tonghuashun_series_from_trade_fact(cur, dates=dates, account_owner_ratio=account_owner_ratio,
                                                 ownership_config=ownership_config)
    if base_currency.upper() == 'CNY':
        return values
    rates = load_fx_timelines(cur, base_currency)
    for day in dates:
        amount = values.get(day, Decimal(0))
        rate = fx_rate_at(rates, 'CNY', day, base_currency)
        if rate is None:
            if amount != 0:
                values.unknown_dates.add(day)
            values.preview_by_date.pop(day, None)
        else:
            values[day] = amount * rate
            if day in values.preview_by_date:
                values.preview_by_date[day] *= rate
    return values


def build_nav_weekly_payload(
    cur,
    *,
    start_date: Date,
    base_currency: str,
    overview_payload: dict[str, Any],
    owner_ratio_case_sql,
    account_owner_ratio,
    terminal_date: Date | None = None,
    terminal_nav_bucket: dict[str, Any] | None = None,
    terminal_account_values_cny: dict[str, Any] | None = None,
    terminal_display_values_cny: dict[str, tuple[Decimal, Date, Date]] | None = None,
    snapshot_scope_incomplete_accounts: set[str] | None = None,
    full_personal_portfolio: bool = False,
    account_histories: dict | None = None,
    evidence_through: Date | None = None,
    include_ibkr: bool = True,
    interval: str = "weekly",
    account_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from domain.accounts.valuation_book import read_account_book, render_histories
    terminal_date = terminal_date or ANCHOR_NAV_DATE
    start_date = min(start_date, terminal_date)
    cfg = account_config or {'accounts': {a['account_code']: a for a in overview_payload['accounts']}}
    accounts = overview_payload['accounts']
    if not include_ibkr:
        accounts = [a for a in accounts if cfg.get('accounts', {}).get(a['account_code'], {}).get('portfolio_value_evidence') != 'official_broker_close']
    values, rows = read_account_book(cur, accounts=accounts, config=cfg,
        start=start_date, end=terminal_date, evidence_through=evidence_through or terminal_date,
        base=base_currency, owner_ratio=account_owner_ratio, full_portfolio=full_personal_portfolio)
    if account_histories is not None:
        account_histories.update(render_histories(values, start_date, evidence_through or terminal_date))
    samples = set(series_dates(start_date, terminal_date, interval))
    return format_weekly_nav_payload(start_date=start_date, terminal_date=terminal_date, base_currency=base_currency,
        rows=[row for row in rows if row['date'] in samples], interval=interval,
        account_labels={a['account_code']: a.get('display_name') for a in overview_payload['accounts']})


def build_nav_series_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Project the verified account-level NAV timeline into the legacy shape."""
    result = []
    for row in rows:
        value = row.get("netWorth")
        item = {"asOfDate": row.get("asOfDate"), "netWorth": float(value) if value is not None else None}
        if value is None:
            item.update(scope_status="incomplete", unavailable_accounts=row.get("unavailable_accounts", []))
        if row.get("valuation_quality"):
            item["valuation_quality"] = row["valuation_quality"]
            item["estimated_accounts"] = row.get("estimated_accounts", [])
        result.append(item)
    return {"rows": result}


def build_nav_drawdown_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute drawdown only while the complete NAV history remains known."""
    out: list[dict[str, Any]] = []
    peak: Decimal | None = None
    max_drawdown: Decimal | None = None
    history_incomplete = False
    for row in rows:
        raw_value = row.get("netWorth")
        value = Decimal(str(raw_value)) if raw_value is not None else None
        if value is None:
            history_incomplete = True
        drawdown = None
        if value is not None and not history_incomplete:
            peak = value if peak is None else max(peak, value)
            drawdown = Decimal("0") if not peak else (value / peak - Decimal("1"))
            max_drawdown = drawdown if max_drawdown is None else min(max_drawdown, drawdown)
        out.append({
            "as_of": row.get("asOfDate"),
            "total_market_value": _norm(value),
            "completeness": None if value is None else "1",
            "peak": _norm(peak) if not history_incomplete else None,
            "drawdown": _norm(drawdown),
            **({"scope_status": "incomplete", "unavailable_accounts": row.get("unavailable_accounts", [])}
               if value is None else {}),
            **({"valuation_quality": row["valuation_quality"], "estimated_accounts": row.get("estimated_accounts", [])}
               if row.get("valuation_quality") else {}),
        })
    return {"max_drawdown": None if history_incomplete or max_drawdown is None else str(max_drawdown), "rows": out}
