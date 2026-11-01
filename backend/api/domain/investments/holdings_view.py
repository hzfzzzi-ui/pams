"""Holdings page read adapters.

The functions in this module keep the legacy holdings payload shape while
moving display-read logic out of ``main.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo
from domain.market.quote_valuation import quote_price_as_of, quote_valuation_cte
from domain.investments.trade_snapshot_display import enrich_trade_snapshot_costs
from domain.market.overseas_return_calendar import MARKET_CURRENCIES, latest_completed_session
from domain.investments.snapshot_coverage import annotate_snapshot_coverage

from domain.investments.daily_return_state import (
    RETURN_STATUS_CONFIRMED,
    RETURN_STATUS_ESTIMATED,
    RETURN_STATUS_MIXED,
    RETURN_STATUS_PENDING,
    normalize_return_status,
    return_status_label,
)
from domain.investments.holdings_positions import (
    _apply_owner_ratio,
    _closed_position_rows,
    _dec,
    _enrich_esop_realized_income,
    _enrich_realized_trade_pnl,
    _filter_current_holding_rows,
    _holding_key,
    _ibkr_closed_position_rows,
    _norm,
)


COMPLETE_ACCOUNT_SNAPSHOT_CTE = """
snapshot_view_cutoff AS (SELECT %s::date AS as_of),
account_snapshot_stats AS (
    SELECT account_code, as_of, COUNT(*) AS row_count
    FROM pams_holdings_snapshot
    WHERE as_of <= (SELECT as_of FROM snapshot_view_cutoff)
    GROUP BY account_code, as_of
), complete_account_snapshot AS (
    -- This selects observed snapshots only; scope completeness is checked
    -- separately from immutable evidence, never inferred from row counts.
    SELECT s.account_code, s.as_of, s.row_count FROM account_snapshot_stats s
), latest_account AS (
    SELECT account_code, MAX(as_of) AS account_as_of
    FROM complete_account_snapshot
    GROUP BY account_code
)
"""

LATEST_KNOWN_HOLDINGS_CTE = COMPLETE_ACCOUNT_SNAPSHOT_CTE + """
, latest_known_position AS (
    SELECT hs.account_code, to_jsonb(hs)->>'currency' AS position_currency,
           COALESCE(NULLIF(to_jsonb(hs)->>'name', ''), NULLIF(to_jsonb(hs)->>'symbol', ''), hs.ctid::text) AS position_key, MAX(hs.as_of) AS as_of
    FROM pams_holdings_snapshot hs
    WHERE as_of <= (SELECT as_of FROM snapshot_view_cutoff)
    GROUP BY hs.account_code, to_jsonb(hs)->>'currency', COALESCE(NULLIF(to_jsonb(hs)->>'name', ''), NULLIF(to_jsonb(hs)->>'symbol', ''), hs.ctid::text)
), latest_known_holding AS (
    SELECT hs.* FROM pams_holdings_snapshot hs JOIN latest_known_position p
      ON p.account_code=hs.account_code AND p.position_key=COALESCE(NULLIF(to_jsonb(hs)->>'name', ''), NULLIF(to_jsonb(hs)->>'symbol', ''), hs.ctid::text)
     AND p.position_currency IS NOT DISTINCT FROM to_jsonb(hs)->>'currency'
     AND p.as_of=hs.as_of
)
"""


def advance_overseas_view_date(cur, candidate, *, now: datetime | None = None):
    """A completed local-market mark need not wait for China's 15:05 refresh.

    Evidence must belong to a current complete-account member. The source date
    advances only the read cutoff, never the account's persisted membership.
    """
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if candidate >= today:
        return candidate
    cur.execute("SELECT to_regclass('public.market_observations') IS NOT NULL AS exists")
    if not (cur.fetchone() or {}).get("exists"):
        return candidate
    cur.execute(f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT DISTINCT q.market, q.currency, q.quote_date
        FROM (SELECT * FROM market_observations WHERE observation_type = 'quote') q
        JOIN pams_holdings_snapshot h ON h.symbol = q.symbol AND h.currency = q.currency
        JOIN latest_account la ON la.account_code = h.account_code AND la.account_as_of = h.as_of
        WHERE h.major_category = '美股' AND h.asset_liability = '资产' AND h.quantity > 0
          AND q.quote_date > %s AND q.quote_date <= %s
          AND q.market IN ('HK', 'KR', 'SE', 'CH')
          AND q.quote_session = 'close' AND q.return_status = 'confirmed'
          AND q.raw_json @> '{{"valuation_projection_only":true}}'::jsonb
          AND q.price > 0 AND q.price::text NOT IN ('NaN', 'Infinity', '-Infinity')
    """, (today, candidate, today))
    cutoffs = {}
    for row in cur.fetchall():
        bucket = str(row["market"]).lower()
        if row["currency"] != MARKET_CURRENCIES[bucket]:
            continue
        if bucket not in cutoffs:
            cutoffs[bucket] = latest_completed_session(bucket, current)
        day = row["quote_date"]
        if cutoffs[bucket] and day <= cutoffs[bucket]:
            candidate = max(candidate, day)
    return candidate

FUND_NAV_STATUS_COLUMNS = """
                   h.updated_at AS valuation_updated_at,
                   COALESCE(fund_nav_status.daily_pnl_status, market_daily_status.daily_pnl_status) AS daily_pnl_status,
                   COALESCE(fund_nav_status.daily_pnl_status_label, market_daily_status.daily_pnl_status_label) AS daily_pnl_status_label,
                   fund_nav_status.fund_nav_confirmed_date,
                   fund_nav_status.fund_nav_confirmed_unit_nav,
                   fund_nav_status.fund_nav_estimate_time,
                   fund_nav_status.fund_nav_estimated_unit_nav,
                   fund_nav_status.fund_nav_raw_status
"""

FUND_NAV_STATUS_JOIN = """
            LEFT JOIN LATERAL (
                SELECT
                    CASE
                        WHEN e.estimated_unit_nav IS NOT NULL THEN 'estimated'
                        WHEN e.confirmed_unit_nav IS NOT NULL THEN 'confirmed'
                        ELSE NULL
                    END AS daily_pnl_status,
                    CASE
                        WHEN e.estimated_unit_nav IS NOT NULL THEN '预估收益'
                        WHEN e.confirmed_unit_nav IS NOT NULL THEN '今日收益已确认'
                        ELSE NULL
                    END AS daily_pnl_status_label,
                    e.confirmed_nav_date AS fund_nav_confirmed_date,
                    e.confirmed_unit_nav AS fund_nav_confirmed_unit_nav,
                    e.estimate_time AS fund_nav_estimate_time,
                    e.estimated_unit_nav AS fund_nav_estimated_unit_nav,
                    e.status AS fund_nav_raw_status
                FROM (SELECT * FROM market_observations WHERE observation_type = 'fund') e
                WHERE h.major_category = 'A股'
                  AND h.minor_category LIKE '%%基金%%'
                  AND e.as_of = h.as_of
                  AND e.account_code = h.account_code
                  AND (
                      e.symbol = LPAD(COALESCE(NULLIF(h.symbol, ''), e.symbol), 6, '0')
                      OR e.name = h.name
                  )
                ORDER BY e.updated_at DESC
                LIMIT 1
            ) fund_nav_status ON TRUE
"""

MARKET_DAILY_STATUS_JOIN = """
            LEFT JOIN LATERAL (
                SELECT
                    CASE
                        WHEN md.source IN ('databento_estimate', 'ibkr_tws_estimate', 'ibkr_client_portal_estimate', 'external_estimate')
                          OR md.market IN ('US_OVERNIGHT', 'US_EXTENDED', 'YAHOO_EXTENDED')
                        THEN 'estimated'
                        ELSE NULL
                    END AS daily_pnl_status,
                    CASE
                        WHEN md.source IN ('databento_estimate', 'ibkr_tws_estimate', 'ibkr_client_portal_estimate', 'external_estimate')
                          OR md.market IN ('US_OVERNIGHT', 'US_EXTENDED', 'YAHOO_EXTENDED')
                        THEN '预估收益'
                        ELSE NULL
                    END AS daily_pnl_status_label
                FROM (SELECT * FROM market_observations WHERE observation_type = 'daily') md
                WHERE h.major_category = '美股'
                  AND md.as_of = h.as_of
                  AND (
                      (COALESCE(NULLIF(h.symbol, ''), '') <> '' AND UPPER(md.symbol) = UPPER(h.symbol))
                      OR (COALESCE(NULLIF(h.asset_id, ''), '') <> '' AND md.asset_id = h.asset_id)
                  )
                ORDER BY
                    CASE
                        WHEN md.source = 'databento_estimate' THEN 0
                        WHEN md.source IN ('ibkr_tws_estimate', 'ibkr_client_portal_estimate') THEN 1
                        WHEN md.source = 'external_estimate' THEN 2
                        ELSE 3
                    END,
                    md.updated_at DESC
                LIMIT 1
            ) market_daily_status ON TRUE
"""


