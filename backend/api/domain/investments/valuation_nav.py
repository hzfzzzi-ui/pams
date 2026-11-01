"""NAV and valuation read-service boundary.

Batch 2c starts with this module as a staging area for moving NAV helpers out of
``main.py``.  The first rule is compatibility: callers must keep the old API
response shape until the frontend has explicitly migrated.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date as Date, timedelta
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any

from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE

ANCHOR_NAV_DATE = Date(2026, 5, 31)
STATEMENT_BACKCAST_TYPES = {"funding", "cash_management", "benefit", "liability"}
INVESTMENT_SNAPSHOT_ACCOUNT_TYPES = {"brokerage", "fund_platform"}
STABLE_ANCHOR_ACCOUNTS = {"huawei_stock"}
INVESTMENT_SNAPSHOT_EXCLUDE = {"tonghuashun", "ibkr", *STABLE_ANCHOR_ACCOUNTS}


class ValuationEvidenceSeries(dict):
    """Values plus dates where an input is missing; consumers must honor the latter."""
    def __init__(self, values=(), *, unknown_dates=(), estimated_dates=()):
        super().__init__(values)
        self.unknown_dates = set(unknown_dates)
        self.estimated_dates = set(estimated_dates)
        self.unknown_accounts_by_date: dict[Date, list[str]] = {}


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


def manual_effective_date(account: dict[str, Any], terminal_date: Date) -> Date:
    """Return when a manual balance should begin contributing to the NAV curve."""
    if str(account.get("account_code") or "") in STABLE_ANCHOR_ACCOUNTS:
        return Date(2026, 1, 1)
    balance = account.get("manual_balance") or account.get("primary_balance") or {}
    raw = balance.get("as_of") or account.get("as_of") or terminal_date
    if isinstance(raw, Date):
        return raw
    try:
        return Date.fromisoformat(str(raw)[:10])
    except ValueError:
        return terminal_date


def select_weekly_nav_accounts(
    overview_accounts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split account-center rows into statement-backcast and snapshot-valued NAV inputs."""
    bank_accounts = [
        account
        for account in overview_accounts
        if str(account.get("account_type") or "") in STATEMENT_BACKCAST_TYPES
        or str(account.get("truth_source") or "") in {"bank_statement", "official_statement"}
        or str(account.get("account_type") or "") in {"bridge", "receivable"}
        or str(account.get("account_code") or "") in STABLE_ANCHOR_ACCOUNTS
    ]
    investment_snapshot_accounts = [
        str(account.get("account_code"))
        for account in overview_accounts
        if str(account.get("account_type") or "") in INVESTMENT_SNAPSHOT_ACCOUNT_TYPES
        and str(account.get("account_code")) not in INVESTMENT_SNAPSHOT_EXCLUDE
    ]
    return bank_accounts, investment_snapshot_accounts


def load_fx_timelines(
    cur,
    base_currency: str,
    *,
    identity_date: Date | None = None,
) -> dict[str, list[tuple[Date, Decimal]]]:
    """Load FX rates once so valuation back-casting can convert by sample date."""
    base_currency = base_currency.upper()
    cur.execute(
        """
        SELECT base_currency, quote_currency, as_of, rate
        FROM pams_fx_rates_daily
        WHERE base_currency = ANY(%s::text[])
          AND rate > 0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
          AND NULLIF(BTRIM(source), '') IS NOT NULL
        ORDER BY base_currency, quote_currency, as_of
        """,
        ([base_currency, 'CNY'],),
    )
    direct: dict[tuple[str, Date], Decimal] = {}
    cny: dict[tuple[str, Date], Decimal] = {}
    for row in cur.fetchall():
        currency = str(row["quote_currency"] or "").upper()
        day = row["as_of"]
        rate = _dec(row["rate"])
        if str(row.get("base_currency") or base_currency).upper() == base_currency:
            direct[(currency, day)] = rate
        if str(row.get("base_currency") or base_currency).upper() == 'CNY':
            cny[(currency, day)] = rate
    if base_currency != 'CNY':
        for (currency, day), rate in cny.items():
            denominator = cny.get((base_currency, day))
            if denominator is not None:
                direct.setdefault((currency, day), rate / denominator)
                direct.setdefault(('CNY', day), Decimal(1) / denominator)
    timelines: dict[str, list[tuple[Date, Decimal]]] = {}
    for (currency, day), rate in sorted(direct.items(), key=lambda item: (item[0][0], item[0][1])):
        timelines.setdefault(currency, []).append((day, rate))
    timelines.setdefault(base_currency, []).append((identity_date or Date(2026, 5, 31), Decimal("1")))
    return timelines


