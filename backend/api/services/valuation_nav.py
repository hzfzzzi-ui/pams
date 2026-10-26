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

from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE, latest_complete_snapshot_date

ANCHOR_NAV_DATE = Date(2026, 5, 31)
STATEMENT_BACKCAST_TYPES = {"funding", "cash_management", "benefit", "liability"}
INVESTMENT_SNAPSHOT_ACCOUNT_TYPES = {"brokerage", "fund_platform"}
STABLE_ANCHOR_ACCOUNTS = {"huawei_stock"}
INVESTMENT_SNAPSHOT_EXCLUDE = {"tonghuashun", "ibkr", *STABLE_ANCHOR_ACCOUNTS}


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
    return amount * fx_rate_at(fx_timelines, currency, as_of, base_currency)


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
    cur.execute(
        """
        SELECT quote_currency, as_of, rate
        FROM pams_fx_rates_daily
        WHERE base_currency = %s
        ORDER BY quote_currency, as_of
        """,
        (base_currency,),
    )
    timelines: dict[str, list[tuple[Date, Decimal]]] = {}
    for row in cur.fetchall():
        currency = str(row["quote_currency"] or "").upper()
        timelines.setdefault(currency, []).append((row["as_of"], _dec(row["rate"])))
    timelines.setdefault(base_currency.upper(), []).append((identity_date or Date(2026, 5, 31), Decimal("1")))
    return timelines


def fx_rate_at(
    timelines: dict[str, list[tuple[Date, Decimal]]],
    currency: str,
    as_of: Date,
    base_currency: str,
) -> Decimal:
    """Return the latest FX rate on or before a date, falling back to the latest known rate."""
    quote = str(currency or base_currency).upper()
    if quote == str(base_currency).upper():
        return Decimal("1")
    rows = timelines.get(quote) or []
    if not rows:
        return Decimal("1")
    fallback = rows[-1][1]
    for row_date, rate in reversed(rows):
        if row_date <= as_of:
            return rate
    return fallback


