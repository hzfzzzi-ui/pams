"""Investment return calculation helpers.

This service owns return formulas and endpoint payload assembly. SQL reads,
raw source normalization, and IBKR price reconstruction live in focused helper
modules.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date
from decimal import Decimal
from typing import Any

from services.investment_return_math import (
    annualized_rate,
    apply_reported_display_anchor,
    bucket_flow_events as _bucket_flow_events,
    cash_flow_adjusted_return_rows as _cash_flow_adjusted_return_rows,
    dec,
    derived_cumulative_anchor,
    norm,
    row_index_on_or_after,
    section_return_summary,
)
from services.valuation_nav import (
    account_anchor_amount,
    account_anchor_value_cny,
    load_fx_timelines,
    scale_series_to_terminal_anchor,
    series_dates,
    _tonghuashun_series as tonghuashun_series,
)
from services.investment_return_data import (
    INVESTMENT_RETURN_CATEGORIES,
    INVESTMENT_RETURN_SNAPSHOT_EXCLUDE,
    SECTION_RETURN_CONFIG,
    _apply_gold_proxy_to_precious_values,
    _exact_snapshot_category_value,
    _ibkr_trade_flow_events,
    _investment_transaction_flow_events,
    _reported_account_return_adjustments,
    _section_first_activity_date,
    _snapshot_category_value_series,
    _tonghuashun_cost_series,
    _tonghuashun_external_flow_events,
)
from services.holdings_view import build_holdings_by_category_payload
from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from services.investment_return_ibkr import (
    _close_on_or_before,
    _fetch_yfinance_closes,
    _ibkr_full_trade_return_series,
    _ibkr_yfinance_symbol,
)


def _reported_section_performance(section: str, reported_returns_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Return configured official performance anchors for a section when available."""
    account_perf = reported_returns_cfg.get("account_performance") or {}
    sections = reported_returns_cfg.get("sections") or {}
    if section == "a_stock":
        item = account_perf.get("tonghuashun")
        # A-share stock performance is anchored to the Tonghuashun cash ledger:
        # the account is jointly funded, but the personal contribution base is
        # confirmed at CNY 250,000.  Historical platform performance screenshots
        # are kept as audit notes only unless explicitly enabled.
        if isinstance(item, dict) and item.get("use_reported_anchor"):
            return item
        return None
    if section == "a_fund":
        total = Decimal("0")
        has_total = False
        fund_section = sections.get("a_share_fund")
        if isinstance(fund_section, dict) and fund_section.get("total_ytd_pnl") is not None:
            total += Decimal(str(fund_section["total_ytd_pnl"]))
            has_total = True
        tencent = account_perf.get("tencent_licaitong")
        if isinstance(tencent, dict) and tencent.get("total_ytd_pnl") is not None:
            total += Decimal(str(tencent["total_ytd_pnl"]))
            has_total = True
        if has_total:
            return {
                "total_return": total,
                "basis": "建行基金收益中心 + 腾讯理财通官方收益口径",
            }
    if section == "precious_metal":
        metals = sections.get("precious_metals")
        if isinstance(metals, dict) and metals.get("total_ytd_pnl") is not None:
            return {
                "total_return": Decimal(str(metals["total_ytd_pnl"])),
                "basis": metals.get("summary_note") or "建行贵金属收益中心口径",
            }
    return None


def _row_date(row: dict[str, Any]) -> Date:
    value = row.get("date")
    if isinstance(value, Date):
        return value
    return Date.fromisoformat(str(value))


def _date_key(value: Any | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Date):
        return value.isoformat()[:10]
    if hasattr(value, "date") and not isinstance(value, str):
        try:
            return value.date().isoformat()
        except Exception:
            pass
    text = str(value)
    return text[:10] if text else None


def _section_return_amount(row: dict[str, Any] | None) -> Decimal:
    if not row:
        return Decimal("0")
    return dec(row.get("market_value")) - dec(row.get("cumulative_capital"))


def _total_return_amount(row: dict[str, Any] | None) -> Decimal:
    if not row:
        return Decimal("0")
    return dec(row.get("total_pnl_cny", row.get("total_pnl")))


def _market_value_amount(row: dict[str, Any] | None) -> Decimal:
    if not row:
        return Decimal("0")
    return dec(row.get("market_value_cny", row.get("market_value")))


def _reported_total_return(
    reported: dict[str, Any] | None,
    owner_ratio_lookup: Callable[[str], Decimal] | None,
) -> Decimal | None:
    """Return the configured official total return after owner-ratio scaling."""
    if not reported:
        return None
    value: Decimal | None = None
    if reported.get("total_return") is not None:
        value = Decimal(str(reported["total_return"]))
    elif reported.get("total_return_gross") is not None:
        value = Decimal(str(reported["total_return_gross"]))
    elif reported.get("total_ytd_pnl") is not None:
        value = Decimal(str(reported["total_ytd_pnl"]))
    if value is None:
        return None
    account_code = str(reported.get("account_code") or "")
    owner_ratio = owner_ratio_lookup(account_code) if owner_ratio_lookup else Decimal("1")
    return value * owner_ratio


def _account_net_liquidation_anchor_cny(
    cur,
    *,
    account_code: str,
    overview_payload: dict[str, Any],
    account_owner_ratio: Callable[[str], Decimal],
    fx_timelines: dict[str, list[tuple[Date, Decimal]]],
    as_of: Date,
    base_currency: str,
) -> Decimal | None:
    """Return current account net liquidation, including brokerage cash."""
    account = next(
        (a for a in overview_payload.get("accounts", []) if str(a.get("account_code") or "") == account_code),
        None,
    )
    primary_balance = account.get("primary_balance") if account else None
    if isinstance(primary_balance, dict) and primary_balance.get("gross_amount") is not None and primary_balance.get("as_of"):
        payload = build_account_performance_payload(
            cur,
            account_code=account_code,
            owner_ratio=account_owner_ratio(account_code),
            initial_capital=None,
            net_liquidation_gross=Decimal(str(primary_balance.get("gross_amount"))),
            net_liquidation=(
                Decimal(str(primary_balance.get("amount")))
                if primary_balance.get("amount") is not None
                else None
            ),
            net_liquidation_as_of=primary_balance.get("as_of"),
        )
        if payload and payload.get("net_liquidation_cny") not in (None, ""):
            return dec(payload.get("net_liquidation_cny"))
    return account_anchor_value_cny(
        account,
        fx_timelines=fx_timelines,
        as_of=as_of,
        base_currency=base_currency,
    )


def _compound_reported_summary(
    *,
    rows: list[dict[str, Any]],
    reported: dict[str, Any] | None,
    reported_as_of: Date | None,
    terminal_date: Date,
    owner_ratio_lookup: Callable[[str], Decimal] | None,
) -> dict[str, Any] | None:
    """Carry a stale official total-return anchor forward with local PnL deltas."""
    if not rows or not reported or not reported_as_of or reported_as_of >= terminal_date:
        return None
    official_total = _reported_total_return(reported, owner_ratio_lookup)
    if official_total is None:
        return None
    anchor_idx = row_index_on_or_after(rows, reported_as_of)
    if anchor_idx is None:
        return None

    anchor_raw_return = _section_return_amount(rows[anchor_idx])
    latest = rows[-1]
    local_delta = _section_return_amount(latest) - anchor_raw_return
    total_return = official_total + local_delta

    capital = _market_value_amount(latest) - total_return
    if capital <= Decimal("0.0001"):
        capital = dec(latest.get("cumulative_capital"))
    total_return_rate = total_return / capital if capital > Decimal("0.0001") else dec(latest.get("display_return_rate"))
    annualized = annualized_rate(total_return_rate, rows[0]["date"], latest["date"])
    return {
        "total_return": total_return,
        "total_return_rate": total_return_rate,
        "annualized_return_rate": annualized if annualized is not None else Decimal("0"),
    }