def latest_complete_snapshot_date(cur, *, view_as_of: Any, categories: list[str] | None = None) -> Any | None:
    """Return the latest date that is not dominated by partial account snapshots."""
    category_filter = "AND hs.major_category = ANY(%s::text[])" if categories else ""
    params: list[Any] = [view_as_of]
    if categories:
        params.append(categories)
    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT MAX(hs.as_of) AS as_of
        FROM pams_holdings_snapshot hs
        JOIN latest_account la
          ON la.account_code = hs.account_code
         AND la.account_as_of = hs.as_of
        WHERE hs.as_of <= %s
          {category_filter}
        """,
        [view_as_of, *params],
    )
    row = cur.fetchone()
    return row.get("as_of") if row else None


def _bool_field(row: Any, field: str) -> bool:
    if isinstance(row, Mapping):
        return bool(row.get(field))
    if isinstance(row, (list, tuple)) and row:
        return bool(row[0])
    return False


def _trade_fact_rows_available(cur) -> bool:
    cur.execute("SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS exists")
    if not _bool_field(cur.fetchone(), "exists"):
        return False
    cur.execute("SELECT EXISTS (SELECT 1 FROM pams_investment_trade_fact) AS has_rows")
    return _bool_field(cur.fetchone(), "has_rows")


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    return _bool_field(cur.fetchone(), "exists")


def _ibkr_price_history_relation() -> str:
    """Missing prior-price evidence remains unknown, not a zero-valued history."""
    return """(SELECT NULL::text AS account_code, NULL::text AS symbol, NULL::text AS name,
        NULL::numeric AS current_price, NULL::numeric AS quantity, NULL::numeric AS market_value,
        NULL::date AS as_of WHERE FALSE)"""


def _mark_missing_ibkr_daily_basis(rows: list[dict[str, Any]], *, history_available: bool) -> list[dict[str, Any]]:
    """Do not turn an unavailable previous price into a confirmed zero return."""
    if history_available:
        return rows
    for row in rows:
        if (str(row.get("account") or row.get("account_code") or "") != "ibkr"
                or row.get("is_closed") or row.get("previous_price") is not None):
            continue
        for name in ("daily_pnl", "daily_price_pnl", "daily_fx_pnl", "daily_total_pnl"):
            for key in (name, name + "_cny", name + "_gross_cny"):
                if key in row:
                    row[key] = None
        row["daily_return_status"] = RETURN_STATUS_PENDING
        row["daily_return_status_label"] = return_status_label(RETURN_STATUS_PENDING)
        row["daily_return_status_short_label"] = _return_status_short_label(RETURN_STATUS_PENDING)
        row["daily_return_status_source"] = None
        row["daily_pnl_status"] = RETURN_STATUS_PENDING
        row["daily_pnl_status_label"] = return_status_label(RETURN_STATUS_PENDING)
        row["daily_return_status_reason"] = "missing_previous_price_evidence"
    return rows


def _preserve_missing_cost(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if "cost" not in row or row["cost"] is not None:
            continue
        for key in ("cost_cny", "cost_gross_cny", "buy_price", "holding_pnl", "holding_pnl_native",
                    "holding_pnl_cny", "holding_pnl_gross_cny", "holding_pnl_before_realized_trade",
                    "holding_pnl_before_realized_trade_native", "holding_pnl_before_realized_trade_cny"):
            if key in row:
                row[key] = None
        row["cost_evidence"] = "unavailable"
    return rows


def _mark_missing_a_stock_daily_basis(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if not row.pop("_daily_basis_unavailable", False):
            continue
        for name in ("daily_pnl", "daily_price_pnl", "daily_fx_pnl", "daily_total_pnl"):
            for key in (name, name + "_native", name + "_cny", name + "_gross_cny"):
                if key in row:
                    row[key] = None
        row["daily_return_status"] = RETURN_STATUS_PENDING
        row["daily_return_status_label"] = return_status_label(RETURN_STATUS_PENDING)
        row["daily_return_status_short_label"] = _return_status_short_label(RETURN_STATUS_PENDING)
        row["daily_pnl_status"] = RETURN_STATUS_PENDING
        row["daily_pnl_status_label"] = return_status_label(RETURN_STATUS_PENDING)
        row["daily_return_status_reason"] = "missing_previous_price_or_confirmed_trade"
    return rows


def _adjust_a_stock_daily_pnl_for_same_day_buys(cur, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use trade cost, not previous close, for A-share positions opened today.

    Brokers usually report same-day reference PnL as:
    existing shares: current price - previous close
    today-buy shares: current price - execution cost
    Trading fees remain part of total cost/realized accounting, but broker
    "today PnL" screens usually exclude fees from same-day price PnL.
    This keeps newly opened positions from overstating the visible "昨日收益".
    """
    keys = {
        (
            str(row.get("account") or row.get("account_code") or ""),
            str(row.get("symbol") or ""),
            row.get("as_of"),
        )
        for row in rows
        if row.get("major_category") == "A股"
        and row.get("symbol")
        and row.get("as_of") is not None
        and _dec(row.get("quantity")) > 0
        and row.get("current_price") is not None
    }
    if not keys:
        return rows

    accounts = sorted({account for account, _, _ in keys})
    symbols = sorted({symbol for _, symbol, _ in keys})
    dates = sorted({as_of for _, _, as_of in keys if as_of is not None})

    if _trade_fact_rows_available(cur):
        cur.execute("""SELECT to_regclass('public.source_items') IS NOT NULL AS item_exists,
            to_regclass('public.source_batches') IS NOT NULL AS batch_exists""")
        source_relations = cur.fetchone()
        if source_relations and source_relations.get("item_exists") and source_relations.get("batch_exists"):
            source_filter = """((source_item_id IS NULL AND source_table NOT IN
                ('tradingview_csv', 'source_items', 'ibkr_flex_trade')) OR EXISTS (
                SELECT 1 FROM source_items si JOIN source_batches sb ON sb.batch_id=si.batch_id
                WHERE si.source_item_id=pams_investment_trade_fact.source_item_id
                  AND si.review_status='confirmed' AND sb.status='published'
                  AND sb.account_code=pams_investment_trade_fact.account_code))"""
        else:
            source_filter = "source_table NOT IN ('tradingview_csv', 'source_items', 'ibkr_flex_trade')"
        cur.execute(
            f"""
            SELECT
                account_code,
                symbol AS normalized_symbol,
                trade_date AS as_of,
                SUM(
                    CASE
                        WHEN trade_type IN ('buy', 'fund_subscription') OR LOWER(COALESCE(side, '')) = 'buy'
                        THEN ABS(COALESCE(quantity, 0))
                        ELSE 0
                    END
                ) AS buy_qty,
                SUM(
                    CASE
                        WHEN trade_type IN ('buy', 'fund_subscription') OR LOWER(COALESCE(side, '')) = 'buy'
                        THEN ABS(COALESCE(gross_amount, COALESCE(quantity, 0) * COALESCE(price, 0)))
                        ELSE 0
                    END
                ) AS buy_value
            FROM pams_investment_trade_fact
            WHERE account_code = ANY(%s::text[])
              AND symbol = ANY(%s::text[])
              AND trade_date = ANY(%s::date[])
              AND COALESCE(quantity, 0) > 0
              AND LOWER(review_status) IN ('auto','reviewed','confirmed')
              AND {source_filter}
            GROUP BY account_code, symbol, trade_date
            """,
            (accounts, symbols, dates),
        )
    else:
        for row in rows:
            if (row.get("major_category") == "A股" and row.get("previous_price") is None
                    and row.get("current_price") is not None and _dec(row.get("quantity")) > 0):
                row["_daily_basis_unavailable"] = True
        return rows
    trades = {
        (
            str(row["account_code"]),
            str(row["normalized_symbol"]),
            row["as_of"],
        ): row
        for row in cur.fetchall()
    }

    for row in rows:
        key = (
            str(row.get("account") or row.get("account_code") or ""),
            str(row.get("symbol") or ""),
            row.get("as_of"),
        )
        trade = trades.get(key)
        if not trade:
            if row.get("major_category") == "A股" and row.get("previous_price") is None:
                row["_daily_basis_unavailable"] = True
            continue
        buy_qty = _dec(trade.get("buy_qty"))
        if buy_qty <= 0:
            continue

        quantity = _dec(row.get("quantity"))
        current_price = _dec(row.get("current_price"))
        previous_price = _dec(row.get("previous_price"))
        if quantity <= 0 or current_price <= 0:
            continue

        today_qty = min(buy_qty, quantity)
        existing_qty = max(quantity - today_qty, Decimal("0"))
        today_buy_value = _dec(trade.get("buy_value"))
        if existing_qty == 0:
            native_daily_pnl = current_price * today_qty - today_buy_value
        else:
            native_daily_pnl = (
                (current_price - previous_price) * existing_qty
                + (current_price * today_qty - today_buy_value)
            )

        native_before = _dec(row.get("daily_pnl"))
        cny_before = row.get("daily_pnl_cny")
        market_native = _dec(row.get("market_value"))
        market_cny = row.get("market_value_cny")
        if str(row.get("currency") or "").upper() == "CNY":
            fx_rate = Decimal(1)
        elif native_before != 0 and cny_before is not None:
            fx_rate = _dec(cny_before) / native_before
        elif market_native != 0 and market_cny is not None:
            fx_rate = _dec(market_cny) / market_native
        else:
            fx_rate = None

        row["daily_pnl"] = native_daily_pnl
        row["daily_pnl_native"] = native_daily_pnl
        cny_daily_pnl = (native_daily_pnl * fx_rate if fx_rate is not None
                         else Decimal(0) if native_daily_pnl == 0 else None)
        row["daily_pnl_cny"] = cny_daily_pnl
        fx_daily_pnl_cny = row.get("daily_fx_pnl_cny")
        if fx_daily_pnl_cny is None and str(row.get("currency") or "").upper() == "CNY":
            fx_daily_pnl_cny = Decimal(0)
        row["daily_total_pnl_cny"] = (cny_daily_pnl + _dec(fx_daily_pnl_cny)
                                      if cny_daily_pnl is not None and fx_daily_pnl_cny is not None else None)
        row["daily_total_pnl"] = native_daily_pnl + _dec(row.get("daily_fx_pnl"))

    return rows


