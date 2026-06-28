"""Holdings page read adapters.

The functions in this module keep the legacy holdings payload shape while
moving display-read logic out of ``main.py``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from collections.abc import Mapping
from typing import Any

from services.daily_return_state import (
    RETURN_STATUS_CONFIRMED,
    RETURN_STATUS_ESTIMATED,
    RETURN_STATUS_MIXED,
    RETURN_STATUS_PENDING,
    normalize_return_status,
    return_status_label,
)
from services.holdings_positions import (
    _apply_owner_ratio,
    _closed_position_rows,
    _dec,
    _enrich_esop_realized_income,
    _enrich_realized_trade_pnl,
    _filter_current_holding_rows,
    _holding_key,
    _ibkr_closed_position_rows,
    _norm,
    _tradingview_closed_position_rows,
)


COMPLETE_ACCOUNT_SNAPSHOT_CTE = """
account_snapshot_stats AS (
    SELECT account_code, as_of, COUNT(*) AS row_count
    FROM pams_holdings_snapshot
    WHERE as_of <= %s
    GROUP BY account_code, as_of
), complete_account_snapshot AS (
    SELECT s.account_code, s.as_of, s.row_count
    FROM account_snapshot_stats s
    LEFT JOIN LATERAL (
        SELECT MAX(prev.row_count) AS reference_row_count
        FROM account_snapshot_stats prev
        WHERE prev.account_code = s.account_code
          AND prev.as_of < s.as_of
          AND prev.as_of >= s.as_of - INTERVAL '14 days'
    ) ref ON TRUE
    WHERE ref.reference_row_count IS NULL
       OR s.row_count >= GREATEST(1, CEIL(ref.reference_row_count * 0.5)::int)
), latest_account AS (
    SELECT account_code, MAX(as_of) AS account_as_of
    FROM complete_account_snapshot
    GROUP BY account_code
)
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
    dates = sorted({as_of for _, _, as_of in keys})

    if _trade_fact_rows_available(cur):
        cur.execute(
            """
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
            GROUP BY account_code, symbol, trade_date
            """,
            (accounts, symbols, dates),
        )
    else:
        cur.execute(
            """
            SELECT
                account_code,
                normalized_symbol,
                closing_time::date AS as_of,
                SUM(CASE WHEN LOWER(side) = 'buy' THEN quantity ELSE 0 END) AS buy_qty,
                SUM(CASE WHEN LOWER(side) = 'buy' THEN quantity * fill_price ELSE 0 END) AS buy_value
            FROM pams_tradingview_transactions
            WHERE account_code = ANY(%s::text[])
              AND normalized_symbol = ANY(%s::text[])
              AND closing_time::date = ANY(%s::date[])
            GROUP BY account_code, normalized_symbol, closing_time::date
            """,
            (accounts, symbols, dates),
        )
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
        cny_before = _dec(row.get("daily_pnl_cny"))
        if native_before != 0:
            fx_rate = cny_before / native_before
        else:
            market_native = _dec(row.get("market_value"))
            market_cny = _dec(row.get("market_value_cny"))
            fx_rate = market_cny / market_native if market_native != 0 else Decimal("1")

        row["daily_pnl"] = native_daily_pnl
        row["daily_pnl_native"] = native_daily_pnl
        cny_daily_pnl = native_daily_pnl * fx_rate
        row["daily_pnl_cny"] = cny_daily_pnl
        fx_daily_pnl_cny = _dec(row.get("daily_fx_pnl_cny"))
        row["daily_total_pnl_cny"] = cny_daily_pnl + fx_daily_pnl_cny
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

    if _table_exists(cur, "pams_market_quote_snapshot"):
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
                FROM pams_market_quote_snapshot
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
    if missing_keys and _table_exists(cur, "pams_asset_market_daily"):
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
                FROM pams_asset_market_daily
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
        row["daily_return_status"] = status_info["status"]
        row["daily_return_status_label"] = status_info["label"]
        row["daily_return_status_short_label"] = status_info["short_label"]
        row["daily_return_status_as_of"] = status_info.get("as_of")
        row["daily_return_status_source"] = status_info.get("source")
    return rows