def _overlay_exact_snapshot_values(
    cur,
    values: dict[Date, Decimal],
    *,
    dates: list[Date],
    base_currency: str,
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    account_codes: list[str],
) -> dict[Date, Decimal]:
    """Use exact same-day account snapshots when they exist, keeping reconstructed gaps."""
    adjusted = dict(values)
    if not dates or not account_codes:
        return adjusted
    owner_ratio_sql, owner_ratio_params = owner_ratio_case_sql("h")
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT h.as_of, COALESCE(SUM(
            h.market_value * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {owner_ratio_sql}
        ), 0) AS value_cny,
        COUNT(*) AS row_count
        FROM pams_holdings_snapshot h
        JOIN complete_account_snapshot cas
          ON cas.account_code = h.account_code
         AND cas.as_of = h.as_of
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %s
              AND fx.quote_currency = h.currency
              AND fx.as_of <= h.as_of
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
        WHERE h.as_of = ANY(%s::date[])
          AND h.account_code = ANY(%s::text[])
          AND h.asset_liability = %s
        GROUP BY h.as_of
        """,
        (
            dates[-1],
            *owner_ratio_params,
            base_currency,
            base_currency,
            [d.isoformat() for d in dates],
            account_codes,
            "\u8d44\u4ea7",
        ),
    )
    for row in cur.fetchall():
        if int(row.get("row_count") or 0) > 0:
            adjusted[row["as_of"]] = dec(row.get("value_cny"))
    return adjusted


def _overlay_terminal_exact_section_value(
    cur,
    values: dict[Date, Decimal],
    *,
    dates: list[Date],
    section: str,
    base_currency: str,
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    excluded_holding_accounts: set[str],
    account_codes: list[str] | None = None,
) -> dict[Date, Decimal]:
    """Use the current holding fact table for the terminal sample only.

    The return curves can reconstruct historical gaps from trades or vendor
    prices, but the current point must match the same holding snapshot used by
    the holding, lookthrough, risk, and config pages.
    """
    adjusted = dict(values)
    if not dates or section not in SECTION_RETURN_CONFIG:
        return adjusted
    cfg = SECTION_RETURN_CONFIG[section]
    exact = _exact_snapshot_category_value(
        cur,
        as_of=dates[-1],
        base_currency=base_currency,
        major_category=str(cfg["major_category"]),
        minor_category=cfg.get("minor_category"),
        owner_ratio_case_sql=owner_ratio_case_sql,
        excluded_holding_accounts=excluded_holding_accounts,
        account_codes=account_codes,
    )
    if exact is not None:
        adjusted[dates[-1]] = exact
    return adjusted


def _today_window_payload(
    rows: list[dict[str, Any]],
    *,
    label_date: Date | None,
    return_amount,
) -> dict[str, Any] | None:
    """Return the active 15:05-to-15:05 daily return bucket.

    The visible "today" bucket remains on the latest A-share close date until
    the next 15:05 Beijing refresh. Manual or automatic snapshots created within
    that window are compared against the row before the bucket label date, so
    they add to the active day's return instead of resetting it to zero.
    """
    if not rows:
        return None

    label = label_date or _row_date(rows[-1])
    latest = rows[-1]
    latest_date = _row_date(latest)

    if label_date is not None and latest_date < label:
        return {
            "label_date": label.isoformat(),
            "baseline_date": latest_date.isoformat(),
            "latest_date": latest_date.isoformat(),
            "total_return": norm(Decimal("0")),
            "return_rate": norm(Decimal("0")),
        }

    label_index: int | None = None
    for idx, row in enumerate(rows):
        if _row_date(row) <= label:
            label_index = idx

    if label_index is None:
        baseline = rows[-2] if len(rows) > 1 else None
    elif label_index > 0:
        baseline = rows[label_index - 1]
    else:
        baseline = None

    amount = return_amount(latest) - return_amount(baseline)
    capital = abs(_market_value_amount(baseline))
    if capital <= Decimal("0.0001"):
        capital = abs(_market_value_amount(latest))
    return {
        "label_date": label.isoformat(),
        "baseline_date": _row_date(baseline).isoformat() if baseline else None,
        "latest_date": _row_date(latest).isoformat(),
        "total_return": norm(amount),
        "return_rate": norm(amount / capital if capital > Decimal("0.0001") else Decimal("0")),
    }


def _ibkr_holdings_today_window_payload(
    cur,
    *,
    base_currency: str,
    label_date: Date | None,
    view_as_of: Date,
) -> dict[str, Any] | None:
    """Return IBKR today's PnL from latest holdings prices instead of the return curve.

    IBKR Flex position history can lag same-day trades by one snapshot. Around
    sells, the return curve may see a position drop and a trade cash-flow on
    different sample dates. The visible "today" PnL should stay on the holdings
    daily-price/FX basis used by the positions table.
    """
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, latest AS (
            SELECT MAX(pams_holdings_snapshot.as_of) AS as_of
            FROM pams_holdings_snapshot
            JOIN complete_account_snapshot cas
              ON cas.account_code = pams_holdings_snapshot.account_code
             AND cas.as_of = pams_holdings_snapshot.as_of
            WHERE pams_holdings_snapshot.account_code = 'ibkr'
              AND pams_holdings_snapshot.asset_liability = %s
              AND pams_holdings_snapshot.major_category = %s
        ), previous_snapshot AS (
            SELECT MAX(hs.as_of) AS as_of
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
            JOIN latest l ON hs.as_of < l.as_of
            WHERE hs.account_code = 'ibkr'
              AND hs.asset_liability = %s
              AND hs.major_category = %s
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = %s
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            l.as_of AS latest_date,
            ps.as_of AS baseline_date,
            COALESCE(SUM(
                CASE
                    WHEN h.quantity IS NOT NULL
                     AND eff.current_price IS NOT NULL
                     AND eff.previous_price IS NOT NULL
                    THEN (eff.current_price - eff.previous_price) * h.quantity * COALESCE(fx.rate, 1)
                    ELSE 0
                END
            ), 0) AS daily_price_pnl_cny,
            COALESCE(SUM(
                CASE
                    WHEN h.quantity IS NOT NULL
                     AND eff.previous_price IS NOT NULL
                    THEN eff.previous_price * h.quantity * (COALESCE(fx.rate, 1) - COALESCE(prev_overseas_fx.rate, fx.rate, 1))
                    ELSE 0
                END
            ), 0) AS daily_fx_pnl_cny,
            COALESCE(SUM(
                CASE
                    WHEN h.quantity IS NOT NULL
                     AND eff.previous_price IS NOT NULL
                    THEN eff.previous_price * h.quantity * COALESCE(prev_overseas_fx.rate, fx.rate, 1)
                    ELSE 0
                END
            ), 0) AS baseline_value_cny
        FROM latest l
        LEFT JOIN previous_snapshot ps ON TRUE
        JOIN pams_holdings_snapshot h
          ON h.account_code = 'ibkr'
         AND h.as_of = l.as_of
         AND h.asset_liability = %s
         AND h.major_category = %s
        LEFT JOIN fx ON fx.quote_currency = h.currency
        LEFT JOIN LATERAL (
            SELECT o.trade_date, o.close
            FROM pams_ohlcv_daily o
            WHERE o.market = 'YF'
              AND o.symbol = h.symbol
              AND o.trade_date <= h.as_of
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) curr_overseas_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT o.trade_date, o.close
            FROM pams_ohlcv_daily o
            WHERE o.market = 'YF'
              AND o.symbol = h.symbol
              AND o.trade_date < curr_overseas_px.trade_date
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) prev_overseas_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT hs.current_price
            FROM pams_holdings_snapshot hs
            WHERE hs.account_code = h.account_code
              AND hs.as_of < h.as_of
              AND hs.current_price IS NOT NULL
              AND (
                  (COALESCE(h.symbol, '') <> '' AND hs.symbol = h.symbol)
                  OR (COALESCE(h.symbol, '') = '' AND hs.name = h.name)
              )
            ORDER BY hs.as_of DESC
            LIMIT 1
        ) prev_snap ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                CASE
                    WHEN h.current_price IS NOT NULL
                     AND curr_overseas_px.close IS NOT NULL
                     AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                    THEN curr_overseas_px.close
                    ELSE COALESCE(prev_overseas_px.close, prev_snap.current_price)
                END AS previous_price,
                CASE
                    WHEN h.current_price IS NOT NULL
                     AND curr_overseas_px.close IS NOT NULL
                     AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                    THEN curr_overseas_px.trade_date
                    ELSE COALESCE(prev_overseas_px.trade_date, h.as_of - 1)
                END AS previous_as_of
        ) prev_basis ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily prev_fx
            WHERE prev_fx.base_currency = %s
              AND prev_fx.quote_currency = h.currency
              AND prev_basis.previous_as_of IS NOT NULL
              AND prev_fx.as_of <= prev_basis.previous_as_of
            ORDER BY prev_fx.as_of DESC
            LIMIT 1
        ) prev_overseas_fx ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                h.current_price AS current_price,
                prev_basis.previous_price AS previous_price
        ) eff ON TRUE
        GROUP BY l.as_of, ps.as_of
        """,
        (
            view_as_of,
            "\u8d44\u4ea7",
            "\u7f8e\u80a1",
            "\u8d44\u4ea7",
            "\u7f8e\u80a1",
            base_currency,
            "\u8d44\u4ea7",
            "\u7f8e\u80a1",
            base_currency,
        ),
    )
    row = cur.fetchone()
    if not row or row.get("latest_date") is None:
        return None

    latest_date = row["latest_date"]
    if label_date is not None and latest_date < label_date:
        return {
            "label_date": label_date.isoformat(),
            "baseline_date": latest_date.isoformat(),
            "latest_date": latest_date.isoformat(),
            "total_return": norm(Decimal("0")),
            "return_rate": norm(Decimal("0")),
        }

    amount = dec(row.get("daily_price_pnl_cny")) + dec(row.get("daily_fx_pnl_cny"))
    capital = abs(dec(row.get("baseline_value_cny")))
    return {
        "label_date": (label_date or latest_date).isoformat(),
        "baseline_date": row["baseline_date"].isoformat() if row.get("baseline_date") else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(amount),
        "return_rate": norm(amount / capital if capital > Decimal("0.0001") else Decimal("0")),
    }