def _date_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)[:10]


def _return_status_short_label(status: Any) -> str:
    normalized = normalize_return_status(status)
    if normalized == RETURN_STATUS_CONFIRMED:
        return "已确认"
    if normalized == RETURN_STATUS_ESTIMATED:
        return "预估"
    if normalized == RETURN_STATUS_MIXED:
        return "部分预估"
    return "未刷新"


def _holding_return_status_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("asset_id", "symbol", "name"):
        value = str(row.get(field) or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def _latest_return_status_map(cur, rows: list[dict[str, Any]], view_as_of: Any) -> dict[str, dict[str, Any]]:
    keys = sorted({key for row in rows for key in _holding_return_status_keys(row)})
    if not keys:
        return {}

    status_by_key: dict[str, dict[str, Any]] = {}
    view_key = _date_key(view_as_of)

    if _table_exists(cur, "market_observations"):
        cur.execute(
            """
            WITH latest AS (
                SELECT
                    COALESCE(NULLIF(asset_id, ''), NULLIF(symbol, ''), name) AS asset_key,
                    asset_id, symbol, name,
                    quote_date, return_status, source, provider, quote_session, market,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(NULLIF(asset_id, ''), NULLIF(symbol, ''), name)
                        ORDER BY quote_date DESC, updated_at DESC, quote_id DESC
                    ) AS rn
                FROM (SELECT * FROM market_observations WHERE observation_type = 'quote') AS observation_rows
                WHERE quote_date <= %s
                  AND (
                      asset_id = ANY(%s::text[])
                      OR symbol = ANY(%s::text[])
                      OR name = ANY(%s::text[])
                  )
            )
            SELECT *
            FROM latest
            WHERE rn = 1
            """,
            (view_as_of, keys, keys, keys),
        )
        for raw in cur.fetchall():
            row = dict(raw)
            status = normalize_return_status(
                row.get("return_status"),
                source=row.get("source") or row.get("provider"),
                market=row.get("market"),
                quote_session=row.get("quote_session"),
            )
            status_info = {
                "status": status,
                "label": return_status_label(status),
                "short_label": _return_status_short_label(status),
                "as_of": row.get("quote_date"),
                "source": row.get("source") or row.get("provider"),
            }
            for field in ("asset_key", "asset_id", "symbol", "name"):
                asset_key = str(row.get(field) or "").strip()
                if asset_key:
                    status_by_key[asset_key] = status_info

    missing_keys = [key for key in keys if key not in status_by_key]
    if missing_keys and _table_exists(cur, "market_observations"):
        cur.execute(
            """
            WITH latest AS (
                SELECT
                    COALESCE(NULLIF(asset_id, ''), NULLIF(symbol, ''), name) AS asset_key,
                    asset_id, symbol, name,
                    as_of AS quote_date, source, market,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(NULLIF(asset_id, ''), NULLIF(symbol, ''), name)
                        ORDER BY as_of DESC, updated_at DESC
                    ) AS rn
                FROM (SELECT * FROM market_observations WHERE observation_type = 'daily') AS observation_rows
                WHERE as_of <= %s
                  AND (
                      asset_id = ANY(%s::text[])
                      OR symbol = ANY(%s::text[])
                      OR name = ANY(%s::text[])
                  )
            )
            SELECT *
            FROM latest
            WHERE rn = 1
            """,
            (view_as_of, missing_keys, missing_keys, missing_keys),
        )
        for raw in cur.fetchall():
            row = dict(raw)
            status = normalize_return_status(
                source=row.get("source"),
                market=row.get("market"),
            )
            status_info = {
                "status": status,
                "label": return_status_label(status),
                "short_label": _return_status_short_label(status),
                "as_of": row.get("quote_date"),
                "source": row.get("source"),
            }
            for field in ("asset_key", "asset_id", "symbol", "name"):
                asset_key = str(row.get(field) or "").strip()
                if asset_key:
                    status_by_key[asset_key] = status_info

    # Rows beyond the requested date should never be shown as refreshed.
    if view_key:
        status_by_key = {
            key: value
            for key, value in status_by_key.items()
            if not value.get("as_of") or _date_key(value.get("as_of")) <= view_key
        }
    return status_by_key


def _apply_daily_return_status(cur, rows: list[dict[str, Any]], view_as_of: Any) -> list[dict[str, Any]]:
    status_by_key = _latest_return_status_map(cur, rows, view_as_of)
    for row in rows:
        if row.get("daily_pnl_stale"):
            status_info = {
                "status": RETURN_STATUS_PENDING,
                "label": return_status_label(RETURN_STATUS_PENDING),
                "short_label": _return_status_short_label(RETURN_STATUS_PENDING),
                "as_of": row.get("daily_pnl_as_of") or row.get("as_of") or row.get("snapshot_as_of"),
                "source": None,
            }
        else:
            status_info = next(
                (status_by_key[key] for key in _holding_return_status_keys(row) if key in status_by_key),
                {
                    "status": RETURN_STATUS_PENDING,
                    "label": return_status_label(RETURN_STATUS_PENDING),
                    "short_label": _return_status_short_label(RETURN_STATUS_PENDING),
                    "as_of": row.get("as_of") or row.get("snapshot_as_of"),
                    "source": None,
                },
            )
        if _date_key(status_info.get("as_of")) != _date_key(view_as_of):
            # A historical confirmed NAV does not confirm today's return.
            status_info = {
                **status_info,
                "status": RETURN_STATUS_PENDING,
                "label": return_status_label(RETURN_STATUS_PENDING),
                "short_label": _return_status_short_label(RETURN_STATUS_PENDING),
            }
        row["daily_return_status"] = status_info["status"]
        row["daily_return_status_label"] = status_info["label"]
        row["daily_return_status_short_label"] = status_info["short_label"]
        row["daily_return_status_as_of"] = status_info.get("as_of")
        row["daily_return_status_source"] = status_info.get("source")
        row["daily_pnl_status"] = status_info["status"]
        row["daily_pnl_status_label"] = status_info["label"]
    return rows


def _suppress_stale_daily_pnl(
    rows: list[dict[str, Any]],
    view_as_of: Any,
    daily_pnl_start_as_of: Any | None = None,
) -> list[dict[str, Any]]:
    """Do not report an older snapshot move as today's PnL.

    Fund NAVs and precious-metal marks can lag the page date by one or more
    days. Their market value is still the latest known value, but daily PnL is
    only "today" when the row snapshot date is inside the active daily-PnL
    window. Before the 07:00 Beijing rollover, the previous local date remains
    active while newly refreshed US rows on the current local date are also
    valid.
    """
    view_key = _date_key(view_as_of)
    if not view_key:
        return rows
    start_key = _date_key(daily_pnl_start_as_of) or view_key

    daily_fields = (
        "daily_pnl",
        "daily_pnl_cny",
        "daily_pnl_native",
        "daily_fx_pnl",
        "daily_fx_pnl_cny",
        "daily_total_pnl",
        "daily_total_pnl_cny",
        "price_change_pct",
        "price_change_amount",
    )
    for row in rows:
        row_key = _date_key(row.get("snapshot_as_of") or row.get("as_of"))
        if not row_key or start_key <= row_key <= view_key:
            continue
        for field in daily_fields:
            row[field] = None
        row["previous_price"] = None
        row["daily_pnl_stale"] = True
        row["daily_pnl_as_of"] = row.get("snapshot_as_of") or row.get("as_of")
    return rows








def build_api_holdings_latest_payload(
    cur,
    *,
    account_code: str | None,
    major_category: str | None,
    limit: int,
    view_as_of: Any | None = None,
) -> dict[str, Any]:
    """Return the legacy /api/holdings/latest payload."""
    where_clauses: list[str] = []
    params: list[Any] = []

    if account_code:
        where_clauses.append("account_code = %s")
        params.append(account_code)

    if major_category:
        where_clauses.append("major_category = %s")
        params.append(major_category)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    as_of_params = list(params)
    as_of_where_sql = where_sql
    if view_as_of is not None:
        as_of_where_sql = f"{where_sql} AND as_of <= %s"
        as_of_params.append(view_as_of)

    cur.execute(f"SELECT MAX(as_of) as as_of FROM pams_holdings_snapshot WHERE {as_of_where_sql}", as_of_params)
    row = cur.fetchone()
    as_of = row["as_of"] if row else None

    if as_of is None:
        return {"as_of": None, "rows": []}

    cur.execute(
        f"""
        SELECT
            id, symbol, name, asset_liability, currency, account_code,
            asset_id,
            major_category, minor_category, market_value, cost,
            CASE WHEN cost IS NULL THEN NULL ELSE holding_pnl END AS holding_pnl,
            as_of, created_at, updated_at
        FROM pams_holdings_snapshot
        WHERE {where_sql} AND as_of = %s
        ORDER BY market_value DESC
        LIMIT %s
        """,
        [*params, as_of, limit],
    )
    rows = _preserve_missing_cost(_apply_daily_return_status(cur, [dict(row) for row in cur.fetchall()], as_of))
    rows = annotate_snapshot_coverage(cur, rows)

    return {
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "rows": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }


def build_holdings_by_category_payload(
    cur,
    *,
    category: str,
    limit: int,
    minor_category: str | None,
    include_closed: bool,
    view_as_of,
    excluded_accounts: set[str],
    owner_ratios: dict[str, Decimal],
    default_owner_ratio: Decimal,
    daily_pnl_start_as_of: Any | None = None,
) -> dict[str, Any]:
    """Return the legacy /holdings/by-category/{category} payload."""
    cur.execute("SELECT MAX(as_of) AS as_of, to_regclass('market_observations') IS NOT NULL AS quote_available FROM pams_holdings_snapshot")
    row = cur.fetchone()
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {"as_of": None, "rows": []}

    quote_overlay = quote_valuation_cte(bool(row.get("quote_available")))
    price_as_of = quote_price_as_of(bool(row.get("quote_available")))
    ibkr_history_available = False
    ibkr_price_history = _ibkr_price_history_relation()
    if category == "其他":
        cur.execute(
            f"""
            WITH {LATEST_KNOWN_HOLDINGS_CTE}, latest_position AS (
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
            ), source_holdings AS (
                SELECT * FROM latest_known_holding
            ), {quote_overlay}, fx AS (
                SELECT quote_currency, rate, as_of
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                  AND as_of <= (SELECT as_of FROM snapshot_view_cutoff)
                  AND rate > 0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(source),'') IS NOT NULL
            )
            SELECT h.id AS holding_id, account_code AS account, minor_category, symbol, name,
                   h.asset_id,
                   h.asset_liability,
                   currency,
                   fx.as_of AS fx_rate_as_of,
                   CASE WHEN h.currency = 'CNY' THEN 'daily_exact' WHEN fx.rate IS NULL THEN 'unavailable'
                        WHEN fx.as_of = {price_as_of}::date THEN 'daily_exact' ELSE 'estimated' END AS fx_precision,
                   market_value,
                   market_value AS market_value_native,
                   CASE WHEN market_value=0 THEN 0 ELSE market_value * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) END AS market_value_cny,
                   cost,
                   cost * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) AS cost_cny,
                   CASE WHEN cost IS NULL THEN NULL ELSE holding_pnl END AS holding_pnl,
                   CASE WHEN cost IS NULL THEN NULL ELSE holding_pnl END AS holding_pnl_native,
                   CASE WHEN cost IS NULL THEN NULL ELSE holding_pnl * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) END AS holding_pnl_cny,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND h.current_price IS NOT NULL
                        AND prev_snap.current_price IS NOT NULL
                       THEN (h.current_price - prev_snap.current_price) * h.quantity
                       ELSE 0
                   END AS daily_pnl,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND h.current_price IS NOT NULL
                        AND prev_snap.current_price IS NOT NULL
                       THEN (h.current_price - prev_snap.current_price) * h.quantity * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END)
                       ELSE 0
                   END AS daily_pnl_cny,
                   major_category,
                   h.as_of,
                   {price_as_of} AS snapshot_as_of,
                   h.quantity, h.buy_price, h.current_price,
                   prev_snap.current_price AS previous_price,
                   CASE
                       WHEN h.current_price IS NOT NULL
                        AND prev_snap.current_price IS NOT NULL
                        AND prev_snap.current_price <> 0
                       THEN (h.current_price - prev_snap.current_price) / prev_snap.current_price
                       ELSE NULL
                   END AS price_change_pct,
                   CASE
                       WHEN h.current_price IS NOT NULL
                        AND prev_snap.current_price IS NOT NULL
                       THEN h.current_price - prev_snap.current_price
                       ELSE NULL
                   END AS price_change_amount,
                   {FUND_NAV_STATUS_COLUMNS}
            FROM h
            LEFT JOIN LATERAL (SELECT available.rate, available.as_of FROM fx available
                WHERE available.quote_currency=h.currency AND available.as_of <= {price_as_of}::date
                ORDER BY available.as_of DESC LIMIT 1) fx ON TRUE
            {FUND_NAV_STATUS_JOIN}
            {MARKET_DAILY_STATUS_JOIN}
            LEFT JOIN LATERAL (
                SELECT hs.current_price, hs.as_of
                FROM pams_holdings_snapshot hs
                WHERE hs.account_code = h.account_code
                  AND hs.as_of < h.as_of
                  AND hs.current_price IS NOT NULL
                  AND (
                      h.account_code <> 'ibkr'
                      OR hs.current_price IS DISTINCT FROM h.current_price
                      OR hs.quantity IS DISTINCT FROM h.quantity
                      OR hs.market_value IS DISTINCT FROM h.market_value
                  )
                  AND (
                      (COALESCE(h.symbol, '') <> '' AND hs.symbol = h.symbol)
                      OR (COALESCE(h.symbol, '') = '' AND hs.name = h.name)
                  )
                ORDER BY hs.as_of DESC
                LIMIT 1
            ) prev_snap ON TRUE
            WHERE asset_liability = '资产'
              AND major_category NOT IN ('A股', '美股', '加密', '贵金属')
            ORDER BY market_value_cny DESC NULLS LAST
            LIMIT %s
            """,
            (view_as_of, view_as_of, limit),
        )
    else:
        minor_filter_sql = "AND h.minor_category = %s" if minor_category else ""
        params: list[Any] = [view_as_of, view_as_of, category]
        if minor_category:
            params.append(minor_category)
        params.append(limit)
        cur.execute(
            f"""
            WITH {LATEST_KNOWN_HOLDINGS_CTE}, latest_position AS (
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
            ), source_holdings AS (
                SELECT * FROM latest_known_holding
            ), {quote_overlay}, fx AS (
                SELECT quote_currency, rate, as_of
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                  AND as_of <= (SELECT as_of FROM snapshot_view_cutoff)
                  AND rate > 0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(source),'') IS NOT NULL
            )
            SELECT h.id AS holding_id, account_code AS account, minor_category, symbol, name,
                   h.asset_id,
                   h.asset_liability,
                   currency,
                   fx.as_of AS fx_rate_as_of,
                   CASE WHEN h.currency = 'CNY' THEN 'daily_exact' WHEN fx.rate IS NULL THEN 'unavailable'
                        WHEN fx.as_of = {price_as_of}::date THEN 'daily_exact' ELSE 'estimated' END AS fx_precision,
                   eff.market_value AS market_value,
                   eff.market_value AS market_value_native,
                   CASE WHEN eff.market_value=0 THEN 0 ELSE eff.market_value * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) END AS market_value_cny,
                   cost,
                   cost * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) AS cost_cny,
                   eff.market_value - cost AS holding_pnl,
                   eff.market_value - cost AS holding_pnl_native,
                   (eff.market_value - cost) * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) AS holding_pnl_cny,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN (eff.current_price - eff.previous_price) * h.quantity
                       ELSE 0
                   END AS daily_pnl,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN (eff.current_price - eff.previous_price) * h.quantity * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END)
                       ELSE 0
                   END AS daily_pnl_cny,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN (eff.current_price - eff.previous_price) * h.quantity
                       ELSE 0
                   END AS daily_pnl_native,
                   CASE
                       WHEN h.major_category = '美股'
                        AND h.quantity IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN eff.previous_price * h.quantity * ((CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) - (CASE WHEN h.currency = 'CNY' THEN 1 ELSE prev_overseas_fx.rate END))
                       ELSE 0
                   END AS daily_fx_pnl_cny,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN (eff.current_price - eff.previous_price) * h.quantity * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END)
                       ELSE 0
                   END
                   + CASE
                       WHEN h.major_category = '美股'
                        AND h.quantity IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN eff.previous_price * h.quantity * ((CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) - (CASE WHEN h.currency = 'CNY' THEN 1 ELSE prev_overseas_fx.rate END))
                       ELSE 0
                   END AS daily_total_pnl_cny,
                   major_category,
                   h.as_of,
                   {price_as_of} AS snapshot_as_of,
                   h.quantity, h.buy_price, eff.current_price AS current_price,
                   eff.previous_price AS previous_price,
                   CASE
                       WHEN eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                        AND eff.previous_price <> 0
                       THEN (eff.current_price - eff.previous_price) / eff.previous_price
                       ELSE NULL
                   END AS price_change_pct,
                   CASE
                       WHEN eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN eff.current_price - eff.previous_price
                       ELSE NULL
                   END AS price_change_amount,
                   {FUND_NAV_STATUS_COLUMNS}
            FROM h
            LEFT JOIN LATERAL (SELECT available.rate, available.as_of FROM fx available
                WHERE available.quote_currency=h.currency AND available.as_of <= {price_as_of}::date
                ORDER BY available.as_of DESC LIMIT 1) fx ON TRUE
            {FUND_NAV_STATUS_JOIN}
            {MARKET_DAILY_STATUS_JOIN}
            LEFT JOIN LATERAL (
                SELECT hs.current_price, hs.as_of
                FROM pams_holdings_snapshot hs
                WHERE hs.account_code = h.account_code
                  AND hs.as_of < h.as_of
                  AND hs.current_price IS NOT NULL
                  AND (
                      h.account_code <> 'ibkr'
                      OR hs.current_price IS DISTINCT FROM h.current_price
                      OR hs.quantity IS DISTINCT FROM h.quantity
                      OR hs.market_value IS DISTINCT FROM h.market_value
                  )
                  AND (
                      (COALESCE(h.symbol, '') <> '' AND hs.symbol = h.symbol)
                      OR (COALESCE(h.symbol, '') = '' AND hs.name = h.name)
                  )
                ORDER BY hs.as_of DESC
                LIMIT 1
            ) prev_snap ON TRUE
            LEFT JOIN LATERAL (
                SELECT ph.current_price, ph.as_of
                FROM {ibkr_price_history} ph
                WHERE h.account_code = 'ibkr'
                  AND ph.account_code = h.account_code
                  AND ph.as_of < h.as_of
                  AND ph.current_price IS NOT NULL
                  AND (
                      ph.current_price IS DISTINCT FROM h.current_price
                      OR ph.quantity IS DISTINCT FROM h.quantity
                      OR ph.market_value IS DISTINCT FROM h.market_value
                  )
                  AND (
                      (COALESCE(h.symbol, '') <> '' AND ph.symbol = h.symbol)
                      OR (COALESCE(h.symbol, '') = '' AND ph.name = h.name)
                  )
                ORDER BY ph.as_of DESC
                LIMIT 1
            ) prev_ibkr_snap ON TRUE
            LEFT JOIN LATERAL (
                SELECT close
                FROM pams_ohlcv_daily o
                WHERE h.major_category = 'A股'
                  AND o.market = 'CN_A'
                  AND o.symbol = CASE
                      WHEN LEFT(h.symbol, 1) = '6' THEN 'sh' || h.symbol
                      ELSE 'sz' || h.symbol
                  END
                  AND o.trade_date < h.as_of
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) prev_px ON TRUE
            LEFT JOIN LATERAL (
                SELECT o.trade_date, o.close
                FROM pams_ohlcv_daily o
                WHERE h.major_category = '美股'
                  AND o.market = 'YF'
                  AND o.symbol = h.symbol
                  AND o.trade_date <= h.as_of
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) curr_overseas_px ON TRUE
            LEFT JOIN LATERAL (
                SELECT trade_date, close
                FROM pams_ohlcv_daily o
                WHERE h.major_category = '美股'
                  AND o.market = 'YF'
                  AND o.symbol = h.symbol
                  AND o.trade_date < curr_overseas_px.trade_date
                ORDER BY o.trade_date DESC
                LIMIT 1
            ) prev_overseas_px ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    CASE
                        WHEN h.major_category = '美股'
                         AND h.current_price IS NOT NULL
                         AND curr_overseas_px.close IS NOT NULL
                         AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                        THEN curr_overseas_px.close
                        ELSE COALESCE(
                            prev_px.close,
                            prev_overseas_px.close,
                            prev_ibkr_snap.current_price,
                            prev_snap.current_price
                        )
                    END AS previous_price,
                    CASE
                        WHEN h.major_category = '美股'
                         AND h.current_price IS NOT NULL
                         AND curr_overseas_px.close IS NOT NULL
                         AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                        THEN curr_overseas_px.trade_date
                        ELSE COALESCE(
                            prev_overseas_px.trade_date,
                            prev_ibkr_snap.as_of,
                            prev_snap.as_of
                        )
                    END AS previous_as_of
            ) prev_basis ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM pams_fx_rates_daily prev_fx
                WHERE prev_fx.base_currency = 'CNY'
                  AND prev_fx.quote_currency = h.currency
                  AND prev_basis.previous_as_of IS NOT NULL
                  AND prev_fx.as_of = prev_basis.previous_as_of
                  AND prev_fx.rate > 0 AND prev_fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(prev_fx.source),'') IS NOT NULL
                ORDER BY prev_fx.as_of DESC
                LIMIT 1
            ) prev_overseas_fx ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    CASE
                        WHEN h.major_category = '美股'
                         AND h.current_price IS NOT NULL
                        THEN h.current_price
                        ELSE h.current_price
                    END AS current_price,
                    prev_basis.previous_price AS previous_price,
                    CASE
                        WHEN h.major_category = '美股'
                         AND h.quantity IS NOT NULL
                         AND h.current_price IS NOT NULL
                        THEN h.current_price * h.quantity
                        ELSE h.market_value
                    END AS market_value
            ) eff ON TRUE
            WHERE major_category = %s
              {minor_filter_sql}
            ORDER BY market_value_cny DESC NULLS LAST
            LIMIT %s
            """,
            params,
        )

    current_rows = _filter_current_holding_rows(
        _enrich_esop_realized_income(cur, [dict(row) for row in cur.fetchall()]),
        excluded_accounts=excluded_accounts,
    )
    current_rows = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, current_rows)
    current_rows = _enrich_realized_trade_pnl(cur, current_rows)
    current_rows = enrich_trade_snapshot_costs(cur, current_rows)
    current_keys = {_holding_key(row) for row in current_rows}
    rows = [
        _apply_owner_ratio(
            row,
            owner_ratios=owner_ratios,
            default_owner_ratio=default_owner_ratio,
        )
        for row in current_rows
    ]
    closed_evidence: dict[str, str] = {}
    if include_closed and category != "其他":
        closed_rows = _closed_position_rows(
            cur,
            category=category,
            minor_category=minor_category,
            current_keys=current_keys,
            limit=limit,
        )
        if category == "A股" and minor_category in (None, "A股"):
            closed_evidence["tradingview"] = "unavailable_unverified_trade_coverage"
        if category == "美股" and minor_category in (None, "美股基金"):
            closed_rows.extend(_ibkr_closed_position_rows(cur, current_keys=current_keys, limit=limit))
            closed_evidence["ibkr"] = "confirmed_zero_only_coverage_unknown"
        closed_evidence["other"] = "unavailable_unverified_exit_coverage"
        rows.extend(
            _apply_owner_ratio(
                row,
                owner_ratios=owner_ratios,
                default_owner_ratio=default_owner_ratio,
            )
            for row in closed_rows
        )

    rows = _suppress_stale_daily_pnl(rows, view_as_of, daily_pnl_start_as_of)
    rows = _apply_daily_return_status(cur, rows, view_as_of)
    rows = _mark_missing_ibkr_daily_basis(rows, history_available=ibkr_history_available)
    rows = _mark_missing_a_stock_daily_basis(rows)
    rows = _preserve_missing_cost(rows)
    rows = annotate_snapshot_coverage(cur, rows)
    snapshot_dates = [row["as_of"] for row in rows if row.get("as_of") is not None]
    payload = {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "data_as_of_min": min(snapshot_dates).isoformat() if snapshot_dates else None,
        "data_as_of_max": max(snapshot_dates).isoformat() if snapshot_dates else None,
        "rows": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }
    if closed_evidence:
        payload["closed_positions_evidence"] = closed_evidence
    return payload