def _drop_superseded_account_snapshot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop stale rows superseded by a newer account-level position snapshot.

    A-share brokers and fund platforms are imported as account snapshots. If a
    later snapshot for the same account and bucket exists, symbols missing from
    that snapshot should not be carried forward as current holdings.
    """
    latest_by_account: dict[str, str] = {}
    latest_by_bucket: dict[tuple[str, str, str, str], str] = {}
    for row in rows:
        if not _is_account_snapshot_bucket(row):
            continue
        account = str(row.get("account") or row.get("account_code") or "")
        row_key = _date_key(row.get("as_of") or row.get("snapshot_as_of"))
        if account and row_key and row_key > latest_by_account.get(account, ""):
            latest_by_account[account] = row_key
        minor = row.get("minor_category") or row.get("type")
        bucket = (
            account,
            str(row.get("asset_liability") or ""),
            str(row.get("major_category") or ""),
            str(minor or ""),
        )
        if row_key and row_key > latest_by_bucket.get(bucket, ""):
            latest_by_bucket[bucket] = row_key

    if not latest_by_account and not latest_by_bucket:
        return rows

    out: list[dict[str, Any]] = []
    for row in rows:
        if not _is_account_snapshot_bucket(row):
            out.append(row)
            continue
        account = str(row.get("account") or row.get("account_code") or "")
        row_key = _date_key(row.get("as_of") or row.get("snapshot_as_of"))
        account_latest_key = latest_by_account.get(account)
        if account_latest_key and row_key and row_key < account_latest_key:
            row["is_superseded_snapshot"] = True
            continue
        minor = row.get("minor_category") or row.get("type")
        bucket = (
            account,
            str(row.get("asset_liability") or ""),
            str(row.get("major_category") or ""),
            str(minor or ""),
        )
        latest_key = latest_by_bucket.get(bucket)
        if latest_key and row_key and row_key < latest_key:
            row["is_superseded_snapshot"] = True
            continue
        out.append(row)
    return out


def _is_account_snapshot_bucket(row: dict[str, Any]) -> bool:
    return bool(row.get("account") or row.get("account_code"))


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

    zero = Decimal("0")
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
        row_key = _date_key(row.get("as_of") or row.get("snapshot_as_of"))
        if not row_key or start_key <= row_key <= view_key:
            continue
        for field in daily_fields:
            row[field] = zero
        if row.get("current_price") is not None:
            row["previous_price"] = row.get("current_price")
        row["daily_pnl_stale"] = True
        row["daily_pnl_as_of"] = row.get("as_of") or row.get("snapshot_as_of")
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
            major_category, minor_category, market_value, cost, holding_pnl,
            as_of, created_at, updated_at
        FROM pams_holdings_snapshot
        WHERE {where_sql} AND as_of = %s
        ORDER BY market_value DESC
        LIMIT %s
        """,
        [*params, as_of, limit],
    )
    rows = _apply_daily_return_status(cur, [dict(row) for row in cur.fetchall()], as_of)

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
    cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {"as_of": None, "rows": []}

    if category == "其他":
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
                JOIN latest_account la
                  ON la.account_code = hs.account_code
                 AND la.account_as_of = hs.as_of
                JOIN latest_position lp
                  ON lp.account_code = hs.account_code
                 AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
                 AND lp.as_of = hs.as_of
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                ORDER BY quote_currency, as_of DESC
            )
            SELECT account_code AS account, minor_category, symbol, name,
                   h.asset_liability,
                   currency,
                   market_value,
                   market_value AS market_value_native,
                   market_value * COALESCE(fx.rate, 1) AS market_value_cny,
                   cost,
                   cost * COALESCE(fx.rate, 1) AS cost_cny,
                   COALESCE(holding_pnl, 0) AS holding_pnl,
                   COALESCE(holding_pnl, 0) AS holding_pnl_native,
                   COALESCE(holding_pnl, 0) * COALESCE(fx.rate, 1) AS holding_pnl_cny,
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
                       THEN (h.current_price - prev_snap.current_price) * h.quantity * COALESCE(fx.rate, 1)
                       ELSE 0
                   END AS daily_pnl_cny,
                   major_category,
                   h.as_of,
                   h.as_of AS snapshot_as_of,
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
                   END AS price_change_amount
            FROM h
            LEFT JOIN fx ON fx.quote_currency = h.currency
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
                JOIN latest_account la
                  ON la.account_code = hs.account_code
                 AND la.account_as_of = hs.as_of
                JOIN latest_position lp
                  ON lp.account_code = hs.account_code
                 AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
                 AND lp.as_of = hs.as_of
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                ORDER BY quote_currency, as_of DESC
            )
            SELECT account_code AS account, minor_category, symbol, name,
                   h.asset_liability,
                   currency,
                   eff.market_value AS market_value,
                   eff.market_value AS market_value_native,
                   eff.market_value * COALESCE(fx.rate, 1) AS market_value_cny,
                   cost,
                   cost * COALESCE(fx.rate, 1) AS cost_cny,
                   eff.market_value - COALESCE(cost, 0) AS holding_pnl,
                   eff.market_value - COALESCE(cost, 0) AS holding_pnl_native,
                   (eff.market_value - COALESCE(cost, 0)) * COALESCE(fx.rate, 1) AS holding_pnl_cny,
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
                       THEN (eff.current_price - eff.previous_price) * h.quantity * COALESCE(fx.rate, 1)
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
                       THEN eff.previous_price * h.quantity * (COALESCE(fx.rate, 1) - COALESCE(prev_overseas_fx.rate, fx.rate, 1))
                       ELSE 0
                   END AS daily_fx_pnl_cny,
                   CASE
                       WHEN h.quantity IS NOT NULL
                        AND eff.current_price IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN (eff.current_price - eff.previous_price) * h.quantity * COALESCE(fx.rate, 1)
                       ELSE 0
                   END
                   + CASE
                       WHEN h.major_category = '美股'
                        AND h.quantity IS NOT NULL
                        AND eff.previous_price IS NOT NULL
                       THEN eff.previous_price * h.quantity * (COALESCE(fx.rate, 1) - COALESCE(prev_overseas_fx.rate, fx.rate, 1))
                       ELSE 0
                   END AS daily_total_pnl_cny,
                   major_category,
                   h.as_of,
                   h.as_of AS snapshot_as_of,
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
                   END AS price_change_amount
            FROM h
            LEFT JOIN fx ON fx.quote_currency = h.currency
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
                FROM pams_ibkr_position_history ph
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
                SELECT rate
                FROM pams_fx_rates_daily prev_fx
                WHERE prev_fx.base_currency = 'CNY'
                  AND prev_fx.quote_currency = h.currency
                  AND COALESCE(
                      prev_overseas_px.trade_date,
                      prev_ibkr_snap.as_of,
                      prev_snap.as_of
                  ) IS NOT NULL
                  AND prev_fx.as_of <= COALESCE(
                      prev_overseas_px.trade_date,
                      prev_ibkr_snap.as_of,
                      prev_snap.as_of
                  )
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
                    COALESCE(
                        prev_px.close,
                        prev_overseas_px.close,
                        prev_ibkr_snap.current_price,
                        prev_snap.current_price
                    ) AS previous_price,
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
    current_rows = _drop_superseded_account_snapshot_rows(current_rows)
    current_rows = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, current_rows)
    current_rows = _enrich_realized_trade_pnl(cur, current_rows)
    current_keys = {_holding_key(row) for row in current_rows}
    rows = [
        _apply_owner_ratio(
            row,
            owner_ratios=owner_ratios,
            default_owner_ratio=default_owner_ratio,
        )
        for row in current_rows
    ]
    if include_closed and category != "其他":
        closed_rows = _closed_position_rows(
            cur,
            category=category,
            minor_category=minor_category,
            current_keys=current_keys,
            limit=limit,
        )
        if category == "A股" and minor_category in (None, "A股"):
            cur.execute("SELECT to_regclass('public.pams_tradingview_transactions') IS NOT NULL AS exists")
            has_tradingview_transactions = _bool_field(cur.fetchone(), "exists")
            if has_tradingview_transactions:
                closed_rows.extend(
                    _tradingview_closed_position_rows(
                        cur,
                        current_keys=current_keys,
                        daily_pnl_as_of=view_as_of,
                        limit=limit,
                    )
                )
        if category == "美股" and minor_category in (None, "美股基金"):
            cur.execute("SELECT to_regclass('public.pams_ibkr_trades') IS NOT NULL AS exists")
            has_ibkr_trades = _bool_field(cur.fetchone(), "exists")
            if has_ibkr_trades:
                closed_rows.extend(_ibkr_closed_position_rows(cur, current_keys=current_keys, limit=limit))
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
    snapshot_dates = [row["as_of"] for row in rows if row.get("as_of") is not None]
    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "data_as_of_min": min(snapshot_dates).isoformat() if snapshot_dates else None,
        "data_as_of_max": max(snapshot_dates).isoformat() if snapshot_dates else None,
        "rows": [{key: _norm(value) for key, value in dict(row).items()} for row in rows],
    }


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
    date_expr = "DATE_TRUNC('week', h.date)::date" if interval == "weekly" else "h.date"

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

    cur.execute("SELECT to_regclass('public.pams_ibkr_position_history') IS NOT NULL AS exists")
    has_ibkr_position_history = bool(cur.fetchone()["exists"])
    cur.execute("SELECT to_regclass('public.pams_tradingview_transactions') IS NOT NULL AS exists")
    has_tradingview_transactions = bool(cur.fetchone()["exists"])

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
    elif category == "美股" and has_ibkr_position_history:
        cur.execute(
            """
            SELECT MIN(as_of) AS start_date
            FROM pams_ibkr_position_history
            WHERE account_code = 'ibkr'
              AND major_category = '美股'
            """
        )
        row = cur.fetchone()
        if row and row["start_date"]:
            start_date = row["start_date"]

    if category == "A股" and (minor_category in (None, "A股")) and has_tradingview_transactions:
        tv_date_expr = "DATE_TRUNC('week', p.date)::date" if interval == "weekly" else "p.date"
        cur.execute(
            f"""
            WITH bounds AS (
                SELECT MIN(closing_time::date) AS start_date
                FROM pams_tradingview_transactions
                WHERE account_code = 'tonghuashun'
                  AND side IN ('Buy', 'Sell')
            ), txn_symbols AS (
                SELECT DISTINCT
                    normalized_symbol,
                    CASE WHEN LEFT(normalized_symbol, 1) = '6'
                         THEN 'sh' || normalized_symbol
                         ELSE 'sz' || normalized_symbol
                    END AS internal_symbol
                FROM pams_tradingview_transactions
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
                FROM pams_tradingview_transactions
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
                        px.close,
                        cm.current_price,
                        0
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
            SELECT {tv_date_expr} AS date, SUM(p.quantity * p.price) * %s AS value
            FROM priced_positions p
            GROUP BY {tv_date_expr}
            ORDER BY date
            """,
            (view_as_of, view_as_of, view_as_of, float(tonghuashun_owner_ratio)),
        )
    elif category == "美股" and has_ibkr_position_history:
        cur.execute(
            f"""
            WITH series_dates AS (
                SELECT DISTINCT as_of AS date
                FROM pams_ibkr_position_history
                WHERE as_of >= %s AND as_of <= %s
                  AND account_code = 'ibkr'
                  AND NOT (account_code = ANY(%s::text[]))
                UNION
                SELECT %s::date AS date
            ), account_latest AS (
                SELECT d.date, ph.account_code, MAX(ph.as_of) AS as_of
                FROM series_dates d
                JOIN pams_ibkr_position_history ph ON ph.as_of <= d.date
                WHERE ph.account_code = 'ibkr'
                  AND NOT (ph.account_code = ANY(%s::text[]))
                GROUP BY d.date, ph.account_code
            ), h AS (
                SELECT al.date, ph.*
                FROM account_latest al
                JOIN pams_ibkr_position_history ph
                  ON ph.account_code = al.account_code
                 AND ph.as_of = al.as_of
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                ORDER BY quote_currency, as_of DESC
            )
            SELECT {date_expr} AS date, SUM(h.market_value * COALESCE(fx.rate, 1) * {owner_ratio_sql}) AS value
            FROM h
            LEFT JOIN fx ON fx.quote_currency = h.currency
            WHERE h.major_category = '美股'
              AND h.asset_liability = '资产'
              AND NOT (h.account_code = ANY(%s::text[]))
            GROUP BY {date_expr}
            ORDER BY date
            """,
            (start_date, view_as_of, excluded_accounts, view_as_of, excluded_accounts, *owner_ratio_params, excluded_accounts),
        )
    elif category == "其他":
        cur.execute(
            f"""
            WITH series_dates AS (
                SELECT DISTINCT hs.as_of AS date
                FROM pams_holdings_snapshot hs
                WHERE hs.as_of >= %s AND hs.as_of <= %s
                  AND NOT (hs.account_code = ANY(%s::text[]))
                UNION
                SELECT %s::date AS date
            ), account_latest AS (
                SELECT d.date, hs.account_code, MAX(hs.as_of) AS as_of
                FROM series_dates d
                JOIN pams_holdings_snapshot hs ON hs.as_of <= d.date
                WHERE NOT (hs.account_code = ANY(%s::text[]))
                GROUP BY d.date, hs.account_code
            ), h AS (
                SELECT al.date, hs.*
                FROM account_latest al
                JOIN pams_holdings_snapshot hs
                  ON hs.account_code = al.account_code
                 AND hs.as_of = al.as_of
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                ORDER BY quote_currency, as_of DESC
            )
            SELECT {date_expr} AS date, SUM(h.market_value * COALESCE(fx.rate, 1) * {owner_ratio_sql}) AS value
            FROM h
            LEFT JOIN fx ON fx.quote_currency = h.currency
            WHERE h.asset_liability = '资产'
              AND h.major_category NOT IN ('A股', '美股', '加密', '贵金属')
              AND NOT (h.account_code = ANY(%s::text[]))
            GROUP BY {date_expr}
            ORDER BY date
            """,
            (start_date, view_as_of, excluded_accounts, view_as_of, excluded_accounts, *owner_ratio_params, excluded_accounts),
        )
    else:
        minor_filter_sql = "AND h.minor_category = %s" if minor_category else ""
        params = [
            start_date,
            view_as_of,
            excluded_accounts,
            view_as_of,
            excluded_accounts,
            *owner_ratio_params,
            category,
        ]
        if minor_category:
            params.append(minor_category)
        params.append(excluded_accounts)
        cur.execute(
            f"""
            WITH series_dates AS (
                SELECT DISTINCT hs.as_of AS date
                FROM pams_holdings_snapshot hs
                WHERE hs.as_of >= %s AND hs.as_of <= %s
                  AND NOT (hs.account_code = ANY(%s::text[]))
                UNION
                SELECT %s::date AS date
            ), account_latest AS (
                SELECT d.date, hs.account_code, MAX(hs.as_of) AS as_of
                FROM series_dates d
                JOIN pams_holdings_snapshot hs ON hs.as_of <= d.date
                WHERE NOT (hs.account_code = ANY(%s::text[]))
                GROUP BY d.date, hs.account_code
            ), h AS (
                SELECT al.date, hs.*
                FROM account_latest al
                JOIN pams_holdings_snapshot hs
                  ON hs.account_code = al.account_code
                 AND hs.as_of = al.as_of
            ), fx AS (
                SELECT DISTINCT ON (quote_currency) quote_currency, rate
                FROM pams_fx_rates_daily
                WHERE base_currency = 'CNY'
                ORDER BY quote_currency, as_of DESC
            )
            SELECT {date_expr} AS date, SUM(h.market_value * COALESCE(fx.rate, 1) * {owner_ratio_sql}) AS value
            FROM h
            LEFT JOIN fx ON fx.quote_currency = h.currency
            WHERE h.major_category = %s
              {minor_filter_sql}
              AND NOT (h.account_code = ANY(%s::text[]))
            GROUP BY {date_expr}
            ORDER BY date
            """,
            params,
        )
    rows = cur.fetchall()
    series = [{"date": _norm(row["date"]), "value": float(row["value"] or 0)} for row in rows]

    return {
        "category": category,
        "start": start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date),
        "end": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "interval": interval,
        "base_currency": base_currency,
        "rows": series,
        "series": series,
    }


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
    cur.execute("SELECT MAX(as_of) AS as_of FROM pams_holdings_snapshot")
    row = cur.fetchone()
    max_snapshot_as_of = row["as_of"] if row else None
    if max_snapshot_as_of is None:
        return {"as_of": None, "rows": []}

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
            JOIN latest_account la
              ON la.account_code = hs.account_code
             AND la.account_as_of = hs.as_of
            JOIN latest_position lp
              ON lp.account_code = hs.account_code
             AND lp.position_key = COALESCE(NULLIF(hs.symbol, ''), hs.name)
             AND lp.as_of = hs.as_of
        ), fx AS (
            SELECT DISTINCT ON (quote_currency) quote_currency, rate
            FROM pams_fx_rates_daily
            WHERE base_currency = 'CNY'
            ORDER BY quote_currency, as_of DESC
        )
        SELECT
            h.account_code AS account,
            h.minor_category AS type,
            h.symbol, h.name, h.asset_liability, h.currency,
            eff.market_value AS market_value,
            eff.market_value AS market_value_native,
            eff.market_value * COALESCE(fx.rate, 1) AS market_value_cny,
            h.cost,
            h.cost * COALESCE(fx.rate, 1) AS cost_cny,
            eff.market_value - COALESCE(h.cost, 0) AS holding_pnl,
            eff.market_value - COALESCE(h.cost, 0) AS holding_pnl_native,
            (eff.market_value - COALESCE(h.cost, 0)) * COALESCE(fx.rate, 1) AS holding_pnl_cny,
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
                THEN (eff.current_price - eff.previous_price) * h.quantity * COALESCE(fx.rate, 1)
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
                THEN eff.previous_price * h.quantity * (COALESCE(fx.rate, 1) - COALESCE(prev_overseas_fx.rate, fx.rate, 1))
                ELSE 0
            END AS daily_fx_pnl_cny,
            CASE
                WHEN h.quantity IS NOT NULL
                 AND eff.current_price IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN (eff.current_price - eff.previous_price) * h.quantity * COALESCE(fx.rate, 1)
                ELSE 0
            END
            + CASE
                WHEN h.major_category = '美股'
                 AND h.quantity IS NOT NULL
                 AND eff.previous_price IS NOT NULL
                THEN eff.previous_price * h.quantity * (COALESCE(fx.rate, 1) - COALESCE(prev_overseas_fx.rate, fx.rate, 1))
                ELSE 0
            END AS daily_total_pnl_cny,
            h.major_category, h.minor_category, h.as_of,
            h.as_of AS snapshot_as_of,
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
            END AS price_change_amount
        FROM h
        LEFT JOIN fx ON fx.quote_currency = h.currency
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
            FROM pams_ibkr_position_history ph
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
            SELECT rate
            FROM pams_fx_rates_daily prev_fx
            WHERE prev_fx.base_currency = 'CNY'
              AND prev_fx.quote_currency = h.currency
              AND COALESCE(
                  prev_overseas_px.trade_date,
                  prev_ibkr_snap.as_of,
                  prev_snap.as_of
              ) IS NOT NULL
              AND prev_fx.as_of <= COALESCE(
                  prev_overseas_px.trade_date,
                  prev_ibkr_snap.as_of,
                  prev_snap.as_of
              )
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
                COALESCE(
                    prev_px.close,
                    prev_overseas_px.close,
                    prev_ibkr_snap.current_price,
                    prev_snap.current_price
                ) AS previous_price,
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
    current_rows = _drop_superseded_account_snapshot_rows(current_rows)
    current_rows = _adjust_a_stock_daily_pnl_for_same_day_buys(cur, current_rows)
    current_rows = _enrich_realized_trade_pnl(cur, current_rows)
    current_keys = {_holding_key(r) for r in current_rows}
    rows = [
        _apply_owner_ratio(
            r,
            owner_ratios=owner_ratios,
            default_owner_ratio=default_owner_ratio,
        )
        for r in current_rows
    ]
    if include_closed:
        rows.extend(
            _apply_owner_ratio(
                r,
                owner_ratios=owner_ratios,
                default_owner_ratio=default_owner_ratio,
            )
            for r in _closed_position_rows(cur, current_keys=current_keys, limit=limit)
        )
        cur.execute("SELECT to_regclass('public.pams_ibkr_trades') IS NOT NULL AS exists")
        has_ibkr_trades = _bool_field(cur.fetchone(), "exists")
        if has_ibkr_trades:
            rows.extend(
                _apply_owner_ratio(
                    r,
                    owner_ratios=owner_ratios,
                    default_owner_ratio=default_owner_ratio,
            )
                for r in _ibkr_closed_position_rows(cur, current_keys=current_keys, limit=limit)
            )

    rows = _suppress_stale_daily_pnl(rows, view_as_of, daily_pnl_start_as_of)
    rows = _apply_daily_return_status(cur, rows, view_as_of)
    snapshot_dates = [r["as_of"] for r in rows if r.get("as_of") is not None]
    return {
        "as_of": view_as_of.isoformat() if hasattr(view_as_of, "isoformat") else str(view_as_of),
        "data_as_of_min": min(snapshot_dates).isoformat() if snapshot_dates else None,
        "data_as_of_max": max(snapshot_dates).isoformat() if snapshot_dates else None,
        "rows": [{k: _norm(v) for k, v in dict(r).items()} for r in rows],
    }