def scale_series_to_terminal_anchor(
    series: dict[Date, Decimal],
    *,
    terminal_date: Date,
    target_value: Decimal | None,
) -> dict[Date, Decimal]:
    """Scale a reconstructed curve so the terminal value matches a confirmed account anchor."""
    if target_value is None:
        return series
    terminal_value = series.get(terminal_date, Decimal("0"))
    adjusted = dict(series)
    if abs(terminal_value) <= Decimal("0.0001"):
        adjusted[terminal_date] = target_value
        return adjusted
    scale = target_value / terminal_value
    return {date: value * scale for date, value in adjusted.items()}


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
    """Bind supplemental account identity to the exact native historical snapshot."""
    cur.execute("SELECT to_regclass('public.pams_ibkr_position_history') IS NOT NULL AS exists")
    row = cur.fetchone()
    if not row or not row["exists"]:
        return {"status": "unconfirmed", "reasons": ["missing_native_account_identity"]}
    cur.execute("""SELECT DISTINCT source_account_id FROM pams_ibkr_position_history
        WHERE account_code = %s AND as_of = %s""", ("ibkr", as_of))
    identities = [row["source_account_id"] for row in cur.fetchall()]
    if not identities or any(not isinstance(value, str) or not value.strip() for value in identities):
        return {"status": "unconfirmed", "reasons": ["missing_native_account_identity"]}
    if len(identities) != 1:
        return {"status": "unconfirmed", "reasons": ["ambiguous_native_account_identity"]}
    if identities[0] != source_account_id:
        return {"status": "unconfirmed", "reasons": ["native_account_identity_mismatch"]}
    return {"status": "ok", "basis": "exact_date_native_position_history_account", "as_of": as_of.isoformat()}


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
    bank_series: dict[Date, dict[str, Decimal]],
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
        bucket["assets"] += ibkr_bucket.get("assets", Decimal("0"))
        bucket["liabilities"] += ibkr_bucket.get("liabilities", Decimal("0"))
        bucket["net_worth"] += ibkr_bucket.get("net_worth", Decimal("0"))
        row: dict[str, Any] = {"date": sample_date, **bucket}
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
    represented_accounts = {
        str(row.get("account_code") or "")
        for row in investment_positions
        if row.get("account_code")
    }
    bucket: dict[str, Any] = {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")}
    official = current_daily_close_accounts(account_balances, investment_positions)
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
    represented_accounts = {
        str(row.get("account_code") or "")
        for row in investment_positions
        if row.get("account_code")
    }
    values: dict[str, Decimal] = {}
    official = current_daily_close_accounts(account_balances, investment_positions)
    for row in investment_positions:
        account_code = str(row.get("account_code") or "")
        if not account_code:
            continue
        if account_code in official:
            continue
        values[account_code] = values.get(account_code, Decimal("0")) + _dec(row.get("market_value_cny"))
    for row in account_balances:
        account_code = str(row.get("account_code") or "")
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


def _statement_rows_by_account(cur, account_codes: list[str], start_date: Date, terminal_date: Date) -> dict[str, list[dict[str, Any]]]:
    """Fetch cash movements used to back-cast bank/payment account balances from the anchor."""
    if not account_codes:
        return {}
    cur.execute(
        """
        SELECT to_regclass('public.pams_bank_statement_rows') IS NOT NULL AS bank_rows_exists,
               to_regclass('public.pams_cashflow_fact') IS NOT NULL AS cashflow_fact_exists
        """
    )
    exists = cur.fetchone() or {}
    rows_by_account: dict[str, list[dict[str, Any]]] = {}
    if exists.get("bank_rows_exists"):
        cur.execute(
            """
            SELECT id, source_uid, account_code, transaction_date, amount, currency,
                   category, subcategory, summary, description, counterparty,
                   balance AS statement_balance
            FROM pams_bank_statement_rows
            WHERE account_code = ANY(%s::text[])
              AND (
                  transaction_date >= %s
                  OR (transaction_date < %s AND balance IS NOT NULL)
              )
              AND transaction_date <= %s
            ORDER BY account_code, transaction_date, id
            """,
            (account_codes, start_date, start_date, terminal_date),
        )
        for row in cur.fetchall():
            item = dict(row)
            rows_by_account.setdefault(str(item["account_code"]), []).append(item)

    cashflow_account_codes = account_codes

    if exists.get("cashflow_fact_exists") and cashflow_account_codes:
        seen_source_uids = {
            str(row.get("source_uid"))
            for rows in rows_by_account.values()
            for row in rows
            if row.get("source_uid")
        }
        cur.execute(
            """
            SELECT cashflow_id AS id,
                   source_uid,
                   account_code,
                   event_date AS transaction_date,
                   amount,
                   currency,
                   category,
                   subcategory,
                   summary,
                   description,
                   counterparty,
                   NULLIF(COALESCE(
                       raw_json #>> '{statement_row,balance}',
                       raw_json #>> '{statement_row,raw_json,balance}',
                       raw_json #>> '{balance}'
                   ), '') AS statement_balance
            FROM pams_cashflow_fact
            WHERE account_code = ANY(%s::text[])
              AND (
                  event_date >= %s
                  OR (
                      event_date < %s
                      AND NULLIF(COALESCE(
                          raw_json #>> '{statement_row,balance}',
                          raw_json #>> '{statement_row,raw_json,balance}',
                          raw_json #>> '{balance}'
                      ), '') IS NOT NULL
                  )
              )
              AND event_date <= %s
              AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
            ORDER BY account_code, event_date, cashflow_id
            """,
            (cashflow_account_codes, start_date, start_date, terminal_date),
        )
        for row in cur.fetchall():
            item = dict(row)
            if item.get("source_uid") and str(item.get("source_uid")) in seen_source_uids:
                continue
            rows_by_account.setdefault(str(item["account_code"]), []).append(item)

    if not exists.get("bank_rows_exists") and not exists.get("cashflow_fact_exists"):
        return {}
    return rows_by_account


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
    values = {d: {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")} for d in dates}
    for account in accounts:
        anchor = account_anchor_amount(account)
        code = str(account.get("account_code") or "")
        rows = rows_by_account.get(code, [])
        if anchor is None:
            anchor_currency = str(account.get("base_currency") or "CNY").upper()
            if not rows:
                continue
            has_statement_balance = any(_statement_balance(row) is not None for row in rows)
            if str(account.get("account_type") or "") == "liability" and not has_statement_balance:
                continue
            anchor_amount = Decimal("0")
            anchor_date = terminal_date
        else:
            anchor_amount, anchor_currency = anchor
            anchor_date = account_anchor_date(account, terminal_date)
        has_statement_rows = bool(rows)
        effective_date = manual_effective_date(account, terminal_date)

        for sample_date in dates:
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
    """Return latest-known investment platform values by account on each sample date."""
    values = {d: Decimal("0") for d in dates}
    if not account_codes:
        return values
    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, account_latest AS (
            SELECT d.date, cas.account_code, MAX(cas.as_of) AS as_of
            FROM series_dates d
            JOIN complete_account_snapshot cas ON cas.as_of <= d.date
            WHERE cas.account_code = ANY(%s::text[])
            GROUP BY d.date, cas.account_code
        ), h AS (
            SELECT al.date, hs.*
            FROM account_latest al
            JOIN pams_holdings_snapshot hs
              ON hs.account_code = al.account_code
             AND hs.as_of = al.as_of
        ), marked AS (
            SELECT
                h.date,
                CASE
                    WHEN h.asset_liability = '负债' THEN -ABS(h.market_value)
                    ELSE h.market_value
                END * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql} AS value_cny
            FROM h
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                  AND fx.as_of <= h.date
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = h.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
        )
        SELECT date, COALESCE(SUM(value_cny), 0) AS value_cny
        FROM marked
        GROUP BY date
        ORDER BY date
        """,
        ([d.isoformat() for d in dates], dates[-1], account_codes, *owner_ratio_params, base_currency, base_currency),
    )
    for row in cur.fetchall():
        values[row["date"]] = _dec(row["value_cny"])
    return values


def _tonghuashun_series_from_trade_fact(
    cur,
    *,
    dates: list[Date],
    account_owner_ratio,
) -> dict[Date, Decimal]:
    """Value Tonghuashun A-shares from the current investment trade fact table."""
    values = {d: Decimal("0") for d in dates}
    owner_ratio = account_owner_ratio("tonghuashun")
    cur.execute(
        """
        SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS fact_exists,
               to_regclass('public.pams_ohlcv_daily') IS NOT NULL AS px_exists
        """
    )
    exists = cur.fetchone()
    if not exists or not exists["fact_exists"] or not exists["px_exists"]:
        return values

    cur.execute(
        """
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
                net_cash,
                net_cash_cny,
                COALESCE(fx_rate_to_cny, 1) AS fx_rate_to_cny
            FROM pams_investment_trade_fact
            WHERE account_code = 'tonghuashun'
              AND COALESCE(LOWER(review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
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
                COALESCE(px.close, cm.current_price, 0) AS price
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
                SELECT close
                FROM pams_ohlcv_daily o
                WHERE o.symbol = CASE WHEN LEFT(dp.stock_code, 1) = '6'
                                       THEN 'sh' || dp.stock_code
                                       ELSE 'sz' || dp.stock_code
                                  END
                  AND o.market = 'CN_A'
                  AND o.trade_date <= dp.date
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) px ON TRUE
        ), equity AS (
            SELECT date, SUM(quantity * price) AS value_cny
            FROM priced_positions
            GROUP BY date
        ), cash_events AS (
            SELECT trade_date AS event_date,
                   CASE
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
            SELECT d.date, COALESCE(SUM(e.cash_delta), 0) AS cash_cny
            FROM series_dates d
            LEFT JOIN cash_events e ON e.event_date <= d.date
            GROUP BY d.date
        )
        SELECT d.date,
               (COALESCE(e.value_cny, 0) + COALESCE(c.cash_cny, 0)) AS gross_value_cny
        FROM series_dates d
        LEFT JOIN equity e ON e.date = d.date
        LEFT JOIN cash c ON c.date = d.date
        ORDER BY d.date
        """,
        ([d.isoformat() for d in dates],),
    )
    for row in cur.fetchall():
        values[row["date"]] = _dec(row["gross_value_cny"]) * owner_ratio
    return values


def _tonghuashun_series_from_tradingview(cur, *, dates: list[Date], account_owner_ratio) -> dict[Date, Decimal]:
    """Fallback for databases that still keep the old TradingView raw table."""
    values = {d: Decimal("0") for d in dates}
    owner_ratio = account_owner_ratio("tonghuashun")
    cur.execute(
        """
        SELECT to_regclass('public.pams_tradingview_transactions') IS NOT NULL AS tv_exists,
               to_regclass('public.pams_ohlcv_daily') IS NOT NULL AS px_exists
        """
    )
    exists = cur.fetchone()
    if not exists or not exists["tv_exists"] or not exists["px_exists"]:
        return values

    cur.execute(
        """
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), txn_symbols AS (
            SELECT DISTINCT
                normalized_symbol,
                CASE WHEN LEFT(normalized_symbol, 1) = '6'
                     THEN 'sh' || normalized_symbol
                     ELSE 'sz' || normalized_symbol
                END AS internal_symbol
            FROM pams_tradingview_transactions
            WHERE account_code = 'tonghuashun'
              AND side IN ('Buy', 'Sell')
              AND normalized_symbol IS NOT NULL
        ), trade_events AS (
            SELECT
                closing_time::date AS trade_date,
                normalized_symbol,
                CASE
                    WHEN side = 'Buy' THEN quantity
                    WHEN side = 'Sell' THEN -quantity
                    ELSE 0
                END AS quantity_delta
            FROM pams_tradingview_transactions
            WHERE account_code = 'tonghuashun'
              AND side IN ('Buy', 'Sell')
              AND normalized_symbol IS NOT NULL
        ), daily_positions AS (
            SELECT d.date, e.normalized_symbol, SUM(e.quantity_delta) AS quantity
            FROM series_dates d
            JOIN trade_events e ON e.trade_date <= d.date
            GROUP BY d.date, e.normalized_symbol
            HAVING SUM(e.quantity_delta) <> 0
        ), priced_positions AS (
            SELECT
                dp.date,
                dp.quantity,
                COALESCE(px.close, cm.current_price, 0) AS price
            FROM daily_positions dp
            LEFT JOIN pams_holdings_snapshot cm
              ON cm.account_code = 'tonghuashun'
             AND cm.symbol = dp.normalized_symbol
             AND cm.as_of = (
                 SELECT MAX(as_of)
                 FROM pams_holdings_snapshot
                 WHERE account_code = 'tonghuashun'
                   AND major_category = 'A股'
                   AND minor_category = 'A股'
             )
            LEFT JOIN LATERAL (
                SELECT close
                FROM pams_ohlcv_daily o
                WHERE o.symbol = CASE WHEN LEFT(dp.normalized_symbol, 1) = '6'
                                       THEN 'sh' || dp.normalized_symbol
                                       ELSE 'sz' || dp.normalized_symbol
                                  END
                  AND o.market = 'CN_A'
                  AND o.trade_date <= dp.date
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) px ON TRUE
        ), equity AS (
            SELECT date, SUM(quantity * price) AS value_cny
            FROM priced_positions
            GROUP BY date
        ), cash_events AS (
            SELECT closing_time::date AS event_date,
                   CASE
                       WHEN side = 'Deposit' THEN COALESCE(quantity, 0)
                       WHEN side = 'Buy' THEN -(COALESCE(quantity, 0) * COALESCE(fill_price, 0) + COALESCE(commission, 0))
                       WHEN side = 'Sell' THEN COALESCE(quantity, 0) * COALESCE(fill_price, 0) - COALESCE(commission, 0)
                       WHEN side = 'Dividend' THEN COALESCE(quantity, 0)
                       ELSE 0
                   END AS cash_delta
            FROM pams_tradingview_transactions
            WHERE account_code = 'tonghuashun'
              AND side IN ('Deposit', 'Buy', 'Sell', 'Dividend')
        ), cash AS (
            SELECT d.date, COALESCE(SUM(e.cash_delta), 0) AS cash_cny
            FROM series_dates d
            LEFT JOIN cash_events e ON e.event_date <= d.date
            GROUP BY d.date
        )
        SELECT d.date,
               (COALESCE(e.value_cny, 0) + COALESCE(c.cash_cny, 0)) AS gross_value_cny
        FROM series_dates d
        LEFT JOIN equity e ON e.date = d.date
        LEFT JOIN cash c ON c.date = d.date
        ORDER BY d.date
        """,
        ([d.isoformat() for d in dates],),
    )
    for row in cur.fetchall():
        values[row["date"]] = _dec(row["gross_value_cny"]) * owner_ratio
    return values


def _tonghuashun_series(cur, *, dates: list[Date], base_currency: str, account_owner_ratio) -> dict[Date, Decimal]:
    """Value the A-share account from current trade facts, with legacy fallback."""
    del base_currency
    values = _tonghuashun_series_from_trade_fact(
        cur,
        dates=dates,
        account_owner_ratio=account_owner_ratio,
    )
    if any(abs(value) > Decimal("0.0001") for value in values.values()):
        return values
    return _tonghuashun_series_from_tradingview(
        cur,
        dates=dates,
        account_owner_ratio=account_owner_ratio,
    )


def _overlay_ibkr_daily_closes(cur, *, values: dict, base_currency: str, account_owner_ratio) -> dict:
    from services import ibkr_daily_close_read as daily_close

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
    """Return IBKR gross assets, margin liabilities, and net account value."""
    values = {d: {"assets": Decimal("0"), "liabilities": Decimal("0"), "net_worth": Decimal("0")} for d in dates}
    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("ph")
    cur.execute("SELECT to_regclass('public.pams_ibkr_position_history') IS NOT NULL AS exists")
    if not bool(cur.fetchone()["exists"]):
        return _overlay_ibkr_daily_closes(cur, values=values, base_currency=base_currency,
                                         account_owner_ratio=account_owner_ratio)
    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT unnest(%s::date[]) AS date
        ), latest AS (
            SELECT d.date, MAX(ph.as_of) AS as_of
            FROM series_dates d
            JOIN pams_ibkr_position_history ph ON ph.account_code = 'ibkr' AND ph.as_of <= d.date
            GROUP BY d.date
        ), marked AS (
            SELECT
                l.date,
                ph.asset_liability,
                ABS(ph.market_value) * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql} AS gross_cny,
                CASE
                    WHEN ph.asset_liability = '负债' THEN -ABS(ph.market_value)
                    ELSE ph.market_value
                END * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql} AS value_cny
            FROM latest l
            JOIN pams_ibkr_position_history ph
              ON ph.account_code = 'ibkr'
             AND ph.as_of = l.as_of
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = ph.currency
                  AND fx.as_of <= l.date
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = ph.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
        )
        SELECT date,
               COALESCE(SUM(CASE WHEN asset_liability = '负债' THEN 0 ELSE gross_cny END), 0) AS assets,
               COALESCE(SUM(CASE WHEN asset_liability = '负债' THEN gross_cny ELSE 0 END), 0) AS liabilities,
               COALESCE(SUM(value_cny), 0) AS net_worth
        FROM marked
        GROUP BY date
        ORDER BY date
        """,
        ([d.isoformat() for d in dates], *owner_ratio_params, *owner_ratio_params, base_currency, base_currency),
    )
    for row in cur.fetchall():
        values[row["date"]] = {
            "assets": _dec(row["assets"]),
            "liabilities": _dec(row["liabilities"]),
            "net_worth": _dec(row["net_worth"]),
        }
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
    a_share_series = _tonghuashun_series(
        cur,
        dates=dates,
        base_currency=base_currency,
        account_owner_ratio=account_owner_ratio,
    )
    terminal_account_values = terminal_account_values_cny or {}
    tonghuashun_anchor_cny: Decimal | None
    if "tonghuashun" in terminal_account_values:
        tonghuashun_anchor_cny = _dec(terminal_account_values["tonghuashun"])
    else:
        tonghuashun_anchor_cny = account_anchor_value_cny(
            next((a for a in overview_accounts if str(a.get("account_code") or "") == "tonghuashun"), None),
            fx_timelines=fx_timelines,
            as_of=terminal_date,
            base_currency=base_currency,
        )
    a_share_series = scale_series_to_terminal_anchor(
        a_share_series,
        terminal_date=terminal_date,
        target_value=tonghuashun_anchor_cny,
    )
    ibkr_values = _ibkr_nav_series(
        cur,
        dates=dates,
        base_currency=base_currency,
        owner_ratio_case_sql=owner_ratio_case_sql,
        account_owner_ratio=account_owner_ratio,
    )
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
                close_proof = row.get("ibkr_daily_close") or {}
                if close_proof.get("status") in {"ok", "last_valid"} and not _terminal_daily_close_compatible(
                    terminal_nav_bucket, close_proof, ibkr_values[terminal_date], terminal_account_values,
                    base_currency=base_currency,
                ):
                    # A portfolio-wide terminal total cannot erase one proven
                    # account component while retaining that component's label.
                    break
                row["assets"] = _dec(terminal_nav_bucket.get("assets"))
                row["liabilities"] = _dec(terminal_nav_bucket.get("liabilities"))
                row["net_worth"] = _dec(terminal_nav_bucket.get("net_worth"))
                break
    return format_weekly_nav_payload(
        start_date=start_date,
        terminal_date=terminal_date,
        base_currency=base_currency,
        rows=rows,
        interval=interval,
    )