def build_holdings_history_payload(
    cur,
    *,
    category: str,
    days: int,
    interval: str,
    minor_category: str | None,
    view_as_of,
    tonghuashun_owner_ratio: Decimal,
    owner_ratio_sql: str,
    owner_ratio_params: tuple[Any, ...] | list[Any],
    excluded_accounts: list[str],
    base_currency: str = "CNY",
) -> dict[str, Any]:
    """Return the legacy /holdings/history/{category} payload."""
    cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {
            "category": category,
            "start": None,
            "end": None,
            "interval": interval,
            "base_currency": base_currency,
            "rows": [],
            "series": [],
        }

    start_date = view_as_of - timedelta(days=days)

    cur.execute("""SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS fact_exists,
        to_regclass('public.source_items') IS NOT NULL AS item_exists,
        to_regclass('public.source_batches') IS NOT NULL AS batch_exists""")
    tradingview_relations = cur.fetchone()
    fact_history = False
    if (tradingview_relations.get("fact_exists") and tradingview_relations.get("item_exists")
            and tradingview_relations.get("batch_exists")):
        cur.execute("""SELECT EXISTS (SELECT 1 FROM pams_investment_trade_fact f
            JOIN source_items si ON si.source_item_id=f.source_item_id
            JOIN source_batches sb ON sb.batch_id=si.batch_id
            WHERE f.source_table='tradingview_csv' AND f.account_code='tonghuashun'
              AND LOWER(COALESCE(f.side, '')) IN ('buy','sell')
              AND LOWER(f.review_status) IN ('auto','reviewed','confirmed')
              AND sb.account_code=f.account_code
              AND si.review_status='confirmed' AND sb.status='published') AS exists""")
        fact_history = _bool_field(cur.fetchone(), "exists")

    tv_history_relation = """(SELECT f.account_code, f.symbol AS normalized_symbol,
            f.trade_date AS closing_time, INITCAP(f.side) AS side, ABS(f.quantity) AS quantity
            FROM pams_investment_trade_fact f
            JOIN source_items si ON si.source_item_id=f.source_item_id
            JOIN source_batches sb ON sb.batch_id=si.batch_id
            WHERE f.source_table='tradingview_csv' AND f.account_code='tonghuashun'
              AND LOWER(f.review_status) IN ('auto','reviewed','confirmed')
              AND sb.account_code=f.account_code
              AND si.review_status='confirmed' AND sb.status='published')"""

    if category == "A股" and minor_category == "A股基金":
        cur.execute(
            """
            SELECT MIN(trade_date) AS start_date
            FROM pams_investment_trade_fact
            WHERE COALESCE(
                    NULLIF(asset_major_category, ''),
                    NULLIF(raw_json #>> '{source_row,major_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                  ) = %s
              AND COALESCE(
                    NULLIF(asset_minor_category, ''),
                    NULLIF(raw_json #>> '{source_row,minor_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                  ) = %s
            """,
            (category, minor_category),
        )
        row = cur.fetchone()
        if row and row["start_date"]:
            start_date = row["start_date"]
    elif category == "贵金属":
        cur.execute(
            """
            SELECT MIN(trade_date) AS start_date
            FROM pams_investment_trade_fact
            WHERE COALESCE(
                    NULLIF(asset_major_category, ''),
                    NULLIF(raw_json #>> '{source_row,major_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                  ) = %s
               OR account_code = 'ccb_gold_accumulation'
            """,
            (category,),
        )
        row = cur.fetchone()
        if row and row["start_date"]:
            start_date = row["start_date"]
    elif category == "ESOP":
        cur.execute(
            """
            SELECT MIN(start_date) AS start_date
            FROM (
                SELECT MIN(as_of) AS start_date
                FROM pams_holdings_snapshot
                WHERE major_category = 'ESOP'
                UNION ALL
                SELECT MIN(trade_date) AS start_date
                FROM pams_investment_trade_fact
                WHERE COALESCE(
                    NULLIF(asset_major_category, ''),
                    NULLIF(raw_json #>> '{source_row,major_category}', ''),
                    NULLIF(market, ''),
                    trade_type
                ) = %s
            ) s
            """,
            (category,),
        )
        row = cur.fetchone()
        if row and row["start_date"]:
            start_date = row["start_date"]

    if category == "A股" and (minor_category in (None, "A股")) and fact_history:
        tv_date_expr = "DATE_TRUNC('week', p.date)::date" if interval == "weekly" else "p.date"
        value_expression = ("CASE WHEN COUNT(*) FILTER (WHERE p.price IS NULL) > 0 THEN NULL "
                            "ELSE SUM(p.quantity * p.price) * %s END")
        cur.execute(
            f"""
            WITH bounds AS (
                SELECT MIN(closing_time::date) AS start_date
                FROM {tv_history_relation} tv
                WHERE account_code = 'tonghuashun'
                  AND side IN ('Buy', 'Sell')
            ), txn_symbols AS (
                SELECT DISTINCT
                    normalized_symbol,
                    CASE WHEN LEFT(normalized_symbol, 1) = '6'
                         THEN 'sh' || normalized_symbol
                         ELSE 'sz' || normalized_symbol
                    END AS internal_symbol
                FROM {tv_history_relation} tv
                WHERE account_code = 'tonghuashun'
                  AND normalized_symbol IS NOT NULL
            ), series_dates AS (
                SELECT DISTINCT o.trade_date AS date
                FROM pams_ohlcv_daily o
                JOIN txn_symbols s ON s.internal_symbol = o.symbol
                CROSS JOIN bounds b
                WHERE b.start_date IS NOT NULL
                  AND o.trade_date >= b.start_date
                  AND o.trade_date <= %s
                UNION
                SELECT %s::date AS date
            ), trade_events AS (
                SELECT
                    closing_time::date AS trade_date,
                    normalized_symbol,
                    CASE
                        WHEN side = 'Buy' THEN quantity
                        WHEN side = 'Sell' THEN -quantity
                        ELSE 0
                    END AS quantity_delta
                FROM {tv_history_relation} tv
                WHERE account_code = 'tonghuashun'
                  AND side IN ('Buy', 'Sell')
                  AND normalized_symbol IS NOT NULL
            ), current_marks AS (
                SELECT symbol, current_price
                FROM pams_holdings_snapshot
                WHERE account_code = 'tonghuashun'
                  AND major_category = 'A股'
                  AND minor_category = 'A股'
                  AND as_of = (
                      SELECT MAX(as_of)
                      FROM pams_holdings_snapshot
                      WHERE account_code = 'tonghuashun'
                        AND major_category = 'A股'
                        AND minor_category = 'A股'
                  )
            ), daily_positions AS (
                SELECT
                    d.date,
                    e.normalized_symbol,
                    SUM(e.quantity_delta) AS quantity
                FROM series_dates d
                JOIN trade_events e ON e.trade_date <= d.date
                GROUP BY d.date, e.normalized_symbol
                HAVING SUM(e.quantity_delta) <> 0
            ), priced_positions AS (
                SELECT
                    dp.date,
                    dp.quantity,
                    COALESCE(
                        CASE WHEN dp.date = %s::date THEN cm.current_price END,
                        px.close
                    ) AS price
                FROM daily_positions dp
                LEFT JOIN current_marks cm ON cm.symbol = dp.normalized_symbol
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
            )
            SELECT {tv_date_expr} AS date, {value_expression} AS value
            FROM priced_positions p
            GROUP BY {tv_date_expr}
            ORDER BY date
            """,
            (view_as_of, view_as_of, view_as_of, float(tonghuashun_owner_ratio)),
        )
    else:
        # A partial refresh must not erase older, still-known positions.  Scope
        # and FX are validated separately before this observed value is called
        # complete; an omitted row never proves an exit.
        if category == "其他":
            category_filter = "h.asset_liability = '资产' AND h.major_category NOT IN ('A股','美股','加密','贵金属')"
            category_params: list[Any] = []
        else:
            category_filter = "h.major_category = %s"
            category_params = [category]
            if minor_category:
                category_filter += " AND h.minor_category = %s"
                category_params.append(minor_category)
        cur.execute(
            f"""
            WITH series_dates AS (
                SELECT DISTINCT as_of AS date FROM pams_holdings_snapshot
                WHERE as_of >= %s AND as_of <= %s AND NOT (account_code = ANY(%s::text[]))
                UNION SELECT %s::date
            ), config AS (SELECT %s::text AS base_currency), h AS (
                SELECT DISTINCT ON (d.date, hs.account_code, hs.name, hs.currency)
                       d.date, hs.*
                FROM series_dates d JOIN pams_holdings_snapshot hs ON hs.as_of <= d.date
                WHERE NOT (hs.account_code = ANY(%s::text[]))
                ORDER BY d.date, hs.account_code, hs.name, hs.currency, hs.as_of DESC, hs.id DESC
            )
            SELECT h.date,h.as_of,h.account_code,h.market_value,
              CASE WHEN h.market_value=0 THEN 0 WHEN h.currency=config.base_currency
                     THEN h.market_value * {owner_ratio_sql}
                   WHEN fx.rate IS NOT NULL THEN h.market_value * fx.rate * {owner_ratio_sql}
                   WHEN h.currency='CNY' THEN h.market_value / NULLIF(base_fx.rate,0) * {owner_ratio_sql}
                   WHEN config.base_currency='CNY' THEN h.market_value * source_fx.rate * {owner_ratio_sql}
                   ELSE h.market_value * source_fx.rate / NULLIF(base_fx.rate,0) * {owner_ratio_sql}
              END AS value
            FROM h CROSS JOIN config
            LEFT JOIN LATERAL (SELECT rate FROM pams_fx_rates_daily r
              WHERE r.base_currency=config.base_currency AND r.quote_currency=h.currency AND r.as_of=h.date
                AND r.rate>0 AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1) fx ON TRUE
            LEFT JOIN LATERAL (SELECT rate FROM pams_fx_rates_daily r
              WHERE r.base_currency='CNY' AND r.quote_currency=h.currency AND r.as_of=h.date
                AND r.rate>0 AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1) source_fx ON TRUE
            LEFT JOIN LATERAL (SELECT rate FROM pams_fx_rates_daily r
              WHERE r.base_currency='CNY' AND r.quote_currency=config.base_currency AND r.as_of=h.date
                AND r.rate>0 AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1) base_fx ON TRUE
            WHERE {category_filter}
            ORDER BY h.date,h.account_code,h.name,h.currency
            """,
            (start_date, view_as_of, excluded_accounts, view_as_of, base_currency, excluded_accounts,
             *owner_ratio_params, *owner_ratio_params, *owner_ratio_params, *owner_ratio_params,
             *owner_ratio_params, *category_params),
        )
        observed = [dict(row) for row in cur.fetchall()]
        from domain.investments.snapshot_coverage import load_account_snapshot_coverage
        pairs = {(str(row['account_code']), row['as_of']) for row in observed}
        coverage = load_account_snapshot_coverage(cur, pairs)
        groups: dict[Any, dict[str, Any]] = {}
        for row in observed:
            day = row['date']
            bucket = groups.setdefault(day, {'known_value': Decimal(0), 'incomplete': False})
            if row['value'] is None:
                bucket['incomplete'] = True
            else:
                bucket['known_value'] += Decimal(str(row['value']))
            if (row['as_of'] != day or coverage.get((str(row['account_code']),row['as_of']), {}).get('status') != 'complete'):
                bucket['incomplete'] = True
        if interval == 'weekly':
            weekly: dict[Any, tuple[Any, dict[str, Any]]] = {}
            for day, bucket in sorted(groups.items()):
                week = day - timedelta(days=day.weekday())
                weekly[week] = (day, bucket)
            groups = {week: bucket for week, (_, bucket) in weekly.items()}
        series = [{"date": _norm(day), "value": None if data['incomplete'] else float(data['known_value']),
                   "known_value": float(data['known_value']),
                   "scope_status": "incomplete" if data['incomplete'] else "complete_for_observed_accounts"}
                  for day, data in sorted(groups.items())]
    if category == "A股" and (minor_category in (None, "A股")) and fact_history:
        rows = cur.fetchall()
        series = [{"date": _norm(row["date"]),
                   "value": None if row["value"] is None or base_currency != "CNY" else float(row["value"]),
                   **({"known_value": float(row["value"]), "known_value_currency": "CNY",
                       "fx_status": "unavailable"} if row["value"] is not None and base_currency != "CNY" else {})}
                  for row in rows]

    payload = {
        "category": category,
        "start": start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date),
        "end": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "interval": interval,
        "base_currency": base_currency,
        "rows": series,
        "series": series,
    }
    if category == "美股":
        payload["history_evidence"] = {
            "status": "snapshot_based" if series else "unavailable",
            "coverage": "unknown",
            "reason": "ibkr_position_history_unavailable",
        }
    if category == "A股" and minor_category in (None, "A股") and fact_history:
        payload["history_evidence"] = {"status": "estimated", "coverage": "unknown",
                                       "reason": "tradingview_report_coverage_unverified"}
    return payload