def fx_rate_at(
    timelines: dict[str, list[tuple[Date, Decimal]]],
    currency: str,
    as_of: Date,
    base_currency: str,
) -> Decimal | None:
    """Return a same-day rate, or unknown when the evidence is absent."""
    quote = str(currency or base_currency).upper()
    if quote == str(base_currency).upper():
        return Decimal("1")
    rows = timelines.get(quote) or []
    for row_date, rate in reversed(rows):
        if row_date == as_of:
            return rate if rate.is_finite() and rate > 0 else None
    return None


def scale_series_to_terminal_anchor(
    series: dict[Date, Decimal],
    *,
    terminal_date: Date,
    target_value: Decimal | None,
) -> ValuationEvidenceSeries:
    """Scale a reconstructed curve so the terminal value matches a confirmed account anchor."""
    adjusted = ValuationEvidenceSeries(series, unknown_dates=getattr(series, "unknown_dates", ()),
                                       estimated_dates=getattr(series, "estimated_dates", ()))
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
                                   unknown_dates=adjusted.unknown_dates, estimated_dates=adjusted.estimated_dates)


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
        if missing_accounts:
            rows.append({"date": sample_date, "assets": None, "liabilities": None,
                         "net_worth": None, "unavailable_accounts": sorted(set(missing_accounts)),
                         **({"source_evidence_unavailable_accounts": sorted(set(bank_bucket['source_evidence_unavailable_accounts']))}
                            if bank_bucket.get('source_evidence_unavailable_accounts') else {}),
                         **({"ibkr_history_evidence": "unavailable"} if "ibkr" in missing_accounts else {}),
                         **({"ibkr_daily_close": ibkr_bucket["daily_close"]} if "daily_close" in ibkr_bucket else {})})
            continue
        bucket["assets"] += ibkr_bucket.get("assets", Decimal("0"))
        bucket["liabilities"] += ibkr_bucket.get("liabilities", Decimal("0"))
        bucket["net_worth"] += ibkr_bucket.get("net_worth", Decimal("0"))
        row: dict[str, Any] = {"date": sample_date, **bucket}
        estimated = set(bank_bucket.get('estimated_accounts') or ())
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
                         and (row.get("snapshot_scope_status") in {"partial", "unknown"} or row.get("fx_status") == "unavailable")
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
    values: dict[str, Decimal] = {}
    official = current_daily_close_accounts(account_balances, investment_positions)
    incomplete = {str(row.get("account_code")) for row in [*account_balances, *investment_positions]
                  if row.get("account_code") and row.get("include_in_nav", True)
                  and (row.get("snapshot_scope_status") in {"partial", "unknown"} or row.get("fx_status") == "unavailable")
                  and str(row.get("account_code")) not in official}
    for row in investment_positions:
        account_code = str(row.get("account_code") or "")
        if not account_code or account_code in incomplete:
            continue
        if account_code in official:
            continue
        values[account_code] = values.get(account_code, Decimal("0")) + _dec(row.get("market_value_cny"))
    for row in account_balances:
        account_code = str(row.get("account_code") or "")
        if account_code in incomplete:
            continue
        if account_code in official:
            values[account_code] = _dec(official.pop(account_code)["amount_cny"])
            continue
        if not account_code or not row.get("include_in_nav", True) or account_code in represented_accounts:
            continue
        values[account_code] = values.get(account_code, Decimal("0")) + _dec(row.get("amount_cny"))
    return values