def build_nav_series_payload(cur, *, limit: int, base_currency: str) -> dict[str, Any]:
    """Return the legacy /nav/series payload from stored NAV rows plus latest holdings."""
    if not _table_exists(cur, "pams_nav_daily"):
        return {
            "rows": [
                {"asOfDate": _norm(row["as_of"]), "netWorth": float(row["total_market_value"] or 0)}
                for row in _fallback_nav_rows_from_holdings(cur, limit=limit, base_currency=base_currency)
            ]
        }

    latest_as_of = latest_complete_snapshot_date(cur, view_as_of=Date.today())

    cur.execute(
        """
        SELECT as_of, total_market_value, holdings_count, venues_count, completeness
        FROM pams_nav_daily
        ORDER BY as_of DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()

    result = [
        {
            "asOfDate": _norm(item["as_of"]),
            "netWorth": float(item["total_market_value"] or 0),
        }
        for item in reversed(list(rows))
    ]

    if latest_as_of and (not result or result[-1]["asOfDate"] != latest_as_of.isoformat()):
        cur.execute(
            """
            WITH h AS (
                SELECT * FROM pams_holdings_snapshot WHERE as_of = %s
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = %s
                  AND as_of <= %s
                ORDER BY quote_currency, as_of DESC
            )
            SELECT COALESCE(SUM(h.market_value * fx.rate), 0) AS total_mv
            FROM h JOIN fx ON fx.quote_currency = h.currency
            """,
            (latest_as_of, base_currency, latest_as_of),
        )
        row = cur.fetchone()
        total_mv = float(row["total_mv"] or 0) if row else 0.0
        result.append({"asOfDate": latest_as_of.isoformat(), "netWorth": total_mv})

    return {"rows": result}


def build_nav_drawdown_payload(cur, *, limit: int) -> dict[str, Any]:
    """Return the legacy /nav/drawdown payload from stored NAV daily rows."""
    if not _table_exists(cur, "pams_nav_daily"):
        return _nav_drawdown_from_rows(
            _fallback_nav_rows_from_holdings(cur, limit=limit, base_currency="CNY")
        )

    cur.execute(
        """
        WITH r AS (
            SELECT as_of, total_market_value, completeness
            FROM pams_nav_daily ORDER BY as_of DESC LIMIT %s
        ), x AS (
            SELECT * FROM r ORDER BY as_of
        ), s AS (
            SELECT as_of, total_market_value, completeness,
                   MAX(total_market_value) OVER (ORDER BY as_of) AS peak
            FROM x
        )
        SELECT as_of, total_market_value, completeness, peak,
               CASE WHEN peak=0 THEN 0 ELSE (total_market_value/peak - 1) END AS drawdown
        FROM s ORDER BY as_of
        """,
        (limit,),
    )
    rows = cur.fetchall()

    max_drawdown = None
    for row in rows:
        try:
            drawdown = float(row["drawdown"])
            max_drawdown = drawdown if max_drawdown is None else min(max_drawdown, drawdown)
        except Exception:
            continue

    return {
        "max_drawdown": None if max_drawdown is None else str(max_drawdown),
        "rows": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone() or {}
    return bool(row.get("exists"))


def _fallback_nav_rows_from_holdings(cur, *, limit: int, base_currency: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        WITH sample_dates AS (
            SELECT DISTINCT as_of
            FROM pams_holdings_snapshot
            ORDER BY as_of DESC
            LIMIT %s
        )
        SELECT
            d.as_of,
            COALESCE(SUM(h.market_value * COALESCE(fx.rate, 1)), 0) AS total_market_value,
            COUNT(*) AS holdings_count,
            COUNT(DISTINCT h.account_code) AS venues_count,
            1::numeric AS completeness
        FROM sample_dates d
        JOIN pams_holdings_snapshot h
          ON h.as_of = d.as_of
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
              AND quote_currency = h.currency
              AND as_of <= d.as_of
            ORDER BY as_of DESC
            LIMIT 1
        ) fx ON true
        GROUP BY d.as_of
        ORDER BY d.as_of
        """,
        (limit, base_currency),
    )
    return [dict(row) for row in cur.fetchall()]


def _nav_drawdown_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    peak: Decimal | None = None
    max_drawdown: Decimal | None = None
    for row in rows:
        value = _dec(row.get("total_market_value"))
        peak = value if peak is None else max(peak, value)
        drawdown = Decimal("0") if not peak else (value / peak - Decimal("1"))
        max_drawdown = drawdown if max_drawdown is None else min(max_drawdown, drawdown)
        out.append(
            {
                "as_of": row.get("as_of"),
                "total_market_value": value,
                "completeness": row.get("completeness", Decimal("1")),
                "peak": peak,
                "drawdown": drawdown,
            }
        )

    return {
        "max_drawdown": None if max_drawdown is None else str(max_drawdown),
        "rows": [{key: _norm(value) for key, value in dict(row).items()} for row in out],
    }