def build_holdings_latest_payload(
    cur,
    *,
    limit: int,
    include_closed: bool,
    view_as_of,
    excluded_accounts: set[str],
    owner_ratios: dict[str, Decimal],
    default_owner_ratio: Decimal,
    daily_pnl_start_as_of: Any | None = None,
) -> dict[str, Any]:
    """Return the legacy /holdings/latest payload for the investments page."""
    cur.execute("SELECT MAX(as_of) AS as_of, to_regclass('market_observations') IS NOT NULL AS quote_available FROM pams_holdings_snapshot")
    row = cur.fetchone()
    quote_overlay = quote_valuation_cte(bool(row and row.get("quote_available")))
    price_as_of = quote_price_as_of(bool(row and row.get("quote_available")))
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {"as_of": None, "rows": []}
    ibkr_history_available = False
    ibkr_price_history = _ibkr_price_history_relation()

    cur.execute(
        f"""
        WITH {LATEST_KNOWN_HOLDINGS_CTE}, latest_position AS (
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
        ), source_holdings AS (
            SELECT * FROM latest_known_holding
        ), {quote_overlay}, fx AS (
            SELECT quote_currency, rate, as_of
            FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY'
              AND as_of <= (SELECT as_of FROM snapshot_view_cutoff)
              AND rate > 0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
              AND NULLIF(BTRIM(source),'') IS NOT NULL
        )
        SELECT
            h.id AS holding_id, h.account_code AS account,
            h.minor_category AS type,
            h.symbol, h.name, h.asset_id, h.asset_liability, h.currency,
            fx.as_of AS fx_rate_as_of,
            CASE WHEN h.currency = 'CNY' THEN 'daily_exact' WHEN fx.rate IS NULL THEN 'unavailable'
                 WHEN fx.as_of = {price_as_of}::date THEN 'daily_exact' ELSE 'estimated' END AS fx_precision,
            eff.market_value AS market_value,
            eff.market_value AS market_value_native,
            CASE WHEN eff.market_value=0 THEN 0 ELSE eff.market_value * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) END AS market_value_cny,
            h.cost,
            h.cost * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) AS cost_cny,
            eff.market_value - h.cost AS holding_pnl,
            eff.market_value - h.cost AS holding_pnl_native,
            (eff.market_value - h.cost) * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) AS holding_pnl_cny,
            CASE
                WHEN h.quantity IS NOT NULL
                 AND eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN (eff.current_price - eff.previous_price) * h.quantity
                ELSE 0
            END AS daily_pnl,
            CASE
                WHEN h.quantity IS NOT NULL
                 AND eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN (eff.current_price - eff.previous_price) * h.quantity * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END)
                ELSE 0
            END AS daily_pnl_cny,
            CASE
                WHEN h.quantity IS NOT NULL
                 AND eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN (eff.current_price - eff.previous_price) * h.quantity
                ELSE 0
            END AS daily_pnl_native,
            CASE
                WHEN h.major_category = '美股'
                 AND h.quantity IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN eff.previous_price * h.quantity * ((CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) - (CASE WHEN h.currency = 'CNY' THEN 1 ELSE prev_overseas_fx.rate END))
                ELSE 0
            END AS daily_fx_pnl_cny,
            CASE
                WHEN h.quantity IS NOT NULL
                 AND eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN (eff.current_price - eff.previous_price) * h.quantity * (CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END)
                ELSE 0
            END
            + CASE
                WHEN h.major_category = '美股'
                 AND h.quantity IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN eff.previous_price * h.quantity * ((CASE WHEN h.currency = 'CNY' THEN 1 ELSE fx.rate END) - (CASE WHEN h.currency = 'CNY' THEN 1 ELSE prev_overseas_fx.rate END))
                ELSE 0
            END AS daily_total_pnl_cny,
            h.major_category, h.minor_category, h.as_of,
            {price_as_of} AS snapshot_as_of,
            h.quantity, h.buy_price, eff.current_price AS current_price,
            eff.previous_price AS previous_price,
            CASE
                WHEN eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                 AND eff.previous_price <> 0
                THEN (eff.current_price - eff.previous_price) / eff.previous_price
                ELSE NULL
            END AS price_change_pct,
            CASE
                WHEN eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN eff.current_price - eff.previous_price
                ELSE NULL
            END AS price_change_amount,
            {FUND_NAV_STATUS_COLUMNS}
        FROM h
        LEFT JOIN LATERAL (SELECT available.rate, available.as_of FROM fx available
                WHERE available.quote_currency=h.currency AND available.as_of <= {price_as_of}::date
                ORDER BY available.as_of DESC LIMIT 1) fx ON TRUE
        {FUND_NAV_STATUS_JOIN}
        {MARKET_DAILY_STATUS_JOIN}
        LEFT JOIN LATERAL (
            SELECT hs.current_price, hs.as_of
            FROM pams_holdings_snapshot hs
            WHERE hs.account_code = h.account_code
              AND hs.as_of < h.as_of
              AND hs.current_price IS NOT NULL
              AND (
                  h.account_code <> 'ibkr'
                  OR hs.current_price IS DISTINCT FROM h.current_price
                  OR hs.quantity IS DISTINCT FROM h.quantity
                  OR hs.market_value IS DISTINCT FROM h.market_value
              )
              AND (
                  (COALESCE(h.symbol, '') <> '' AND hs.symbol = h.symbol)
                  OR (COALESCE(h.symbol, '') = '' AND hs.name = h.name)
              )
            ORDER BY hs.as_of DESC
            LIMIT 1
        ) prev_snap ON TRUE
        LEFT JOIN LATERAL (
            SELECT ph.current_price, ph.as_of
            FROM {ibkr_price_history} ph
            WHERE h.account_code = 'ibkr'
              AND ph.account_code = h.account_code
              AND ph.as_of < h.as_of
              AND ph.current_price IS NOT NULL
              AND (
                  ph.current_price IS DISTINCT FROM h.current_price
                  OR ph.quantity IS DISTINCT FROM h.quantity
                  OR ph.market_value IS DISTINCT FROM h.market_value
              )
              AND (
                  (COALESCE(h.symbol, '') <> '' AND ph.symbol = h.symbol)
                  OR (COALESCE(h.symbol, '') = '' AND ph.name = h.name)
              )
            ORDER BY ph.as_of DESC
            LIMIT 1
        ) prev_ibkr_snap ON TRUE
        LEFT JOIN LATERAL (
            SELECT close
            FROM pams_ohlcv_daily o
            WHERE h.major_category = 'A股'
              AND o.market = 'CN_A'
              AND o.symbol = CASE
                  WHEN LEFT(h.symbol, 1) = '6' THEN 'sh' || h.symbol
                  ELSE 'sz' || h.symbol
              END
              AND o.trade_date < h.as_of
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) prev_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT o.trade_date, o.close
            FROM pams_ohlcv_daily o
            WHERE h.major_category = '美股'
              AND o.market = 'YF'
              AND o.symbol = h.symbol
              AND o.trade_date <= h.as_of
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) curr_overseas_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT trade_date, close
            FROM pams_ohlcv_daily o
            WHERE h.major_category = '美股'
              AND o.market = 'YF'
              AND o.symbol = h.symbol
              AND o.trade_date < curr_overseas_px.trade_date
            ORDER BY o.trade_date DESC
            LIMIT 1
        ) prev_overseas_px ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                CASE
                    WHEN h.major_category = '美股'
                     AND h.current_price IS NOT NULL
                     AND curr_overseas_px.close IS NOT NULL
                     AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                    THEN curr_overseas_px.close
                    ELSE COALESCE(
                        prev_px.close,
                        prev_overseas_px.close,
                        prev_ibkr_snap.current_price,
                        prev_snap.current_price
                    )
                END AS previous_price,
                CASE
                    WHEN h.major_category = '美股'
                     AND h.current_price IS NOT NULL
                     AND curr_overseas_px.close IS NOT NULL
                     AND h.current_price IS DISTINCT FROM curr_overseas_px.close
                    THEN curr_overseas_px.trade_date
                    ELSE COALESCE(
                        prev_overseas_px.trade_date,
                        prev_ibkr_snap.as_of,
                        prev_snap.as_of
                    )
                END AS previous_as_of
        ) prev_basis ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily prev_fx
            WHERE prev_fx.base_currency = 'CNY'
              AND prev_fx.quote_currency = h.currency
              AND prev_basis.previous_as_of IS NOT NULL
              AND prev_fx.as_of = prev_basis.previous_as_of
              AND prev_fx.rate > 0 AND prev_fx.rate::text NOT IN ('NaN','Infinity','-Infinity')
                  AND NULLIF(BTRIM(prev_fx.source),'') IS NOT NULL
            ORDER BY prev_fx.as_of DESC
            LIMIT 1
        ) prev_overseas_fx ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                CASE
                    WHEN h.major_category = '美股'
                     AND h.current_price IS NOT NULL
                    THEN h.current_price
                    ELSE h.current_price
                END AS current_price,
                prev_basis.previous_price AS previous_price,
                CASE
                    WHEN h.major_category = '美股'
                     AND h.quantity IS NOT NULL
                     AND h.current_price IS NOT NULL
                    THEN h.current_price * h.quantity
                    ELSE h.market_value
                END AS market_value
        ) eff ON TRUE
        ORDER BY market_value_cny DESC NULLS LAST
        LIMIT %s
        """,
        (view_as_of, view_as_of, limit),
    )
    current_rows = _filter_current_holding_rows(
        _enrich_esop_realized_income(cur, [dict(r) for r in cur.fetchall()]),
        excluded_accounts=excluded_accounts,
    )
    current_rows = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, current_rows)
    current_rows = _enrich_realized_trade_pnl(cur, current_rows)
    current_rows = enrich_trade_snapshot_costs(cur, current_rows)
    current_keys = {_holding_key(r) for r in current_rows}
    rows = [
        _apply_owner_ratio(
            r,
            owner_ratios=owner_ratios,
            default_owner_ratio=default_owner_ratio,
        )
        for r in current_rows
    ]
    closed_evidence: dict[str, str] = {}
    if include_closed:
        rows.extend(
            _apply_owner_ratio(
                r,
                owner_ratios=owner_ratios,
                default_owner_ratio=default_owner_ratio,
            )
            for r in _closed_position_rows(cur, current_keys=current_keys, limit=limit)
        )
        closed_evidence["tradingview"] = "unavailable_unverified_trade_coverage"
        rows.extend(
            _apply_owner_ratio(
                r,
                owner_ratios=owner_ratios,
                default_owner_ratio=default_owner_ratio,
            )
            for r in _ibkr_closed_position_rows(cur, current_keys=current_keys, limit=limit)
        )
        closed_evidence["ibkr"] = "confirmed_zero_only_coverage_unknown"
        closed_evidence["other"] = "unavailable_unverified_exit_coverage"

    rows = _suppress_stale_daily_pnl(rows, view_as_of, daily_pnl_start_as_of)
    rows = _apply_daily_return_status(cur, rows, view_as_of)
    rows = _mark_missing_ibkr_daily_basis(rows, history_available=ibkr_history_available)
    rows = _mark_missing_a_stock_daily_basis(rows)
    rows = _preserve_missing_cost(rows)
    rows = annotate_snapshot_coverage(cur, rows)
    snapshot_dates = [r["as_of"] for r in rows if r.get("as_of") is not None]
    payload = {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "data_as_of_min": min(snapshot_dates).isoformat() if snapshot_dates else None,
        "data_as_of_max": max(snapshot_dates).isoformat() if snapshot_dates else None,
        "rows": [{k: _norm(v) for k, v in dict(r).items()} for r in rows],
    }
    if closed_evidence:
        payload["closed_positions_evidence"] = closed_evidence
    return payload