def normalize_nav_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize internal NAV keys without changing the legacy API field names."""
    result: dict[str, Any] = {
        "date": _norm(row.get("date")),
        "asOfDate": _norm(row.get("date")),
        "assets": _norm(row.get("assets", Decimal("0"))),
        "liabilities": _norm(row.get("liabilities", Decimal("0"))),
        "netWorth": _norm(row.get("net_worth", Decimal("0"))),
    }
    if "ibkr_daily_close" in row:
        result["ibkr_daily_close"] = row["ibkr_daily_close"]
    if "ibkr_history_evidence" in row:
        result["ibkr_history_evidence"] = row["ibkr_history_evidence"]
    if "unavailable_accounts" in row:
        result["unavailable_accounts"] = row["unavailable_accounts"]
    if "scope_status" in row:
        result["scope_status"] = row["scope_status"]
    for field in ('source_evidence_unavailable_accounts', 'valuation_quality', 'estimated_accounts'):
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
) -> dict[str, Any]:
    """Return the legacy /nav/weekly response shape for frontend compatibility."""
    return {
        "start": start_date.isoformat(),
        "end": terminal_date.isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": "account_anchor_backcast",
        "anchor_date": terminal_date.isoformat(),
        "rows": [normalize_nav_row(row) for row in rows],
    }


class StatementRowsByAccount(dict):
    """Verified cash movements plus accounts with unavailable evidence."""
    def __init__(self):
        super().__init__()
        self.evidence_problems: dict[str, set[str]] = {}

    def unknown(self, account_code: str, reason: str) -> None:
        self.evidence_problems.setdefault(account_code, set()).add(reason)


def _statement_rows_by_account(cur, account_codes: list[str], start_date: Date, terminal_date: Date) -> StatementRowsByAccount:
    """Use only independently verifiable published cash facts for NAV backcasts."""
    result = StatementRowsByAccount()
    if not account_codes:
        return result
    cur.execute("""SELECT to_regclass('public.pams_cashflow_fact') IS NOT NULL AS cashflow_fact_exists,
        to_regclass('public.source_items') IS NOT NULL AS item_exists,
        to_regclass('public.source_batches') IS NOT NULL AS batch_exists""")
    exists = cur.fetchone() or {}
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
            result.unknown(code, str(proof.get('reason') or 'source_evidence_unavailable'))
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
        anchor = account_anchor_amount(account)
        code = str(account.get("account_code") or "")
        rows = rows_by_account.get(code, [])
        problems = getattr(rows_by_account, 'evidence_problems', {}).get(code)
        if problems:
            for sample_date in dates:
                values[sample_date].setdefault('unavailable_accounts', []).append(code)
                values[sample_date].setdefault('source_evidence_unavailable_accounts', []).append(code)
            continue
        if anchor is None:
            anchor_currency = str(account.get("base_currency") or "CNY").upper()
            if not rows:
                for sample_date in dates:
                    values[sample_date].setdefault('unavailable_accounts', []).append(code)
                continue
            has_statement_balance = any(_statement_balance(row) is not None for row in rows)
            if not has_statement_balance:
                for sample_date in dates:
                    values[sample_date].setdefault('unavailable_accounts', []).append(code)
                continue
            anchor_amount = Decimal("0")
            anchor_date = terminal_date
        else:
            anchor_amount, anchor_currency = anchor
            anchor_date = account_anchor_date(account, terminal_date)
        has_statement_rows = bool(rows)
        effective_date = manual_effective_date(account, terminal_date)

        for sample_date in dates:
            # Individual source rows do not prove the statement interval is
            # complete. Back-casts away from the confirmed anchor are estimates.
            if isinstance(rows_by_account, StatementRowsByAccount) and sample_date != anchor_date:
                values[sample_date].setdefault('estimated_accounts', []).append(code)
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
            elif not has_statement_rows and sample_date < effective_date:
                native_value = Decimal("0")
            else:
                reverse_flow = Decimal("0")
                for row in rows:
                    if sample_date < row["transaction_date"] <= terminal_date:
                        row_currency = str(row.get("currency") or anchor_currency).upper()
                        if row_currency == anchor_currency:
                            reverse_flow += _dec(row.get("amount"))
                native_value = anchor_amount - reverse_flow

            signed_native_value = signed_account_native_value(account, native_value)
            rate = fx_rate_at(fx_timelines, anchor_currency, sample_date, base_currency)
            if rate is None:
                values[sample_date].setdefault("unavailable_accounts", []).append(code)
            else:
                add_nav_amount(values[sample_date], signed_native_value * rate)
    return values


def _investment_snapshot_series(
    cur,
    *,
    account_codes: list[str],
    dates: list[Date],
    base_currency: str,
    owner_ratio_case_sql,
) -> dict[Date, Decimal]:
    """Value each account day only after checking immutable complete scope and dated FX."""
    values = ValuationEvidenceSeries({d: Decimal("0") for d in dates})
    if not account_codes or not dates:
        return values
    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    cur.execute(
        f"""
        WITH series_dates AS (SELECT unnest(%s::date[]) AS date),
        {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, account_latest AS (
            SELECT d.date, cas.account_code, MAX(cas.as_of) AS as_of
            FROM series_dates d JOIN complete_account_snapshot cas ON cas.as_of <= d.date
            WHERE cas.account_code = ANY(%s::text[])
            GROUP BY d.date, cas.account_code
        ), h AS (
            SELECT al.date, hs.* FROM account_latest al
            JOIN pams_holdings_snapshot hs ON hs.account_code=al.account_code AND hs.as_of=al.as_of
        ), marked AS (
            SELECT h.date, h.as_of, h.account_code,
                CASE WHEN h.market_value = 0 THEN 0 ELSE
                  (CASE WHEN h.asset_liability='负债' THEN -ABS(h.market_value) ELSE h.market_value END)
                  * (CASE WHEN h.currency=config.base_currency THEN 1
                          WHEN fx.rate IS NOT NULL THEN fx.rate
                          WHEN h.currency='CNY' THEN 1 / NULLIF(base_fx.rate,0)
                          WHEN config.base_currency='CNY' THEN source_fx.rate
                          ELSE source_fx.rate / NULLIF(base_fx.rate,0) END) * {owner_ratio_sql} END AS value_cny
            FROM h CROSS JOIN (SELECT %s::text AS base_currency) config
            LEFT JOIN LATERAL (
                SELECT rate FROM pams_fx_rates_daily fx
                WHERE fx.base_currency=config.base_currency AND fx.quote_currency=h.currency AND fx.as_of=h.date
                  AND fx.rate>0 AND fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(fx.source), '') IS NOT NULL LIMIT 1
            ) fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate FROM pams_fx_rates_daily fx
                WHERE fx.base_currency='CNY' AND fx.quote_currency=h.currency AND fx.as_of=h.date
                  AND fx.rate>0 AND fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(fx.source), '') IS NOT NULL LIMIT 1
            ) source_fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate FROM pams_fx_rates_daily fx
                WHERE fx.base_currency='CNY' AND fx.quote_currency=config.base_currency AND fx.as_of=h.date
                  AND fx.rate>0 AND fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(fx.source), '') IS NOT NULL LIMIT 1
            ) base_fx ON TRUE
        )
        SELECT date,as_of,account_code,SUM(value_cny) AS value_cny,
               COUNT(*) FILTER (WHERE value_cny IS NULL) AS missing_values
        FROM marked GROUP BY date,as_of,account_code ORDER BY date,account_code
        """,
        ([d.isoformat() for d in dates], dates[-1], account_codes, *owner_ratio_params, base_currency),
    )
    rows = [dict(row) for row in cur.fetchall()]
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    pairs = {(str(row["account_code"]), row["as_of"]) for row in rows}
    coverage = load_account_snapshot_coverage(cur, pairs)
    proven: dict[Date, set[str]] = {day: set() for day in dates}
    for row in rows:
        day, code = row["date"], str(row["account_code"])
        # A verified older account snapshot is not proof of today's complete portfolio.
        if (row["as_of"] != day or row["missing_values"] or row["value_cny"] is None
                or coverage.get((code, row["as_of"]), {}).get("status") != "complete"):
            continue
        values[day] += _dec(row["value_cny"])
        proven[day].add(code)
    for day in dates:
        missing = sorted(set(account_codes) - proven[day])
        if missing:
            values.unknown_accounts_by_date[day] = missing
    return values


def _tonghuashun_series_from_trade_fact(
    cur,
    *,
    dates: list[Date],
    account_owner_ratio,
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
               to_regclass('public.source_batches') IS NOT NULL AS batch_exists
        """
    )
    exists = cur.fetchone()
    if not exists or not exists["fact_exists"] or not exists["px_exists"]:
        values.unknown_dates.update(dates)
        return values

    if exists.get("item_exists") and exists.get("batch_exists"):
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
        if row.get("estimated_price"):
            values.estimated_dates.add(row["date"])
    return values