def _a_stock_holdings_today_window_payload(
    cur,
    *,
    view_as_of: Date,
    excluded_holding_accounts: set[str],
    account_owner_ratio: Callable[[str], Decimal],
    label_date: Date | None,
    overview_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return A-share today's PnL using account net liquidation when available."""
    owner_ratio = account_owner_ratio("tonghuashun")
    account = next(
        (
            a
            for a in (overview_payload or {}).get("accounts", [])
            if str(a.get("account_code") or "") == "tonghuashun"
        ),
        None,
    )
    primary_balance = account.get("primary_balance") if account else None
    if isinstance(primary_balance, dict) and primary_balance.get("gross_amount") is not None and primary_balance.get("as_of"):
        account_payload = build_account_performance_payload(
            cur,
            account_code="tonghuashun",
            owner_ratio=owner_ratio,
            initial_capital=None,
            net_liquidation_gross=Decimal(str(primary_balance.get("gross_amount"))),
            net_liquidation=(
                Decimal(str(primary_balance.get("amount")))
                if primary_balance.get("amount") is not None
                else None
            ),
            net_liquidation_as_of=primary_balance.get("as_of"),
            daily_pnl_label_date=label_date,
        )
        if account_payload and account_payload.get("daily_pnl_cny") not in (None, ""):
            latest_raw = account_payload.get("as_of")
            try:
                latest_date = Date.fromisoformat(str(latest_raw)[:10])
            except (TypeError, ValueError):
                latest_date = view_as_of
            amount = dec(account_payload.get("daily_pnl_cny"))
            net_value = dec(account_payload.get("net_liquidation_cny"))
            baseline_value = net_value - amount
            cur.execute(
                """
                SELECT MAX(as_of) AS baseline_date
                FROM pams_holdings_snapshot
                WHERE account_code = 'tonghuashun'
                  AND as_of < %s
                """,
                (latest_date,),
            )
            baseline = cur.fetchone()
            baseline_date = baseline.get("baseline_date") if baseline else None
            return {
                "label_date": (label_date or latest_date).isoformat(),
                "baseline_date": baseline_date.isoformat() if baseline_date else None,
                "latest_date": latest_date.isoformat(),
                "total_return": norm(amount),
                "return_rate": norm(amount / abs(baseline_value) if abs(baseline_value) > Decimal("0.0001") else Decimal("0")),
            }

    payload = build_holdings_by_category_payload(
        cur,
        category="A\u80a1",
        limit=1000,
        minor_category="A\u80a1",
        include_closed=False,
        view_as_of=view_as_of,
        excluded_accounts=excluded_holding_accounts,
        owner_ratios={"tonghuashun": owner_ratio},
        default_owner_ratio=Decimal("1"),
    )
    rows = [row for row in payload.get("rows", []) if not row.get("is_closed")]
    if not rows:
        return None

    amount = sum(
        dec(row.get("daily_total_pnl_cny", row.get("daily_pnl_cny")))
        for row in rows
    )
    market_value = sum(abs(dec(row.get("market_value_cny"))) for row in rows)
    latest_dates: list[Date] = []
    for row in rows:
        raw = row.get("as_of") or row.get("snapshot_as_of")
        try:
            latest_dates.append(Date.fromisoformat(str(raw)))
        except (TypeError, ValueError):
            pass
    latest_date = max(latest_dates) if latest_dates else view_as_of
    if label_date is not None and latest_date < label_date:
        return {
            "label_date": label_date.isoformat(),
            "baseline_date": latest_date.isoformat(),
            "latest_date": latest_date.isoformat(),
            "total_return": norm(Decimal("0")),
            "return_rate": norm(Decimal("0")),
        }

    cur.execute(
        """
        SELECT MAX(as_of) AS baseline_date
        FROM pams_holdings_snapshot
        WHERE account_code = 'tonghuashun'
          AND asset_liability = %s
          AND major_category = %s
          AND minor_category = %s
          AND as_of < %s
        """,
        ("\u8d44\u4ea7", "A\u80a1", "A\u80a1", latest_date),
    )
    baseline = cur.fetchone()
    baseline_date = baseline.get("baseline_date") if baseline else None
    return {
        "label_date": (label_date or latest_date).isoformat(),
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(amount),
        "return_rate": norm(amount / market_value if market_value > Decimal("0.0001") else Decimal("0")),
    }


def _holdings_category_today_window_payload(
    cur,
    *,
    category: str,
    minor_category: str | None,
    view_as_of: Date,
    excluded_holding_accounts: set[str],
    account_owner_ratio: Callable[[str], Decimal],
    label_date: Date | None,
) -> dict[str, Any] | None:
    """Return today's PnL from the same holdings rows used by the holdings page."""
    cur.execute(
        """
        SELECT DISTINCT account_code
        FROM pams_holdings_snapshot
        WHERE major_category = %s
          AND (%s IS NULL OR minor_category = %s)
          AND as_of <= %s
        """,
        (category, minor_category, minor_category, view_as_of),
    )
    owner_ratios = {
        str(row["account_code"]): account_owner_ratio(str(row["account_code"]))
        for row in cur.fetchall()
        if row.get("account_code")
    }
    payload = build_holdings_by_category_payload(
        cur,
        category=category,
        limit=5000,
        minor_category=minor_category,
        include_closed=False,
        view_as_of=view_as_of,
        excluded_accounts=excluded_holding_accounts,
        owner_ratios=owner_ratios,
        default_owner_ratio=Decimal("1"),
        daily_pnl_start_as_of=label_date,
    )
    rows = [row for row in payload.get("rows", []) if not row.get("is_closed")]
    if not rows:
        return None

    amount = sum(
        dec(row.get("daily_total_pnl_cny", row.get("daily_pnl_cny", row.get("daily_pnl"))))
        for row in rows
    )
    market_value = sum(abs(dec(row.get("market_value_cny", row.get("market_value")))) for row in rows)
    latest_dates: list[Date] = []
    for row in rows:
        raw = row.get("as_of") or row.get("snapshot_as_of")
        try:
            latest_dates.append(Date.fromisoformat(str(raw)))
        except (TypeError, ValueError):
            pass
    latest_date = max(latest_dates) if latest_dates else view_as_of
    if label_date is not None and latest_date < label_date:
        return {
            "label_date": label_date.isoformat(),
            "baseline_date": latest_date.isoformat(),
            "latest_date": latest_date.isoformat(),
            "total_return": norm(Decimal("0")),
            "return_rate": norm(Decimal("0")),
        }

    cur.execute(
        """
        SELECT MAX(as_of) AS baseline_date
        FROM pams_holdings_snapshot
        WHERE major_category = %s
          AND (%s IS NULL OR minor_category = %s)
          AND as_of < %s
        """,
        (category, minor_category, minor_category, latest_date),
    )
    baseline = cur.fetchone()
    baseline_date = baseline.get("baseline_date") if baseline else None
    return {
        "label_date": (label_date or latest_date).isoformat(),
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(amount),
        "return_rate": norm(amount / market_value if market_value > Decimal("0.0001") else Decimal("0")),
    }


def _reported_as_of_date(reported: dict[str, Any] | None, reported_returns_cfg: dict[str, Any]) -> Date | None:
    """Return a configured reported-as-of date when it is available."""
    raw = (reported or {}).get("as_of") or reported_returns_cfg.get("as_of")
    if not raw:
        return None
    try:
        return Date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _reported_adjusted_section_amounts(
    rows: list[dict[str, Any]],
    *,
    reported: dict[str, Any] | None,
    reported_as_of: Date | None,
    owner_ratio_lookup: Callable[[str], Decimal] | None,
) -> dict[Date, Decimal]:
    """Return section return amounts, carrying official anchors forward when present."""
    amounts = {_row_date(row): _section_return_amount(row) for row in rows}
    if not rows or not reported or reported_as_of is None:
        return amounts

    official_total = _reported_total_return(reported, owner_ratio_lookup)
    if official_total is None:
        return amounts
    anchor_idx = row_index_on_or_after(rows, reported_as_of)
    if anchor_idx is None:
        return amounts

    anchor_date = _row_date(rows[anchor_idx])
    anchor_raw = _section_return_amount(rows[anchor_idx])
    for row in rows:
        sample_date = _row_date(row)
        if sample_date >= anchor_date:
            amounts[sample_date] = official_total + _section_return_amount(row) - anchor_raw
    return amounts


def _amount_on_or_before(amounts: dict[Date, Decimal], sample_date: Date) -> Decimal:
    """Return the latest available amount on or before a sampled total-return date."""
    for amount_date in sorted(amounts.keys(), reverse=True):
        if amount_date <= sample_date:
            return amounts[amount_date]
    return Decimal("0")


def _total_section_return_amounts(
    cur,
    *,
    dates: list[Date],
    interval: str,
    base_currency: str,
    view_as_of: Date,
    reported_returns_cfg: dict[str, Any],
    overview_payload: dict[str, Any],
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    account_owner_ratio: Callable[[str], Decimal],
    excluded_holding_accounts: set[str],
) -> dict[Date, Decimal] | None:
    """Compose total return from the same section return amounts shown in the UI."""
    if not dates:
        return None

    section_amounts: dict[str, dict[Date, Decimal]] = {}
    for section in SECTION_RETURN_CONFIG:
        payload = build_section_return_series_payload(
            cur,
            section=section,
            start_date=dates[0],
            interval=interval,
            base_currency=base_currency,
            view_as_of=view_as_of,
            reported_returns_cfg=reported_returns_cfg,
            overview_payload=overview_payload,
            owner_ratio_case_sql=owner_ratio_case_sql,
            account_owner_ratio=account_owner_ratio,
            excluded_holding_accounts=excluded_holding_accounts,
            today_label_date=None,
        )
        rows = [
            {
                "date": Date.fromisoformat(str(row["date"])),
                "market_value": dec(row.get("market_value")),
                "cumulative_capital": dec(row.get("cumulative_capital")),
            }
            for row in payload.get("rows", [])
            if row.get("date") is not None
        ]
        reported = _reported_section_performance(section, reported_returns_cfg)
        reported_as_of = _reported_as_of_date(reported, reported_returns_cfg)
        section_amounts[section] = _reported_adjusted_section_amounts(
            rows,
            reported=reported,
            reported_as_of=reported_as_of,
            owner_ratio_lookup=account_owner_ratio,
        )

    return {
        sample_date: sum(
            _amount_on_or_before(amounts, sample_date)
            for amounts in section_amounts.values()
        )
        for sample_date in dates
    }


def _total_section_today_window_payload(
    cur,
    *,
    dates: list[Date],
    interval: str,
    base_currency: str,
    view_as_of: Date,
    reported_returns_cfg: dict[str, Any],
    overview_payload: dict[str, Any],
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    account_owner_ratio: Callable[[str], Decimal],
    excluded_holding_accounts: set[str],
    today_label_date: Date | None,
) -> dict[str, Any] | None:
    """Compose total today PnL from the same section today windows shown in UI."""
    if not dates:
        return None

    total_return = Decimal("0")
    denominator = Decimal("0")
    latest_dates: list[Date] = []
    baseline_dates: list[Date] = []
    has_window = False
    for section in SECTION_RETURN_CONFIG:
        payload = build_section_return_series_payload(
            cur,
            section=section,
            start_date=dates[0],
            interval=interval,
            base_currency=base_currency,
            view_as_of=view_as_of,
            reported_returns_cfg=reported_returns_cfg,
            overview_payload=overview_payload,
            owner_ratio_case_sql=owner_ratio_case_sql,
            account_owner_ratio=account_owner_ratio,
            excluded_holding_accounts=excluded_holding_accounts,
            today_label_date=today_label_date,
        )
        window = payload.get("today_window")
        if not window:
            continue
        has_window = True
        total_return += dec(window.get("total_return"))
        try:
            latest_dates.append(Date.fromisoformat(str(window.get("latest_date"))))
        except (TypeError, ValueError):
            pass
        try:
            baseline_dates.append(Date.fromisoformat(str(window.get("baseline_date"))))
        except (TypeError, ValueError):
            pass
        rows = payload.get("rows") or []
        if rows:
            denominator += abs(dec(rows[-1].get("market_value")))

    if not has_window:
        return None
    latest_date = max(latest_dates) if latest_dates else dates[-1]
    baseline_date = max(baseline_dates) if baseline_dates else None
    return {
        "label_date": (today_label_date or latest_date).isoformat(),
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "latest_date": latest_date.isoformat(),
        "total_return": norm(total_return),
        "return_rate": norm(total_return / denominator if denominator > Decimal("0.0001") else Decimal("0")),
    }


def build_section_return_series_payload(
    cur,
    *,
    section: str,
    start_date: Date,
    interval: str,
    base_currency: str,
    view_as_of: Date,
    reported_returns_cfg: dict[str, Any],
    overview_payload: dict[str, Any],
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    account_owner_ratio: Callable[[str], Decimal],
    excluded_holding_accounts: set[str],
    today_label_date: Date | None = None,
) -> dict[str, Any]:
    """Return the legacy section return-rate payload without changing calculation semantics."""
    reported = _reported_section_performance(section, reported_returns_cfg)
    terminal_date = view_as_of
    reported_as_of = _reported_as_of_date(reported, reported_returns_cfg)
    if start_date > terminal_date:
        start_date = terminal_date
    cfg = SECTION_RETURN_CONFIG[section]

    first_activity = _section_first_activity_date(cur, section)
    if first_activity and section == "a_stock":
        start_date = first_activity
    elif first_activity and first_activity > start_date:
        start_date = first_activity
    dates = series_dates(start_date, terminal_date, interval)
    fx_timelines = load_fx_timelines(cur, base_currency)
    warnings: list[str] = []

    if section == "a_stock":
        values = tonghuashun_series(
            cur,
            dates=dates,
            base_currency=base_currency,
            account_owner_ratio=account_owner_ratio,
        )
        anchor_value = _account_net_liquidation_anchor_cny(
            cur,
            account_code="tonghuashun",
            overview_payload=overview_payload,
            account_owner_ratio=account_owner_ratio,
            fx_timelines=fx_timelines,
            as_of=dates[-1],
            base_currency=base_currency,
        )
        values = scale_series_to_terminal_anchor(values, terminal_date=dates[-1], target_value=anchor_value)
        events = _tonghuashun_external_flow_events(
            cur,
            start_date=start_date,
            end_date=terminal_date,
            base_currency=base_currency,
            fx_timelines=fx_timelines,
            account_owner_ratio=account_owner_ratio,
        )
    elif section == "us_stock":
        values, _costs, warnings = _ibkr_full_trade_return_series(
            cur,
            dates=dates,
            base_currency=base_currency,
            fx_timelines=fx_timelines,
        )
        values = _overlay_terminal_exact_section_value(
            cur,
            values,
            dates=dates,
            section=section,
            base_currency=base_currency,
            owner_ratio_case_sql=owner_ratio_case_sql,
            excluded_holding_accounts=excluded_holding_accounts,
            account_codes=["ibkr"],
        )
        events = _ibkr_trade_flow_events(
            cur,
            start_date=start_date,
            end_date=terminal_date,
            base_currency=base_currency,
            fx_timelines=fx_timelines,
        )
    else:
        values = _snapshot_category_value_series(
            cur,
            dates=dates,
            base_currency=base_currency,
            major_category=str(cfg["major_category"]),
            minor_category=cfg.get("minor_category"),
            owner_ratio_case_sql=owner_ratio_case_sql,
            excluded_holding_accounts=excluded_holding_accounts,
        )
        if section == "precious_metal":
            values = _apply_gold_proxy_to_precious_values(
                cur,
                values,
                dates=dates,
                base_currency=base_currency,
                owner_ratio_case_sql=owner_ratio_case_sql,
                excluded_holding_accounts=excluded_holding_accounts,
            )
        events = _investment_transaction_flow_events(
            cur,
            start_date=start_date,
            end_date=terminal_date,
            base_currency=base_currency,
            fx_timelines=fx_timelines,
            major_category=str(cfg["major_category"]),
            minor_category=cfg.get("minor_category"),
            account_owner_ratio=account_owner_ratio,
        )

    flows = _bucket_flow_events(dates, events)
    rows = _cash_flow_adjusted_return_rows(dates=dates, values=values, flows=flows)
    for row in rows:
        row["display_return_rate"] = (
            row["capital_return_rate"]
            if section == "a_stock"
            else row["time_weighted_return_rate"]
        )
    apply_reported_display_anchor(rows, reported)
    summary_reported = reported
    if reported_as_of and reported_as_of < terminal_date:
        summary_reported = None
    summary = section_return_summary(
        rows=rows,
        reported=summary_reported,
        owner_ratio_lookup=account_owner_ratio,
    )
    compounded_summary = _compound_reported_summary(
        rows=rows,
        reported=reported,
        reported_as_of=reported_as_of,
        terminal_date=terminal_date,
        owner_ratio_lookup=account_owner_ratio,
    )
    if compounded_summary is not None:
        summary = compounded_summary

    payload = {
        "section": section,
        "label": cfg["label"],
        "start": start_date.isoformat(),
        "end": terminal_date.isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": "cash_flow_adjusted_time_weighted_return",
        "coverage_notes": [
            "收益率按市值变动扣除当期净投入/净赎回后计算，不把追加资金当成收益。",
            *(
                [str(reported.get("basis") or reported.get("note") or "已按平台表现口径校准。")]
                if reported
                else []
            ),
            *(
                ["黄金积存金缺失估值按 SGE Au99.99 现货金价补齐；缺失时回退 518880.SH 黄金 ETF。"]
                if section == "precious_metal"
                else []
            ),
            *([str(w) for w in warnings] if warnings else []),
        ],
        "summary": {
            "total_return": norm(summary["total_return"]),
            "total_return_rate": norm(summary["total_return_rate"]),
            "annualized_return_rate": norm(summary["annualized_return_rate"]),
        },
        "rows": [
            {
                "date": norm(r["date"]),
                "market_value": norm(r["market_value"]),
                "net_flow": norm(r["net_flow"]),
                "cumulative_capital": norm(r["cumulative_capital"]),
                "capital_return_rate": norm(r["capital_return_rate"]),
                "daily_return_rate": norm(r["daily_return_rate"]),
                "time_weighted_return_rate": norm(r["time_weighted_return_rate"]),
                "display_return_rate": norm(r["display_return_rate"]),
                "display_return_rate_raw": norm(r.get("display_return_rate_raw")),
            }
            for r in rows
        ],
    }
    if today_label_date is not None:
        if section == "a_stock":
            payload["today_window"] = _a_stock_holdings_today_window_payload(
                cur,
                view_as_of=view_as_of,
                excluded_holding_accounts=excluded_holding_accounts,
                account_owner_ratio=account_owner_ratio,
                label_date=today_label_date,
                overview_payload=overview_payload,
            ) or _today_window_payload(
                rows,
                label_date=today_label_date,
                return_amount=_section_return_amount,
            )
        elif section == "us_stock":
            payload["today_window"] = _ibkr_holdings_today_window_payload(
                cur,
                base_currency=base_currency,
                label_date=today_label_date,
                view_as_of=view_as_of,
            ) or _today_window_payload(
                rows,
                label_date=today_label_date,
                return_amount=_section_return_amount,
            )
        elif section == "a_fund":
            payload["today_window"] = _holdings_category_today_window_payload(
                cur,
                category="A\u80a1",
                minor_category="A\u80a1\u57fa\u91d1",
                view_as_of=view_as_of,
                excluded_holding_accounts=excluded_holding_accounts,
                account_owner_ratio=account_owner_ratio,
                label_date=today_label_date,
            ) or _today_window_payload(
                rows,
                label_date=today_label_date,
                return_amount=_section_return_amount,
            )
        elif section == "precious_metal":
            payload["today_window"] = _holdings_category_today_window_payload(
                cur,
                category="\u8d35\u91d1\u5c5e",
                minor_category=None,
                view_as_of=view_as_of,
                excluded_holding_accounts=excluded_holding_accounts,
                account_owner_ratio=account_owner_ratio,
                label_date=today_label_date,
            ) or _today_window_payload(
                rows,
                label_date=today_label_date,
                return_amount=_section_return_amount,
            )
        else:
            payload["today_window"] = _today_window_payload(
            rows,
            label_date=today_label_date,
            return_amount=_section_return_amount,
            )
    return payload


def build_investment_return_series_payload(
    cur,
    *,
    start_date: Date,
    interval: str,
    base_currency: str,
    view_as_of: Date,
    overview_payload: dict[str, Any],
    reported_returns_cfg: dict[str, Any],
    owner_ratio_case_sql: Callable[[str], tuple[str, list[Any]]],
    account_owner_ratio: Callable[[str], Decimal],
    account_initial_capital_cny: Callable[[str], Decimal | None],
    excluded_holding_accounts: set[str] | None = None,
    today_label_date: Date | None = None,
) -> dict[str, Any]:
    """Return the legacy total investment return-rate payload."""
    if start_date > view_as_of:
        start_date = view_as_of

    step = "1 week" if interval == "weekly" else "1 day"
    h_owner_sql, h_owner_params = owner_ratio_case_sql("h")
    tx_owner_sql, tx_owner_params = owner_ratio_case_sql("tx")
    categories = list(INVESTMENT_RETURN_CATEGORIES)
    snapshot_excluded_accounts = sorted(INVESTMENT_RETURN_SNAPSHOT_EXCLUDE)

    cur.execute(
        f"""
        WITH series_dates AS (
            SELECT generate_series(%s::date, %s::date, %s::interval)::date AS date
            UNION
            SELECT %s::date AS date
        ), {COMPLETE_ACCOUNT_SNAPSHOT_CTE}, complete_holding_snapshot AS (
            SELECT hs.*
            FROM pams_holdings_snapshot hs
            JOIN complete_account_snapshot cas
              ON cas.account_code = hs.account_code
             AND cas.as_of = hs.as_of
        ), account_latest AS (
            SELECT d.date, hs.account_code, MAX(hs.as_of) AS as_of
            FROM series_dates d
            JOIN complete_holding_snapshot hs ON hs.as_of <= d.date
            WHERE hs.asset_liability = '资产'
              AND hs.major_category = ANY(%s::text[])
              AND NOT (hs.account_code = ANY(%s::text[]))
            GROUP BY d.date, hs.account_code
        ), h AS (
            SELECT al.date, hs.*
            FROM account_latest al
            JOIN complete_holding_snapshot hs
              ON hs.account_code = al.account_code
             AND hs.as_of = al.as_of
            WHERE hs.asset_liability = '资产'
              AND hs.major_category = ANY(%s::text[])
              AND NOT (hs.account_code = ANY(%s::text[]))
        ), positions_marked AS (
            SELECT
                h.date,
                h.market_value * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {h_owner_sql} AS market_value_cny
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
        ), position_values AS (
            SELECT
                date,
                COALESCE(SUM(market_value_cny), 0) AS market_value_cny
            FROM positions_marked
            GROUP BY date
        ), cost_marked AS (
            SELECT
                h.date,
                COALESCE(h.cost, 0) * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {h_owner_sql} AS cost_cny
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
        ), costs AS (
            SELECT date, COALESCE(SUM(cost_cny), 0) AS cost_cny
            FROM cost_marked
            GROUP BY date
        ), positions AS (
            SELECT
                d.date,
                COALESCE(v.market_value_cny, 0) AS market_value_cny,
                COALESCE(c.cost_cny, 0) AS cost_cny,
                COALESCE(v.market_value_cny, 0) - COALESCE(c.cost_cny, 0) AS position_pnl_cny
            FROM series_dates d
            LEFT JOIN position_values v ON v.date = d.date
            LEFT JOIN costs c ON c.date = d.date
        ), income_marked AS (
            SELECT
                d.date,
                tx.gross_amount * COALESCE(fx_hist.rate, fx_latest.rate, 1) * {tx_owner_sql} AS income_cny
            FROM series_dates d
            JOIN pams_investment_trade_fact tx ON tx.trade_date <= d.date
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = tx.currency
                  AND fx.as_of <= d.date
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily fx
                WHERE fx.base_currency = %s
                  AND fx.quote_currency = tx.currency
                ORDER BY fx.as_of DESC
                LIMIT 1
            ) fx_latest ON TRUE
            WHERE COALESCE(
                NULLIF(tx.asset_major_category, ''),
                NULLIF(tx.raw_json #>> '{{source_row,major_category}}', ''),
                NULLIF(tx.market, ''),
                tx.trade_type
              ) = ANY(%s::text[])
              AND LOWER(COALESCE(NULLIF(tx.side, ''), tx.trade_type)) IN ('dividend', 'interest', 'income', 'distribution', 'coupon')
              AND tx.gross_amount IS NOT NULL
              AND COALESCE(LOWER(tx.review_status), 'confirmed') NOT IN ('cancelled', 'canceled', 'void')
        ), income AS (
            SELECT date, COALESCE(SUM(income_cny), 0) AS realized_income_cny
            FROM income_marked
            GROUP BY date
        )
        SELECT
            d.date,
            COALESCE(p.market_value_cny, 0) AS market_value_cny,
            COALESCE(p.cost_cny, 0) AS cost_cny,
            COALESCE(p.position_pnl_cny, 0) AS position_pnl_cny,
            COALESCE(i.realized_income_cny, 0) AS realized_income_cny,
            COALESCE(p.position_pnl_cny, 0) + COALESCE(i.realized_income_cny, 0) AS total_pnl_cny,
            CASE
                WHEN COALESCE(p.cost_cny, 0) = 0 THEN 0
                ELSE (COALESCE(p.position_pnl_cny, 0) + COALESCE(i.realized_income_cny, 0)) / p.cost_cny
            END AS return_rate
        FROM series_dates d
        LEFT JOIN positions p ON p.date = d.date
        LEFT JOIN income i ON i.date = d.date
        ORDER BY d.date
        """,
        (
            start_date,
            view_as_of,
            step,
            view_as_of,
            view_as_of,
            categories,
            snapshot_excluded_accounts,
            categories,
            snapshot_excluded_accounts,
            *h_owner_params,
            base_currency,
            base_currency,
            *h_owner_params,
            base_currency,
            base_currency,
            *tx_owner_params,
            base_currency,
            base_currency,
            categories,
        ),
    )
    rows = [dict(r) for r in cur.fetchall()]
    dates = [r["date"] for r in rows]

    if dates:
        overview_accounts = [
            a for a in overview_payload.get("accounts", [])
            if a.get("include_in_nav") and account_anchor_amount(a) is not None
        ]
        fx_timelines = load_fx_timelines(cur, base_currency)

        a_share_values = tonghuashun_series(
            cur,
            dates=dates,
            base_currency=base_currency,
            account_owner_ratio=account_owner_ratio,
        )
        tonghuashun_anchor_cny = _account_net_liquidation_anchor_cny(
            cur,
            account_code="tonghuashun",
            overview_payload=overview_payload,
            account_owner_ratio=account_owner_ratio,
            fx_timelines=fx_timelines,
            as_of=dates[-1],
            base_currency=base_currency,
        )
        a_share_values = scale_series_to_terminal_anchor(
            a_share_values,
            terminal_date=dates[-1],
            target_value=tonghuashun_anchor_cny,
        )
        a_share_costs = _tonghuashun_cost_series(
            cur,
            dates=dates,
            account_owner_ratio=account_owner_ratio,
            account_initial_capital_cny=account_initial_capital_cny,
        )
        ibkr_values, ibkr_costs, ibkr_warnings = _ibkr_full_trade_return_series(
            cur,
            dates=dates,
            base_currency=base_currency,
            fx_timelines=fx_timelines,
        )
        ibkr_values = _overlay_terminal_exact_section_value(
            cur,
            ibkr_values,
            dates=dates,
            section="us_stock",
            base_currency=base_currency,
            owner_ratio_case_sql=owner_ratio_case_sql,
            excluded_holding_accounts=set(excluded_holding_accounts or set()),
            account_codes=["ibkr"],
        )

        esop_value = account_anchor_value_cny(
            next((a for a in overview_accounts if str(a.get("account_code") or "") == "huawei_stock"), None),
            fx_timelines=fx_timelines,
            as_of=dates[-1],
            base_currency=base_currency,
        ) or Decimal("0")

        for row in rows:
            sample_date = row["date"]
            a_value = a_share_values.get(sample_date, Decimal("0"))
            a_cost = a_share_costs.get(sample_date, Decimal("0"))
            ibkr_value = ibkr_values.get(sample_date, Decimal("0"))
            ibkr_cost = ibkr_costs.get(sample_date, Decimal("0"))
            extra_value = a_value + ibkr_value + esop_value
            extra_cost = a_cost + ibkr_cost + esop_value
            row["market_value_cny"] = dec(row["market_value_cny"]) + extra_value
            row["cost_cny"] = dec(row["cost_cny"]) + extra_cost
            row["position_pnl_cny"] = dec(row["position_pnl_cny"]) + (a_value - a_cost)
            row["position_pnl_cny"] += ibkr_value - ibkr_cost
            row["total_pnl_cny"] = row["position_pnl_cny"] + dec(row["realized_income_cny"])
            row["return_rate"] = (
                row["total_pnl_cny"] / row["cost_cny"]
                if row["cost_cny"] != 0
                else Decimal("0")
            )
        reported_adjustments = _reported_account_return_adjustments(
            cur,
            dates,
            base_currency,
            reported_returns_cfg=reported_returns_cfg,
            owner_ratio_case_sql=owner_ratio_case_sql,
        )
        for row in rows:
            adjustment = reported_adjustments.get(row["date"], Decimal("0"))
            row["reported_return_adjustment_cny"] = adjustment
            if adjustment == 0:
                continue
            row["position_pnl_cny"] = dec(row["position_pnl_cny"]) + adjustment
            row["total_pnl_cny"] = row["position_pnl_cny"] + dec(row["realized_income_cny"])
            row["return_rate"] = (
                row["total_pnl_cny"] / row["cost_cny"]
                if row["cost_cny"] != 0
                else Decimal("0")
            )

        section_return_amounts = _total_section_return_amounts(
            cur,
            dates=dates,
            interval=interval,
            base_currency=base_currency,
            view_as_of=view_as_of,
            reported_returns_cfg=reported_returns_cfg,
            overview_payload=overview_payload,
            owner_ratio_case_sql=owner_ratio_case_sql,
            account_owner_ratio=account_owner_ratio,
            excluded_holding_accounts=set(snapshot_excluded_accounts),
        )
        if section_return_amounts:
            for row in rows:
                sample_date = row["date"]
                if sample_date not in section_return_amounts:
                    continue
                original_position_pnl = dec(row["position_pnl_cny"])
                section_position_pnl = dec(section_return_amounts[sample_date])
                correction = section_position_pnl - original_position_pnl
                row["reported_return_adjustment_cny"] = (
                    dec(row.get("reported_return_adjustment_cny")) + correction
                )
                row["position_pnl_cny"] = section_position_pnl
                row["total_pnl_cny"] = section_position_pnl + dec(row["realized_income_cny"])
                row["return_rate"] = (
                    row["total_pnl_cny"] / row["cost_cny"]
                    if row["cost_cny"] != 0
                    else Decimal("0")
                )
    else:
        ibkr_warnings = []

    twr = Decimal("0")
    prev_row: dict[str, Any] | None = None
    for row in rows:
        if prev_row is None:
            daily_return = Decimal("0")
        else:
            capital = abs(dec(prev_row.get("market_value_cny")))
            if capital <= Decimal("0.0001"):
                capital = abs(dec(prev_row.get("cost_cny")))
            pnl_delta = dec(row.get("total_pnl_cny")) - dec(prev_row.get("total_pnl_cny"))
            daily_return = pnl_delta / capital if capital > Decimal("0.0001") else Decimal("0")
            twr = (Decimal("1") + twr) * (Decimal("1") + daily_return) - Decimal("1")
        row["daily_return_rate"] = daily_return
        row["time_weighted_return_rate"] = twr
        prev_row = row

    payload = {
        "start": start_date.isoformat(),
        "end": view_as_of.isoformat(),
        "base_currency": base_currency,
        "interval": interval,
        "method": "coverage_adjusted_daily_return",
        "coverage_notes": [
            {
                "account_code": "ibkr",
                "label": "IBKR 美股",
                "reason": "已用 IBKR 全部股票/ETF交易流水和 Yahoo 日收盘价反算日度市值；已清仓标的在持有期间纳入，清仓后保留实现盈亏；融资现金和汇兑仍以账户收益口径为准。",
            },
            {
                "account_code": "tonghuashun",
                "label": "同花顺A股个股",
                "reason": "已改用 TradingView 交易记录和本地日收盘价重建日度曲线，不再使用稀疏快照。",
            },
            {
                "account_code": "huawei_stock",
                "label": "华为 ESOP",
                "reason": "按 2026-01-01 稳定持仓锚定，分红在实际到账日计入收益。",
            },
            *[
                {
                    "account_code": "ibkr",
                    "label": "IBKR 美股",
                    "reason": warning,
                }
                for warning in ibkr_warnings
            ],
        ],
        "rows": [
            {
                "date": norm(r["date"]),
                "market_value": norm(r["market_value_cny"]),
                "cost": norm(r["cost_cny"]),
                "position_pnl": norm(r["position_pnl_cny"]),
                "realized_income": norm(r["realized_income_cny"]),
                "total_pnl": norm(r["total_pnl_cny"]),
                "return_rate": norm(r["return_rate"]),
                "reported_return_adjustment": norm(r.get("reported_return_adjustment_cny")),
                "daily_return_rate": norm(r.get("daily_return_rate")),
                "time_weighted_return_rate": norm(r.get("time_weighted_return_rate")),
            }
            for r in rows
        ],
    }
    if today_label_date is not None:
        payload["today_window"] = _total_section_today_window_payload(
            cur,
            dates=dates,
            interval=interval,
            base_currency=base_currency,
            view_as_of=view_as_of,
            reported_returns_cfg=reported_returns_cfg,
            overview_payload=overview_payload,
            owner_ratio_case_sql=owner_ratio_case_sql,
            account_owner_ratio=account_owner_ratio,
            excluded_holding_accounts=set(excluded_holding_accounts or set()),
            today_label_date=today_label_date,
        ) or _today_window_payload(
            rows,
            label_date=today_label_date,
            return_amount=_total_return_amount,
        )
    return payload


def build_account_performance_payload(
    cur,
    *,
    account_code: str,
    owner_ratio: Decimal,
    initial_capital: Decimal | None,
    net_liquidation_gross: Decimal | None = None,
    net_liquidation: Decimal | None = None,
    net_liquidation_as_of: Any | None = None,
    daily_pnl_label_date: Date | None = None,
) -> dict[str, Any] | None:
    """Return account-level personal performance using existing snapshot semantics."""
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT account_as_of AS as_of
        FROM latest_account
        WHERE account_code = %s
        """,
        (Date.today(), account_code),
    )
    row = cur.fetchone()
    as_of = row["as_of"] if row else None
    if as_of is None:
        return None

    cur.execute(
        """
        WITH fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY'
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            h.account_code,
            h.as_of,
            h.major_category,
            h.minor_category,
            h.symbol,
            h.name,
            h.currency,
            h.market_value * COALESCE(fx.rate, 1) AS market_value_cny,
            COALESCE(h.holding_pnl, 0) * COALESCE(fx.rate, 1) AS holding_pnl_cny
        FROM pams_holdings_snapshot h
        LEFT JOIN fx ON fx.quote_currency = h.currency
        WHERE h.account_code = %s
          AND h.as_of = %s
        """,
        (account_code, as_of),
    )
    rows = [dict(row) for row in cur.fetchall()]

    position_value_gross = sum(dec(row.get("market_value_cny")) for row in rows)
    holding_pnl_gross = sum(dec(row.get("holding_pnl_cny")) for row in rows)
    net_gross = net_liquidation_gross if net_liquidation_gross is not None else position_value_gross
    net_liquidation_source = "current_snapshot"
    net_liquidation_rollforward: dict[str, Any] | None = None
    rolled_forward_net_liquidation = False
    daily_pnl_personal: Decimal | None = None
    daily_pnl_source: str | None = None
    anchor_position_value_gross: Decimal | None = None

    anchor_as_of = None
    if net_liquidation_as_of is not None:
        try:
            anchor_as_of = (
                net_liquidation_as_of.date()
                if hasattr(net_liquidation_as_of, "date") and not hasattr(net_liquidation_as_of, "isoformat")
                else net_liquidation_as_of
            )
        except Exception:
            anchor_as_of = net_liquidation_as_of

    if net_liquidation_gross is not None and anchor_as_of is not None and str(anchor_as_of)[:10] < str(as_of)[:10]:
        cur.execute(
            f"""
            WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
            SELECT SUM(market_value) AS market_value
            FROM pams_holdings_snapshot hs
            JOIN latest_account la
              ON la.account_code = hs.account_code
             AND la.account_as_of = hs.as_of
            WHERE hs.account_code = %s
            """,
            (anchor_as_of, account_code),
        )
        anchor_row = cur.fetchone() or {}
        anchor_position_value_gross = dec(anchor_row.get("market_value"))
        cur.execute(
            """
            SELECT COALESCE(SUM(
                COALESCE(
                    net_cash_cny,
                    net_cash * fx_rate_to_cny,
                    net_cash,
                    0
                )
            ), 0) AS cash_delta
            FROM pams_investment_trade_fact
            WHERE account_code = %s
              AND affects_cash
              AND trade_date > %s
              AND trade_date <= %s
            """,
            (account_code, anchor_as_of, as_of),
        )
        cash_row = cur.fetchone() or {}
        cash_delta_gross = dec(cash_row.get("cash_delta"))
        position_delta_gross = position_value_gross - anchor_position_value_gross
        net_gross = net_liquidation_gross + position_delta_gross + cash_delta_gross
        net_liquidation_source = "rolled_forward_manual_balance"
        rolled_forward_net_liquidation = True
        net_liquidation_rollforward = {
            "anchor_as_of": str(anchor_as_of)[:10],
            "anchor_net_liquidation_gross_cny": norm(net_liquidation_gross),
            "anchor_position_market_value_gross_cny": norm(anchor_position_value_gross),
            "position_delta_gross_cny": norm(position_delta_gross),
            "cash_delta_gross_cny": norm(cash_delta_gross),
        }

        cur.execute(
            f"""
            WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
            SELECT MAX(cas.as_of) AS previous_as_of
            FROM complete_account_snapshot cas
            WHERE cas.account_code = %s
              AND cas.as_of < %s
            """,
            (as_of, account_code, as_of),
        )
        previous_row = cur.fetchone() or {}
        previous_as_of = previous_row.get("previous_as_of")
        if previous_as_of is not None:
            cur.execute(
                """
                SELECT SUM(market_value) AS market_value
                FROM pams_holdings_snapshot
                WHERE account_code = %s
                  AND as_of = %s
                """,
                (account_code, previous_as_of),
            )
            previous_position_row = cur.fetchone() or {}
            previous_position_value_gross = dec(previous_position_row.get("market_value"))
            cur.execute(
                """
                SELECT COALESCE(SUM(
                    COALESCE(
                        net_cash_cny,
                        net_cash * fx_rate_to_cny,
                        net_cash,
                        0
                    )
                ), 0) AS cash_delta
                FROM pams_investment_trade_fact
                WHERE account_code = %s
                  AND affects_cash
                  AND trade_date > %s
                  AND trade_date <= %s
                """,
                (account_code, anchor_as_of, previous_as_of),
            )
            previous_cash_row = cur.fetchone() or {}
            previous_cash_delta_gross = dec(previous_cash_row.get("cash_delta"))
            previous_net_gross = (
                net_liquidation_gross
                + (previous_position_value_gross - anchor_position_value_gross)
                + previous_cash_delta_gross
            )
            daily_pnl_personal = (net_gross - previous_net_gross) * owner_ratio
            daily_pnl_source = "rolled_forward_net_liquidation_delta"

    if rolled_forward_net_liquidation:
        net_personal = net_gross * owner_ratio
    elif net_liquidation is not None:
        net_personal = net_liquidation
    else:
        net_personal = net_gross * owner_ratio
    holding_pnl_personal = holding_pnl_gross * owner_ratio
    initial_personal = initial_capital * owner_ratio if initial_capital is not None else None
    total_pnl = net_personal - initial_personal if initial_personal is not None else None
    realized_pnl = total_pnl - holding_pnl_personal if total_pnl is not None else None
    return_ratio = total_pnl / initial_personal if initial_personal and initial_personal > 0 else None

    daily_pnl_effective = daily_pnl_personal is not None
    if daily_pnl_label_date is not None:
        daily_pnl_effective = daily_pnl_effective and _date_key(as_of) == _date_key(daily_pnl_label_date)
    visible_daily_pnl = daily_pnl_personal if daily_pnl_effective else None

    return {
        "status": "ok",
        "account_code": account_code,
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "base_currency": "CNY",
        "owner_ratio": norm(owner_ratio),
        "initial_capital_gross_cny": norm(initial_capital),
        "initial_capital_cny": norm(initial_personal),
        "net_liquidation_gross_cny": norm(net_gross),
        "net_liquidation_cny": norm(net_personal),
        "net_liquidation_source": net_liquidation_source,
        "net_liquidation_rollforward": net_liquidation_rollforward,
        "position_market_value_gross_cny": norm(position_value_gross),
        "position_market_value_cny": norm(position_value_gross * owner_ratio),
        "cash_or_uninvested_cny": norm(net_personal - position_value_gross * owner_ratio),
        "holding_pnl_cny": norm(holding_pnl_personal),
        "total_pnl_cny": norm(total_pnl),
        "daily_pnl_cny": norm(visible_daily_pnl),
        "raw_daily_pnl_cny": norm(daily_pnl_personal),
        "daily_pnl_effective": daily_pnl_effective,
        "daily_pnl_label_date": _date_key(daily_pnl_label_date),
        "daily_pnl_source": daily_pnl_source,
        "realized_pnl_cny": norm(realized_pnl),
        "return_ratio": norm(return_ratio),
        "rows_count": len(rows),
    }