def _tonghuashun_series(cur, *, dates: list[Date], base_currency: str, account_owner_ratio) -> ValuationEvidenceSeries:
    """Convert the CNY reconstruction to the requested currency using daily evidence."""
    values = _tonghuashun_series_from_trade_fact(cur, dates=dates, account_owner_ratio=account_owner_ratio)
    if base_currency.upper() == 'CNY':
        return values
    rates = load_fx_timelines(cur, base_currency)
    for day in dates:
        amount = values.get(day, Decimal(0))
        rate = fx_rate_at(rates, 'CNY', day, base_currency)
        if rate is None:
            if amount != 0:
                values.unknown_dates.add(day)
        else:
            values[day] = amount * rate
    return values


def _overlay_ibkr_daily_closes(cur, *, values: dict, base_currency: str, account_owner_ratio) -> dict:
    from domain.brokers.ibkr import ibkr_daily_close_read as daily_close

    if not daily_close.configured():
        return values
    evidence = daily_close.read_series()
    closes = evidence.get("daily_closes", {})
    proven_days = sorted(day for day, close in closes.items() if close.get("status") == "ok")
    result = {}
    identities: dict[Date, dict[str, Any]] = {}
    rates: dict[Date, dict[str, Any]] = {}
    ratio: Decimal | None = None
    ratio_loaded = False
    for day, legacy in values.items():
        requested = day.isoformat()
        index = bisect_right(proven_days, requested) - 1
        close = closes[proven_days[index]] if index >= 0 else {}
        metadata: dict[str, Any] = {"status": "unconfirmed", "as_of": day.isoformat(),
                                   "requested_as_of": requested, "carried": False,
                                   "reasons": (closes.get(requested) or {}).get("reasons")
                                   or evidence.get("reasons") or ["missing_closing_date"]}
        bucket = None
        if close.get("status") == "ok":
            source_day = Date.fromisoformat(close["as_of"])
            if source_day not in identities:
                identities[source_day] = daily_close_account_identity(cur, source_account_id=close["account_id"], as_of=source_day)
            identity = identities[source_day]
            if identity["status"] != "ok":
                result[day] = {**legacy, "daily_close": {**metadata, "reasons": identity["reasons"],
                                                         "identity_evidence": identity}}
                continue
            if account_owner_ratio is None:
                metadata["reasons"] = ["missing_owner_ratio"]
            else:
                # Resolve this invocation's config once, only after eligible evidence.
                if not ratio_loaded:
                    try:
                        ratio = Decimal(str(account_owner_ratio("ibkr")))
                    except (ValueError, TypeError, InvalidOperation):
                        ratio = None
                    ratio_loaded = True
                if ratio is None:
                    result[day] = {**legacy, "daily_close": {**metadata, "reasons": ["invalid_owner_ratio"]}}
                    continue
                if source_day not in rates:
                    rates[source_day] = daily_close.historical_fx(cur, currency=close["base_currency"],
                                                                 base_currency=base_currency, as_of=source_day)
                fx = rates[source_day]
                rate = fx.get("rate") if fx.get("status") == "ok" else None
                bucket = daily_close_nav_bucket(close, rate=rate, owner_ratio=ratio)
                if bucket is not None and rate is not None:
                    metadata = daily_close_value_metadata(close, rate=rate, owner_ratio=ratio,
                                                          legacy_value=_dec(legacy["net_worth"]), fx_evidence=fx)
                    if legacy.get("history_evidence") == "unavailable":
                        for field in ("legacy_native_value_cny", "native_fx_vs_base_fx_bridge_cny",
                                      "nav_adjustment_cny"):
                            metadata[field] = None
                        metadata["bridge_basis"] = "legacy_history_unavailable"
                    metadata["valuation_currency"] = base_currency
                    metadata["identity_evidence"] = identity
                    metadata.update(requested_as_of=requested, carried=source_day != day,
                                    age_days=(day - source_day).days)
                    if source_day != day:
                        metadata.update(status="last_valid", reasons=(closes.get(requested) or {}).get("reasons")
                                        or ["missing_closing_date"])
                else:
                    metadata["reasons"] = fx.get("reasons") if rate is None else ["missing_gross_nav_proof"]
                    metadata["fx_evidence"] = {k: str(v) if isinstance(v, Decimal) else v for k, v in fx.items()}
        result[day] = {**(bucket if bucket is not None else legacy), "daily_close": metadata}
    return result


def _ibkr_nav_series(cur, *, dates: list[Date], base_currency: str, owner_ratio_case_sql,
                     account_owner_ratio=None) -> dict[Date, dict[str, Any]]:
    """Keep IBKR NAV unavailable unless an independently verified day close proves it."""
    values = {day: {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0"),
                    "history_evidence": "unavailable"} for day in dates}
    return _overlay_ibkr_daily_closes(cur, values=values, base_currency=base_currency,
                                      account_owner_ratio=account_owner_ratio)


def _terminal_daily_close_compatible(
    terminal: dict[str, Any], assembled: dict[str, Any], ibkr_bucket: dict[str, Any],
    account_values: dict[str, Any], *, base_currency: str,
) -> bool:
    """A terminal aggregate must prove it includes the same IBKR valuation."""
    proposed = terminal.get("ibkr_daily_close")
    if not isinstance(proposed, dict) or proposed.get("status") not in {"ok", "last_valid"}:
        return False
    for field in ("source_sha256", "source_account_id", "as_of", "nav_basis", "source_base_currency",
                  "valuation_currency", "fx_evidence"):
        if field not in proposed or field not in assembled or proposed[field] != assembled[field]:
            return False
        if field != "fx_evidence" and (not isinstance(proposed[field], str) or not proposed[field]):
            return False
    fx = proposed["fx_evidence"]
    if (proposed["valuation_currency"] != base_currency
            or proposed["nav_basis"] != "official_base_nav_at_internal_historical_fx"
            or not isinstance(fx, dict) or fx.get("status") != "ok" or not fx.get("source")
            or fx.get("as_of") != proposed["as_of"] or fx.get("currency") != proposed["source_base_currency"]
            or fx.get("base_currency") != base_currency):
        return False
    try:
        with localcontext(Context(prec=160, Emax=512, Emin=-512)):
            for field in ("owner_ratio", "fx_rate"):
                left, right = Decimal(str(proposed[field])), Decimal(str(assembled[field]))
                if (not left.is_finite() or not right.is_finite() or left != right
                        or left < 0 or (field == "fx_rate" and left == 0)):
                    return False
            for field, key in (("assets_cny", "assets"), ("liabilities_cny", "liabilities"),
                               ("net_worth_cny", "net_worth")):
                left, right = Decimal(str(proposed[field])), Decimal(str(ibkr_bucket[key]))
                if not left.is_finite() or not right.is_finite() or left != right:
                    return False
            if "ibkr" in account_values:
                account_value = Decimal(str(account_values["ibkr"]))
                if not account_value.is_finite() or account_value != Decimal(str(ibkr_bucket["net_worth"])):
                    return False
            assets, liabilities, net = (Decimal(str(terminal[key])) for key in ("assets", "liabilities", "net_worth"))
            if not all(value.is_finite() for value in (assets, liabilities, net)):
                return False
            tolerance = Decimal("0.00000001")
            return (assets >= 0 and liabilities >= 0
                    and assets + tolerance >= Decimal(str(ibkr_bucket["assets"]))
                    and liabilities + tolerance >= Decimal(str(ibkr_bucket["liabilities"]))
                    and abs(assets - liabilities - net) <= tolerance)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


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
    snapshot_scope_incomplete_accounts: set[str] | None = None,
    include_ibkr: bool = True,
    interval: str = "weekly",
) -> dict[str, Any]:
    """Return the legacy /nav/weekly payload."""
    terminal_date = terminal_date or ANCHOR_NAV_DATE
    if start_date > terminal_date:
        start_date = terminal_date
    dates = series_dates(start_date, terminal_date, interval)
    overview_accounts = [
        account
        for account in overview_payload.get("accounts", [])
        if account.get("include_in_nav")
    ]
    bank_accounts, investment_snapshot_accounts = select_weekly_nav_accounts(overview_accounts)

    fx_timelines = load_fx_timelines(cur, base_currency)
    rows_by_account = _statement_rows_by_account(
        cur,
        [str(account.get("account_code")) for account in bank_accounts],
        start_date,
        terminal_date,
    )
    bank_series = _bank_backcast_series(
        accounts=bank_accounts,
        rows_by_account=rows_by_account,
        dates=dates,
        terminal_date=terminal_date,
        fx_timelines=fx_timelines,
        base_currency=base_currency,
    )
    snapshot_series = _investment_snapshot_series(
        cur,
        account_codes=investment_snapshot_accounts,
        dates=dates,
        base_currency=base_currency,
        owner_ratio_case_sql=owner_ratio_case_sql,
    )
    a_share_series = (_tonghuashun_series(
        cur,
        dates=dates,
        base_currency=base_currency,
        account_owner_ratio=account_owner_ratio,
    ) if any(str(account.get("account_code") or "") == "tonghuashun" for account in overview_accounts)
       else ValuationEvidenceSeries())
    if any(str(account.get("account_code") or "") == "tonghuashun" for account in overview_accounts):
        # Individual reviewed trades do not prove the opening cash balance or
        # that every transaction was imported. A complete same-day account
        # snapshot can establish the full value without that inference.
        complete_a_share = _investment_snapshot_series(
            cur, account_codes=['tonghuashun'], dates=dates, base_currency=base_currency,
            owner_ratio_case_sql=owner_ratio_case_sql,
        )
        missing_by_day = getattr(complete_a_share, 'unknown_accounts_by_date', None)
        for day in dates:
            if missing_by_day is None or day not in complete_a_share or missing_by_day.get(day):
                a_share_series.unknown_dates.add(day)
            else:
                a_share_series[day] = complete_a_share[day]
                a_share_series.unknown_dates.discard(day)
                a_share_series.estimated_dates.discard(day)
    terminal_account_values = terminal_account_values_cny or {}
    ibkr_values = (_ibkr_nav_series(
        cur,
        dates=dates,
        base_currency=base_currency,
        owner_ratio_case_sql=owner_ratio_case_sql,
        account_owner_ratio=account_owner_ratio,
    ) if include_ibkr else {})
    rows = build_weekly_nav_rows(
        dates=dates,
        bank_series=bank_series,
        snapshot_series=snapshot_series,
        a_share_series=a_share_series,
        ibkr_values=ibkr_values,
    )
    if terminal_nav_bucket:
        for row in rows:
            if row.get("date") == terminal_date:
                # A current portfolio with missing scope or FX cannot replace
                # a known historical row with fabricated zeroes. Check the
                # explicit evidence and all three amounts before any overlay.
                try:
                    if (terminal_nav_bucket.get('scope_status') in {'incomplete','partial','unknown'}
                            or terminal_nav_bucket.get('fx_status') == 'unavailable'
                            or terminal_nav_bucket.get('unavailable_accounts')):
                        raise ValueError('incomplete terminal portfolio')
                    values = [terminal_nav_bucket.get(key) for key in ('assets','liabilities','net_worth')]
                    if any(value is None or isinstance(value,bool) for value in values):
                        raise ValueError('missing terminal amount')
                    terminal_assets, terminal_liabilities, terminal_net = (Decimal(str(value)) for value in values)
                    if (not all(value.is_finite() for value in (terminal_assets,terminal_liabilities,terminal_net))
                            or terminal_assets < 0 or terminal_liabilities < 0
                            or abs(terminal_assets-terminal_liabilities-terminal_net) > Decimal('0.00000001')):
                        raise ValueError('invalid terminal portfolio')
                except (ValueError, TypeError, InvalidOperation):
                    row.update(assets=None, liabilities=None, net_worth=None, scope_status='incomplete')
                    missing = terminal_nav_bucket.get('unavailable_accounts') or []
                    row['unavailable_accounts'] = sorted(set(row.get('unavailable_accounts') or ()) | set(missing))
                    break
                if row.get("unavailable_accounts"):
                    if row.get('source_evidence_unavailable_accounts'):
                        break
                    # The terminal aggregate must include each unavailable account.
                    try:
                        amounts = [Decimal(str(terminal_account_values[account]))
                                   for account in row["unavailable_accounts"]]
                        assets, liabilities, net = (Decimal(str(terminal_nav_bucket[key]))
                                                    for key in ("assets", "liabilities", "net_worth"))
                        proven = (all(value.is_finite() for value in (*amounts, assets, liabilities, net))
                                  and assets >= 0 and liabilities >= 0
                                  and abs(assets - liabilities - net) <= Decimal("0.00000001")
                                  and all(assets >= amount if amount >= 0 else liabilities >= -amount
                                          for amount in amounts))
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        proven = False
                    if not proven:
                        break
                    if "ibkr" in row["unavailable_accounts"]:
                        row["ibkr_history_evidence"] = "current_portfolio_snapshot"
                    row["unavailable_accounts"] = []
                close_proof = row.get("ibkr_daily_close") or {}
                if close_proof.get("status") in {"ok", "last_valid"} and not _terminal_daily_close_compatible(
                    terminal_nav_bucket, close_proof, ibkr_values[terminal_date], terminal_account_values,
                    base_currency=base_currency,
                ):
                    # A portfolio-wide terminal total cannot erase one proven
                    # account component while retaining that component's label.
                    break
                row["assets"] = terminal_assets
                row["liabilities"] = terminal_liabilities
                row["net_worth"] = terminal_net
                break
    for row in rows:
        unresolved = set(snapshot_scope_incomplete_accounts or ())
        close = row.get("ibkr_daily_close") or {}
        if (close.get("status") == "ok" and close.get("as_of") == row["date"].isoformat()):
            unresolved.discard("ibkr")
        if unresolved:
            row.update(assets=None, liabilities=None, net_worth=None, scope_status="incomplete")
            row["unavailable_accounts"] = sorted(set(row.get("unavailable_accounts") or ()) | unresolved)
    return format_weekly_nav_payload(
        start_date=start_date,
        terminal_date=terminal_date,
        base_currency=base_currency,
        rows=rows,
        interval=interval,
    )


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
