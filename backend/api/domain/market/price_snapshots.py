"""
持仓行情快照写入、A股当日交易 roll-forward 与海外行情调度入库。
"""
import os
import json as json_mod
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import psycopg2.extras
from domain.imports.isolation import source_enabled
from domain.investments.holdings.holding_evidence_writer import (
    finish_holding_change,
    lock_holding_accounts,
    prepare_holding_change,
)
from domain.investments.returns.daily_return_state import quote_snapshot_from_market_daily, upsert_quote_snapshot
from domain.investments.returns.investment_view_date import daily_pnl_window_date as _daily_pnl_window_date
from domain.brokers.ibkr.ibkr_report_state import expected_statement_date
from domain.market.price_providers import (
    _A_STOCK_FUND_MINOR_CATEGORY,
    _A_STOCK_MAJOR_CATEGORY,
    _A_STOCK_MINOR_CATEGORY,
    _HIGH_CONFIDENCE_US_ESTIMATE_SOURCES,
    _OVERSEAS_MAJOR_CATEGORY,
    _US_ESTIMATE_QUOTE_CACHE,
    _YFINANCE_CLOSE_CACHE,
    _a_stock_quote_symbol,
    _fetch_us_stock_prices_yf,
    _get_conn,
    _yfinance_quote_symbol,
    logger,
)


def _today_cst():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _price_refresh_as_of(category: str, price_basis: str = "close") -> date:
    """Return the snapshot date for a price refresh.

    Overseas daily PnL rolls at 07:00 Beijing time because US market refreshes
    happen after local midnight. Writing US estimates/closes to the calendar
    date before that cutoff makes the holdings page compare the wrong bucket.
    """
    if category == _OVERSEAS_MAJOR_CATEGORY:
        return _daily_pnl_window_date()
    return _today_cst()


def _latest_investment_snapshot_as_of():
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT MAX(as_of) AS as_of
                FROM pams_holdings_snapshot
                WHERE major_category = ANY(%s::text[])
                """,
                (["A股", "美股", "贵金属", "ESOP"],),
            )
            row = cur.fetchone()
            return row["as_of"] if row else None


def _get_holdings_with_symbol(
    category: str,
    exclude_minor_categories: Tuple[str, ...] = (),
    market_buckets: Tuple[str, ...] = (),
) -> List[dict]:
    """查询指定大类中有symbol的持仓"""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            excluded_sql = ""
            excluded_sql_h = ""
            params: List[object] = [category]
            if exclude_minor_categories:
                placeholders = ", ".join(["%s"] * len(exclude_minor_categories))
                excluded_sql = f"AND COALESCE(minor_category, '') NOT IN ({placeholders})"
                excluded_sql_h = f"AND COALESCE(h.minor_category, '') NOT IN ({placeholders})"
                params.extend(exclude_minor_categories)

            cte_params = list(params)
            select_params = [category]
            if exclude_minor_categories:
                select_params.extend(exclude_minor_categories)
            if _use_latest_account_snapshot_bucket(category, exclude_minor_categories):
                if category == _OVERSEAS_MAJOR_CATEGORY:
                    bucket_expr = _overseas_market_bucket_case()
                    h_bucket_expr = _overseas_market_bucket_case("h")
                    cur.execute(f"""
                        WITH latest_bucket AS (
                            SELECT
                                account_code,
                                asset_liability,
                                major_category,
                                minor_category,
                                {bucket_expr} AS market_bucket,
                                MAX(as_of) AS as_of
                            FROM pams_holdings_snapshot
                            WHERE major_category = %s
                              {excluded_sql}
                            GROUP BY account_code, asset_liability, major_category, minor_category, {bucket_expr}
                        )
                        SELECT
                            h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                            h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                            h.quantity, h.buy_price, h.current_price
                        FROM pams_holdings_snapshot h
                        JOIN latest_bucket lb
                          ON lb.account_code = h.account_code
                         AND lb.asset_liability = h.asset_liability
                         AND lb.major_category = h.major_category
                         AND lb.minor_category = h.minor_category
                         AND lb.market_bucket = {h_bucket_expr}
                         AND lb.as_of = h.as_of
                        WHERE h.major_category = %s
                          AND h.symbol IS NOT NULL AND h.symbol != ''
                          AND h.quantity IS NOT NULL AND h.quantity > 0
                          {excluded_sql_h}
                    """, [*cte_params, *select_params])
                else:
                    cur.execute(f"""
                        WITH latest_bucket AS (
                            SELECT
                                account_code,
                                asset_liability,
                                major_category,
                                minor_category,
                                MAX(as_of) AS as_of
                            FROM pams_holdings_snapshot
                            WHERE major_category = %s
                              {excluded_sql}
                            GROUP BY account_code, asset_liability, major_category, minor_category
                        )
                        SELECT
                            h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                            h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                            h.quantity, h.buy_price, h.current_price
                        FROM pams_holdings_snapshot h
                        JOIN latest_bucket lb
                          ON lb.account_code = h.account_code
                         AND lb.asset_liability = h.asset_liability
                         AND lb.major_category = h.major_category
                         AND lb.minor_category = h.minor_category
                         AND lb.as_of = h.as_of
                        WHERE h.major_category = %s
                          AND h.symbol IS NOT NULL AND h.symbol != ''
                          AND h.quantity IS NOT NULL AND h.quantity > 0
                          {excluded_sql_h}
                    """, [*cte_params, *select_params])
            else:
                cur.execute(f"""
                    WITH latest_position AS (
                        SELECT
                            account_code,
                            COALESCE(NULLIF(symbol, ''), name) AS position_key,
                            MAX(as_of) AS as_of
                        FROM pams_holdings_snapshot
                        WHERE major_category = %s
                          AND symbol IS NOT NULL AND symbol != ''
                          AND quantity IS NOT NULL AND quantity > 0
                          {excluded_sql}
                        GROUP BY account_code, COALESCE(NULLIF(symbol, ''), name)
                    )
                    SELECT
                        h.id, h.asset_id, h.symbol, h.name, h.asset_liability, h.currency, h.account_code,
                        h.major_category, h.minor_category, h.market_value, h.cost, h.as_of,
                        h.quantity, h.buy_price, h.current_price
                    FROM pams_holdings_snapshot h
                    JOIN latest_position lp
                      ON lp.account_code = h.account_code
                     AND lp.position_key = COALESCE(NULLIF(h.symbol, ''), h.name)
                     AND lp.as_of = h.as_of
                    WHERE h.major_category = %s
                      AND h.symbol IS NOT NULL AND h.symbol != ''
                      AND h.quantity IS NOT NULL AND h.quantity > 0
                      {excluded_sql}
                """, [*cte_params, *select_params])
            rows = [dict(row) for row in cur.fetchall()]
            if market_buckets:
                wanted = {str(item).strip().lower() for item in market_buckets if str(item).strip()}
                rows = [row for row in rows if _overseas_market_bucket(row) in wanted]
            return rows


def _use_latest_account_snapshot_bucket(category: str, exclude_minor_categories: Tuple[str, ...]) -> bool:
    """Broker snapshots are complete account buckets, not per-symbol feeds."""
    return (
        category == _A_STOCK_MAJOR_CATEGORY
        and _A_STOCK_FUND_MINOR_CATEGORY in set(exclude_minor_categories)
    ) or category == _OVERSEAS_MAJOR_CATEGORY


def _overseas_market_bucket(row: dict) -> str:
    """Classify overseas holdings for targeted refresh scheduling."""
    from domain.market.overseas_return_calendar import market_bucket

    return market_bucket(row) or "other"


def _overseas_market_bucket_case(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    symbol = f"UPPER(COALESCE({prefix}symbol, ''))"
    currency = f"UPPER(COALESCE({prefix}currency, ''))"
    return f"""
        CASE
            WHEN {symbol} LIKE '%%.ST' THEN 'se'
            WHEN {symbol} LIKE '%%.SW' THEN 'ch'
            WHEN {symbol} LIKE '%%.KS' OR {symbol} LIKE '%%.KQ' THEN 'kr'
            WHEN {symbol} LIKE '%%.HK' THEN 'hk'
            WHEN {symbol} ~ '^[0-9]{{1,5}}$' THEN 'hk'
            WHEN {currency} = 'KRW' THEN 'kr'
            WHEN {currency} = 'HKD' THEN 'hk'
            WHEN {currency} = 'SEK' THEN 'se'
            WHEN {currency} = 'CHF' THEN 'ch'
            WHEN {currency} = 'USD' THEN 'us'
            ELSE 'other'
        END
    """


def _normalize_overseas_market_filter(market: Optional[str]) -> Tuple[str, ...]:
    raw = str(market or "").strip().lower().replace("-", "_")
    if raw in {"", "all", "overseas"}:
        return ()
    aliases = {
        "kr": "kr",
        "korea": "kr",
        "korean": "kr",
        "kr_stock": "kr",
        "hk": "hk",
        "hong_kong": "hk",
        "hongkong": "hk",
        "hk_stock": "hk",
        "se": "se",
        "sweden": "se",
        "se_stock": "se",
        "ch": "ch",
        "switzerland": "ch",
        "ch_stock": "ch",
        "us": "us",
        "usa": "us",
        "us_stock": "us",
        "us_only": "us",
    }
    bucket = aliases.get(raw)
    return (bucket,) if bucket else ()


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _same_day_trade_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    if raw.upper().startswith(("SSE:", "SZSE:")):
        return raw.split(":", 1)[1]
    if raw.lower().startswith(("sh", "sz", "bj")) and len(raw) >= 8:
        return raw[2:]
    return raw


def _is_a_stock_buy_trade(trade: dict) -> bool:
    trade_type = str(trade.get("trade_type") or "").lower()
    side = str(trade.get("side") or "").lower()
    return trade_type in {"buy", "fund_subscription"} or side == "buy"


def _is_a_stock_sell_trade(trade: dict) -> bool:
    trade_type = str(trade.get("trade_type") or "").lower()
    side = str(trade.get("side") or "").lower()
    return trade_type in {"sell", "fund_redemption"} or side == "sell"


def _trade_gross_amount(trade: dict, quantity: Decimal) -> Decimal:
    gross = _dec(trade.get("gross_amount"))
    if gross != 0:
        return abs(gross)
    price = _dec(trade.get("price"))
    return abs(quantity * price)


def _roll_forward_a_stock_position(base_row: dict, trades: list[dict]) -> dict[str, Decimal | None]:
    """Replay same-day A-share trades over one position snapshot."""
    quantity = _dec(base_row.get("quantity"))
    cost = None if base_row.get("cost") is None else _dec(base_row.get("cost"))

    for trade in trades:
        trade_qty = abs(_dec(trade.get("quantity")))
        if trade_qty <= 0:
            continue
        if _is_a_stock_buy_trade(trade):
            from domain.investments.trades.trade_cost_evidence import known_trade_charges, unconverted_ibkr_fee
            fees = known_trade_charges(trade)
            quantity += trade_qty
            if unconverted_ibkr_fee(trade) or (fees is None and (trade.get("net_cash") is None or _dec(trade.get("net_cash")) >= 0)):
                cost = None
            elif cost is None:
                pass
            elif fees is None:
                cost += -_dec(trade["net_cash"])
            else:
                cost += _trade_gross_amount(trade, trade_qty) + fees
            continue
        if _is_a_stock_sell_trade(trade):
            if quantity <= 0:
                continue
            sell_qty = min(trade_qty, quantity)
            if cost is not None:
                avg_cost = cost / quantity
                cost -= avg_cost * sell_qty
            quantity -= sell_qty
            if quantity <= Decimal("0.0000001"):
                quantity = Decimal("0")
                cost = Decimal("0")

    return {
        "quantity": quantity,
        "cost": cost,
        "buy_price": cost / quantity if quantity > 0 and cost is not None else None,
    }


def _row_matches_rollforward(row: dict, expected: dict[str, Decimal | None]) -> bool:
    qty_delta = abs(_dec(row.get("quantity")) - expected["quantity"])
    if expected["cost"] is None:
        return qty_delta <= Decimal("0.0001") and row.get("cost") is None
    cost_delta = abs(_dec(row.get("cost")) - expected["cost"])
    return qty_delta <= Decimal("0.0001") and cost_delta <= Decimal("0.01")


def _apply_rollforward_to_row(row: dict, expected: dict[str, Decimal | None]) -> dict:
    adjusted = dict(row)
    adjusted["quantity"] = expected["quantity"]
    adjusted["cost"] = expected["cost"]
    adjusted["buy_price"] = expected["buy_price"]
    current_price = _dec(adjusted.get("current_price"))
    if current_price > 0:
        adjusted["market_value"] = expected["quantity"] * current_price
    adjusted["same_day_trade_adjusted"] = True
    return adjusted


def _previous_a_stock_snapshot_rows(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], dict]:
    if not keys:
        return {}
    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        """
        SELECT DISTINCT ON (account_code, symbol)
            account_code, symbol, quantity, cost, buy_price, current_price, market_value, as_of
        FROM pams_holdings_snapshot
        WHERE major_category = %s
          AND minor_category = %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND as_of < %s
          AND quantity IS NOT NULL
          AND quantity > 0
        ORDER BY account_code, symbol, as_of DESC, updated_at DESC, id DESC
        """,
        (_A_STOCK_MAJOR_CATEGORY, _A_STOCK_MINOR_CATEGORY, accounts, symbols, as_of),
    )
    return {
        (str(row["account_code"]), _same_day_trade_symbol(row["symbol"])): dict(row)
        for row in cur.fetchall()
    }


def _eligible_trade_source_filter(cur) -> str | None:
    cur.execute("""SELECT to_regclass('public.pams_investment_trade_fact') IS NOT NULL AS exists,
        to_regclass('public.source_items') IS NOT NULL AS item_exists,
        to_regclass('public.source_batches') IS NOT NULL AS batch_exists,
        to_regclass('public.source_records') IS NOT NULL AS redesigned""")
    relations = cur.fetchone()
    if not relations or not relations.get("exists"):
        return None
    if relations.get('redesigned'):
        return '__redesigned__'
    if not relations.get("item_exists") or not relations.get("batch_exists"):
        return "source_table NOT IN ('tradingview_csv','source_items','ibkr_flex_trade')"
    return """((source_item_id IS NULL AND source_table NOT IN
        ('tradingview_csv','source_items','ibkr_flex_trade')) OR EXISTS (
        SELECT 1 FROM source_items si JOIN source_batches sb ON sb.batch_id=si.batch_id
        WHERE si.source_item_id=pams_investment_trade_fact.source_item_id
          AND si.review_status='confirmed' AND sb.status='published'
          AND sb.account_code=pams_investment_trade_fact.account_code))"""


class _TradeQuantities(dict):
    def __init__(self, values=(), *, unverified_keys=()):
        super().__init__(values)
        self.unverified_keys = set(unverified_keys)


def _a_stock_trade_net_quantities_before(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], Decimal]:
    """Return pre-day net share quantities from confirmed trade facts."""
    if not keys:
        return {}
    source_filter = _eligible_trade_source_filter(cur)
    if source_filter is None:
        return {}
    if source_filter == '__redesigned__':
        from domain.investments.trades.trade_read_v2 import prior_trade_quantities
        values, unknown = prior_trade_quantities(cur, keys=keys, as_of=as_of)
        return _TradeQuantities(values, unverified_keys=unknown)
    linked_source = "source_item_id IS NOT NULL" if "source_item_id" in source_filter else "FALSE"

    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        f"""
        SELECT
            account_code,
            symbol,
            SUM(
                CASE
                    WHEN trade_type IN ('buy', 'fund_subscription')
                      OR LOWER(COALESCE(side, '')) = 'buy'
                        THEN ABS(COALESCE(quantity, 0))
                    WHEN trade_type IN ('sell', 'fund_redemption')
                      OR LOWER(COALESCE(side, '')) = 'sell'
                        THEN -ABS(COALESCE(quantity, 0))
                    ELSE 0
                END
            ) AS net_quantity,
            BOOL_OR(source_table IN ('tradingview_csv','source_items','ibkr_flex_trade') OR {linked_source}) AS coverage_unverified
        FROM pams_investment_trade_fact
        WHERE trade_date < %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND COALESCE(quantity, 0) > 0
          AND LOWER(review_status) IN ('auto','reviewed','confirmed')
          AND {source_filter}
          AND (
              trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
              OR LOWER(COALESCE(side, '')) IN ('buy', 'sell')
          )
        GROUP BY account_code, symbol
        """,
        (as_of, accounts, symbols),
    )
    values = [dict(row) for row in cur.fetchall()]
    return _TradeQuantities({
        (str(row["account_code"]), _same_day_trade_symbol(row["symbol"])): _dec(row["net_quantity"])
        for row in values
    }, unverified_keys={(str(row["account_code"]), _same_day_trade_symbol(row["symbol"]))
                        for row in values if row.get("coverage_unverified")})


def _same_day_a_stock_trades(cur, keys: set[tuple[str, str]], as_of) -> dict[tuple[str, str], list[dict]]:
    if not keys:
        return {}
    source_filter = _eligible_trade_source_filter(cur)
    if source_filter is None:
        return {}
    if source_filter == '__redesigned__':
        from domain.investments.trades.trade_read_v2 import same_day_trade_groups
        values, unknown = same_day_trade_groups(cur, keys=keys, as_of=as_of)
        return _TradeQuantities(values, unverified_keys=unknown)

    accounts = sorted({account for account, _ in keys})
    symbols = sorted({symbol for _, symbol in keys})
    cur.execute(
        f"""
        SELECT
            trade_id,
            account_code,
            symbol,
            trade_type,
            side,
            quantity,
            price,
            gross_amount,
            fee_amount,
            fee_currency,
            tax_amount,
            net_cash,
            currency,
            source_table,
            raw_json
        FROM pams_investment_trade_fact
        WHERE trade_date = %s
          AND account_code = ANY(%s::text[])
          AND symbol = ANY(%s::text[])
          AND COALESCE(quantity, 0) > 0
          AND LOWER(review_status) IN ('auto','reviewed','confirmed')
          AND {source_filter}
          AND (
              trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
              OR LOWER(COALESCE(side, '')) IN ('buy', 'sell')
          )
        ORDER BY COALESCE(trade_time, trade_date::timestamp), trade_id
        """,
        (as_of, accounts, symbols),
    )
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in cur.fetchall():
        key = (str(row["account_code"]), _same_day_trade_symbol(row["symbol"]))
        grouped.setdefault(key, []).append(dict(row))
    return grouped


def _apply_same_day_a_stock_trades(rows: list[dict], as_of) -> list[dict]:
    """Roll A-share snapshots forward with same-day confirmed trades."""
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("major_category") != _A_STOCK_MAJOR_CATEGORY:
            continue
        if row.get("minor_category") != _A_STOCK_MINOR_CATEGORY:
            continue
        symbol = _same_day_trade_symbol(row.get("symbol"))
        account = str(row.get("account_code") or "")
        if account and symbol:
            keys.add((account, symbol))
    if not keys:
        return rows

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            previous_rows = _previous_a_stock_snapshot_rows(cur, keys, as_of)
            pre_day_quantities = _a_stock_trade_net_quantities_before(cur, keys, as_of)
            trades_by_key = _same_day_a_stock_trades(cur, keys, as_of)

    if not trades_by_key and not getattr(trades_by_key, 'unverified_keys', set()):
        return rows

    adjusted_rows: list[dict] = []
    for row in rows:
        key = (str(row.get("account_code") or ""), _same_day_trade_symbol(row.get("symbol")))
        if key in getattr(trades_by_key, 'unverified_keys', set()):
            adjusted_rows.append({**row, '_daily_basis_unavailable': True})
            continue
        trades = trades_by_key.get(key)
        if not trades:
            adjusted_rows.append(row)
            continue

        pre_day_quantity = pre_day_quantities.get(key)
        if (pre_day_quantity is not None and pre_day_quantity <= 0
                and key not in getattr(pre_day_quantities, "unverified_keys", set())):
            base_row = {"quantity": Decimal("0"), "cost": Decimal("0")}
        else:
            base_row = previous_rows.get(key)
        if base_row is None and str(row.get("as_of") or "")[:10] != str(as_of):
            base_row = row
        if base_row is None:
            adjusted_rows.append(row)
            continue

        expected = _roll_forward_a_stock_position(base_row, trades)
        if _row_matches_rollforward(row, expected):
            adjusted_rows.append(row)
        else:
            adjusted_rows.append(_apply_rollforward_to_row(row, expected))
    return adjusted_rows


def _upsert_price_snapshots(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Copy the latest category snapshot to `as_of` with refreshed prices."""
    payload, origins = [], []
    for row in rows:
        symbol = row["symbol"]
        if symbol not in prices:
            continue
        quantity = float(row["quantity"])
        current_price = float(prices[symbol])
        market_value = round(quantity * current_price, 2)
        payload.append((
            row.get("symbol"),
            row.get("name"),
            row.get("asset_liability") or "资产",
            row.get("currency") or "CNY",
            row.get("account_code"),
            row.get("major_category"),
            row.get("minor_category"),
            market_value,
            row.get("cost"),
            as_of,
            row.get("quantity"),
            row.get("buy_price"),
            current_price,
            row.get("asset_id"),
        ))
        origins.append(row)

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            lock_holding_accounts(cur, [row[4] for row in payload])
            isolated = source_enabled(cur)
            if isolated:
                keys = ('account_code','name','symbol','currency','as_of','quantity','cost','buy_price','market_value','current_price','asset_id')
                for original, target in zip(origins, payload):
                    if not original.get('id'):
                        raise ValueError('price source holding identity missing')
                    cur.execute('SELECT ' + ','.join(keys) + ' FROM pams_holdings_snapshot WHERE id=%s FOR UPDATE', (original['id'],))
                    stored = cur.fetchone()
                    if not stored or any((stored[key] if isinstance(stored, dict) else stored[index]) != original.get(key)
                                         for index,key in enumerate(keys)):
                        raise ValueError('price source holding changed; refresh the input')
                    cur.execute('SELECT id,quantity,cost FROM pams_holdings_snapshot WHERE account_code=%s AND name=%s AND as_of=%s FOR UPDATE',
                                (target[4], target[1], target[9]))
                    existing = cur.fetchone()
                    if existing:
                        stored_id = existing['id'] if isinstance(existing, dict) else existing[0]
                        stored_qty = existing['quantity'] if isinstance(existing, dict) else existing[1]
                        stored_cost = existing['cost'] if isinstance(existing, dict) else existing[2]
                        if stored_id != original['id'] and (stored_qty != original.get('quantity') or stored_cost != original.get('cost')):
                            raise ValueError('newer holding input conflicts with price refresh')
            statement = """
                INSERT INTO pams_holdings_snapshot (
                    symbol, name, asset_liability, currency, account_code,
                    major_category, minor_category, market_value, cost, as_of,
                    quantity, buy_price, current_price, asset_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_code, name, as_of)
                DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    asset_liability = EXCLUDED.asset_liability,
                    currency = EXCLUDED.currency,
                    major_category = EXCLUDED.major_category,
                    minor_category = EXCLUDED.minor_category,
                    market_value = EXCLUDED.market_value,
                    cost = EXCLUDED.cost,
                    quantity = EXCLUDED.quantity,
                    buy_price = EXCLUDED.buy_price,
                    current_price = EXCLUDED.current_price,
                    asset_id = COALESCE(pams_holdings_snapshot.asset_id, EXCLUDED.asset_id),
                    updated_at = now()
            """
            if isolated:
                statement += """ WHERE (pams_holdings_snapshot.symbol,pams_holdings_snapshot.asset_liability,
                    pams_holdings_snapshot.currency,pams_holdings_snapshot.major_category,pams_holdings_snapshot.minor_category,
                    pams_holdings_snapshot.market_value,pams_holdings_snapshot.cost,pams_holdings_snapshot.quantity,
                    pams_holdings_snapshot.buy_price,pams_holdings_snapshot.current_price,pams_holdings_snapshot.asset_id)
                    IS DISTINCT FROM (EXCLUDED.symbol,EXCLUDED.asset_liability,EXCLUDED.currency,EXCLUDED.major_category,
                    EXCLUDED.minor_category,EXCLUDED.market_value,EXCLUDED.cost,EXCLUDED.quantity,
                    EXCLUDED.buy_price,EXCLUDED.current_price,COALESCE(pams_holdings_snapshot.asset_id, EXCLUDED.asset_id)) RETURNING id"""
                for row in payload:
                    prepare_holding_change(cur, account_code=row[4], name=row[1], as_of=row[9], source_item_id=None)
                    cur.execute(statement, row)
                    written = cur.fetchone()
                    if written:
                        identifier = written['id'] if isinstance(written, dict) else written[0]
                        finish_holding_change(cur, holding_id=identifier, source_item_id=None,
                            input_refs=[{'kind':'quote', 'key':None, 'role':'valuation_quote'}])
            else:
                cur.executemany(statement, payload)
            conn.commit()
            return len(payload)


def _upsert_cn_a_closes(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Store refreshed A-share prices as same-day local close rows."""
    payload = []
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in prices:
            continue
        quote_symbol = _a_stock_quote_symbol(str(symbol))
        if not quote_symbol:
            continue
        payload.append((quote_symbol, "CN_A", as_of, prices[symbol]))

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, market, trade_date)
                DO UPDATE SET close = EXCLUDED.close
            """, payload)
            conn.commit()
            return len(payload)


def _upsert_daily_return_quote_snapshots(
    rows: List[dict[str, Any]],
    prices: Dict[str, float],
    as_of,
    *,
    source: str,
    market: str,
    currency: str = "CNY",
) -> int:
    """Mirror refreshed public prices into the canonical daily return state."""
    if not rows or not prices:
        return 0

    written = 0
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol not in prices:
                    continue
                quote_symbol = _a_stock_quote_symbol(symbol) or symbol
                quote = quote_snapshot_from_market_daily(
                    {
                        "as_of": as_of,
                        "asset_id": row.get("asset_id"),
                        "symbol": symbol,
                        "name": row.get("name"),
                        "market": market,
                        "currency": row.get("currency") or currency,
                        "provider": source.split("_")[0] if source else "market",
                        "source": source,
                        "source_key": f"{source}:{quote_symbol}:{as_of.isoformat()}",
                        "close_price": Decimal(str(prices[symbol])),
                    }
                )
                upsert_quote_snapshot(cur, quote)
                written += 1
            conn.commit()
    return written


def _upsert_scheduled_us_quotes(rows: List[dict], prices: Dict[str, float], as_of, basis: str) -> int:
    if not prices:
        return 0
    written = 0
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol not in prices:
                    continue
                meta = _US_ESTIMATE_QUOTE_CACHE.get(symbol.upper(), {}) if basis == "estimate" else {}
                price = Decimal(str(prices[symbol]))
                if not price.is_finite() or price <= 0:
                    continue
                source = meta.get("source") or "yahoo_close"
                quote = quote_snapshot_from_market_daily({
                    "as_of": as_of, "asset_id": row.get("asset_id"), "symbol": symbol,
                    "name": row.get("name"), "currency": row.get("currency") or "USD", "market": "US",
                    "source": source, "provider": meta.get("provider") or "yahoo",
                    "source_key": f"{source}:{symbol}:{as_of}", "close_price": price,
                    "return_status": "estimated" if basis == "estimate" else "confirmed",
                    "price_time": meta.get("as_of_utc"),
                    "raw_json": {**meta, "valuation_projection_only": True},
                })
                upsert_quote_snapshot(cur, quote)
                written += 1
        conn.commit()
    return written


def _overseas_currency_mismatches(rows: list[dict], bucket: str) -> list[dict]:
    from domain.market.overseas_return_calendar import MARKET_CURRENCIES

    expected = MARKET_CURRENCIES[bucket]
    return [{"reason": "currency_mismatch", "symbol": row.get("symbol"),
             "account_code": row.get("account_code"), "currency": row.get("currency"),
             "expected_currency": expected, "market": bucket}
            for row in rows if row.get("currency") != expected]


def _upsert_scheduled_overseas_quotes(
    rows: list[dict], bucket: str, as_of: date, history: dict[str, list[tuple[date, float]]],
) -> tuple[int, int, set[str]]:
    """Persist dated closes and quote marks atomically, using DB-rounded prices."""
    from domain.market.overseas_return_calendar import market_calendar

    if _overseas_currency_mismatches(rows, bucket):
        raise ValueError("currency_mismatch: needs_review")
    payload = {}
    eligible = set()
    previous_session = None
    for row in rows:
        symbol = str(row.get("symbol") or "")
        closes = {}
        for trade_date, close in history.get(symbol, []):
            try:
                value = Decimal(str(close))
            except (ArithmeticError, TypeError, ValueError):
                continue
            if trade_date <= as_of and value.is_finite() and value > 0:
                closes[trade_date] = value
        if as_of not in closes:
            continue
        if previous_session is None:
            previous_session = market_calendar(bucket).previous_session(as_of.isoformat()).date()
        eligible.add(symbol)
        for trade_date in (previous_session, as_of):
            if trade_date in closes:
                payload[(symbol, "YF", trade_date)] = closes[trade_date]
    if not payload:
        return 0, 0, set()

    written = 0
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            missing_previous = sorted(symbol for symbol in eligible if (symbol, "YF", previous_session) not in payload)
            if missing_previous:
                cur.execute("""
                    SELECT symbol, close FROM pams_ohlcv_daily
                    WHERE market = 'YF' AND symbol = ANY(%s::text[]) AND trade_date = %s
                      AND close > 0 AND close::text NOT IN ('NaN', 'Infinity', '-Infinity')
                """, (missing_previous, previous_session))
                available_previous = {str(row["symbol"]) for row in cur.fetchall()}
                eligible.difference_update(set(missing_previous) - available_previous)
            payload = {key: value for key, value in payload.items() if key[0] in eligible}
            if not payload:
                return 0, 0, set()
            cur.executemany("""
                INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, market, trade_date)
                DO UPDATE SET close = EXCLUDED.close
            """, [(*key, value) for key, value in payload.items()])
            # Never replace NUMERIC(12,4) with the original Yahoo float tail.
            cur.execute("""
                SELECT symbol, trade_date, close FROM pams_ohlcv_daily
                WHERE market = 'YF' AND symbol = ANY(%s::text[]) AND trade_date = ANY(%s::date[])
                  AND close > 0 AND close::text NOT IN ('NaN', 'Infinity', '-Infinity')
            """, (sorted(eligible), [previous_session, as_of]))
            stored_closes = {(str(row["symbol"]), row["trade_date"]): row["close"] for row in cur.fetchall()}
            stored = {symbol: stored_closes[(symbol, as_of)] for symbol in eligible
                      if (symbol, as_of) in stored_closes and (symbol, previous_session) in stored_closes}
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol not in stored:
                    continue
                quote = quote_snapshot_from_market_daily({
                    "as_of": as_of, "asset_id": row.get("asset_id"), "symbol": symbol,
                    "name": row.get("name"), "currency": row.get("currency"), "market": bucket.upper(),
                    "source": "yahoo_close", "provider": "yahoo",
                    "source_key": f"yahoo_close:{bucket}:{symbol}:{as_of}", "close_price": stored[symbol],
                    "return_status": "confirmed", "raw_json": {"valuation_projection_only": True},
                })
                upsert_quote_snapshot(cur, quote)
                written += 1
        conn.commit()
    return len(payload), written, set(stored)


def _has_completed_overseas_quotes(rows: list[dict], bucket: str, as_of: date) -> bool:
    """Only complete, matching close evidence can suppress a provider refresh."""
    from domain.market.overseas_return_calendar import market_calendar

    required = {(str(row.get("symbol") or ""), str(row.get("currency") or "")) for row in rows}
    if (not required or any(not symbol or not currency for symbol, currency in required)
            or _overseas_currency_mismatches(rows, bucket)):
        return False
    previous_session = market_calendar(bucket).previous_session(as_of.isoformat()).date()
    with _get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT q.symbol, q.currency FROM (SELECT * FROM market_observations WHERE observation_type = 'quote') q
            JOIN pams_ohlcv_daily current_close ON current_close.symbol = q.symbol
              AND current_close.market = 'YF' AND current_close.trade_date = q.quote_date
            JOIN pams_ohlcv_daily previous_close ON previous_close.symbol = q.symbol
              AND previous_close.market = 'YF' AND previous_close.trade_date = %s
            WHERE q.quote_date = %s AND q.market = %s AND q.symbol = ANY(%s::text[])
              AND q.provider = 'yahoo' AND q.source = 'yahoo_close'
              AND q.return_status = 'confirmed' AND q.quote_session = 'close'
              AND q.price > 0 AND q.price::text NOT IN ('NaN', 'Infinity', '-Infinity')
              AND q.price = current_close.close
              AND previous_close.close > 0 AND previous_close.close::text NOT IN ('NaN', 'Infinity', '-Infinity')
              AND q.raw_json @> '{"valuation_projection_only":true}'::jsonb
        """, (previous_session, as_of, bucket.upper(), sorted({symbol for symbol, _ in required})))
        covered = {(str(row["symbol"]), str(row["currency"])) for row in cur.fetchall()}
    return required <= covered


def _refresh_scheduled_overseas_quotes(bucket: str, as_of: date) -> dict:
    """Refresh one completed local session without writing account snapshots."""
    holdings = _get_holdings_with_symbol(_OVERSEAS_MAJOR_CATEGORY, market_buckets=(bucket,))
    result = {
        "category": _OVERSEAS_MAJOR_CATEGORY,
        "source_as_of": str(holdings[0]["as_of"]) if holdings else None,
        "as_of": as_of.isoformat(), "found": len(holdings), "price_basis": "close",
        "market_buckets": [bucket], "valuation_projection_only": True,
    }
    mismatches = _overseas_currency_mismatches(holdings, bucket)
    if mismatches:
        return {**result, "status": "needs_review", "reason": "currency_mismatch",
                "manual_review_items": mismatches, "updated": 0, "covered": 0,
                "close_rows": 0, "daily_quote_rows": 0, "quote_sources": {}, "errors": ["currency_mismatch"]}
    if _has_completed_overseas_quotes(holdings, bucket, as_of):
        return {**result, "status": "ok", "unchanged": True, "updated": 0, "covered": len(holdings),
                "close_rows": 0, "daily_quote_rows": 0, "quote_sources": {}, "errors": []}
    symbols = {str(row["symbol"]): _yfinance_quote_symbol(str(row["symbol"]), bucket) for row in holdings}
    _YFINANCE_CLOSE_CACHE.clear()
    if symbols:
        _fetch_us_stock_prices_yf(list(dict.fromkeys(symbols.values())))
    history = {symbol: list(_YFINANCE_CLOSE_CACHE.get(provider_symbol, []))
               for symbol, provider_symbol in symbols.items()}
    close_rows, written, stored = _upsert_scheduled_overseas_quotes(holdings, bucket, as_of, history)
    return {
        **result, "updated": written, "covered": written, "close_rows": close_rows, "daily_quote_rows": written,
        "quote_sources": {"yahoo_close": written} if written else {},
        "errors": [f"{row['symbol']}: missing target or previous-session close" for row in holdings
                   if str(row["symbol"]) not in stored],
    }


def _upsert_yfinance_closes(rows: List[dict]) -> int:
    """Store recent Yahoo Finance close rows for overseas daily PnL."""
    payload = []
    seen = set()
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        for trade_date, close in _YFINANCE_CLOSE_CACHE.get(str(symbol), []):
            key = (str(symbol), "YF", trade_date)
            if key in seen:
                continue
            seen.add(key)
            payload.append((str(symbol), "YF", trade_date, close))

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, close)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, market, trade_date)
                DO UPDATE SET close = EXCLUDED.close
            """, payload)
            conn.commit()
            return len(payload)


def _estimate_quote_market(meta: dict[str, Any], symbol: str) -> str:
    source = str(meta.get("source") or "")
    if source == "databento_estimate":
        return "US_EXTENDED"
    if source == "ibkr_client_portal_estimate":
        return "US_EXTENDED"
    if source == "ibkr_tws_estimate":
        return "US_OVERNIGHT"
    raw_symbol = str(symbol or "").upper()
    if raw_symbol.endswith(".KS") or raw_symbol.endswith(".KQ"):
        return "KR_ESTIMATE"
    if raw_symbol.endswith(".HK") or raw_symbol.isdigit():
        return "HK_ESTIMATE"
    return "YAHOO_EXTENDED"


def _upsert_us_estimate_market_daily(rows: List[dict], prices: Dict[str, float], as_of) -> int:
    """Persist selected estimate quote marks for source-level auditability."""
    payload = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        quoted_price = prices.get(symbol)
        if quoted_price is None:
            quoted_price = prices.get(symbol.upper())
        if quoted_price is None:
            continue
        cache_key = symbol.upper()
        meta = dict(_US_ESTIMATE_QUOTE_CACHE.get(cache_key) or {})
        source = str(meta.get("source") or "external_estimate")
        raw_json = {
            "price_basis": "estimate",
            "provider": meta.get("provider") or source,
            "session": meta.get("session"),
            "quote_type": meta.get("quote_type"),
            "as_of_utc": meta.get("as_of_utc"),
            "conid": meta.get("conid"),
            "raw": meta.get("raw"),
        }
        payload.append(
            (
                as_of,
                source,
                f"{source}:{symbol}:{as_of}",
                row.get("asset_id"),
                symbol,
                row.get("name"),
                _estimate_quote_market(meta, symbol),
                row.get("currency") or "USD",
                quoted_price,
                str(meta.get("confidence") or "medium"),
                psycopg2.extras.Json(raw_json),
            )
        )

    if not payload:
        return 0

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market_observations (
            observation_type,
                    as_of, source, source_key, asset_id, symbol, name, market,
                    currency, close_price, confidence, raw_json
                ) VALUES ('daily', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (as_of, source, source_key) WHERE observation_type = 'daily'
                DO UPDATE SET
                    asset_id = EXCLUDED.asset_id,
                    symbol = EXCLUDED.symbol,
                    name = EXCLUDED.name,
                    market = EXCLUDED.market,
                    currency = EXCLUDED.currency,
                    close_price = EXCLUDED.close_price,
                    confidence = EXCLUDED.confidence,
                    raw_json = market_observations.raw_json || EXCLUDED.raw_json,
                    updated_at = now()
                """,
                payload,
            )
            conn.commit()
            return len(payload)


def _estimate_quote_source_summary(symbols: List[str]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for symbol in symbols:
        meta = _US_ESTIMATE_QUOTE_CACHE.get(str(symbol or "").strip().upper()) or {}
        source = str(meta.get("source") or "untracked")
        summary[source] = summary.get(source, 0) + 1
    return summary


def _price_key_for_symbol(prices: Dict[str, float], symbol: Any) -> str | None:
    raw = str(symbol or "").strip()
    if not raw:
        return None
    if raw in prices:
        return raw
    upper = raw.upper()
    if upper in prices:
        return upper
    return None


def _is_ibkr_account(account_code: Any) -> bool:
    raw = str(account_code or "").strip().lower()
    return raw in {"ibkr", "interactive_brokers", "interactivebrokers"}


def _is_high_confidence_us_estimate_source(symbol: Any) -> bool:
    meta = _US_ESTIMATE_QUOTE_CACHE.get(str(symbol or "").strip().upper()) or {}
    source = str(meta.get("source") or "")
    return source in _HIGH_CONFIDENCE_US_ESTIMATE_SOURCES


def _filter_us_estimate_prices_for_holdings(
    rows: List[dict],
    prices: Dict[str, float],
) -> tuple[Dict[str, float], list[dict[str, Any]]]:
    """IBKR holdings need high-confidence all-hours marks, not fallback quotes."""
    filtered = dict(prices)
    skipped: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        price_key = _price_key_for_symbol(filtered, symbol)
        if price_key is None:
            continue
        if not _is_ibkr_account(row.get("account_code")):
            continue
        if _is_high_confidence_us_estimate_source(symbol):
            corrected = _correct_ibkr_estimate_scale(
                symbol=symbol,
                price=filtered[price_key],
                reference_price=row.get("current_price"),
            )
            if corrected is not None and corrected != filtered[price_key]:
                filtered[price_key] = corrected
                if symbol in filtered:
                    filtered[symbol] = corrected
                if symbol.upper() in filtered:
                    filtered[symbol.upper()] = corrected
            continue
        meta = _US_ESTIMATE_QUOTE_CACHE.get(symbol.upper()) or {}
        filtered.pop(price_key, None)
        filtered.pop(symbol, None)
        filtered.pop(symbol.upper(), None)
        skipped.append(
            {
                "symbol": symbol,
                "name": row.get("name"),
                "account_code": row.get("account_code"),
                "source": meta.get("source") or "untracked",
                "reason": "ibkr_estimate_requires_high_confidence_quote",
            }
        )
    return filtered, skipped


def _correct_ibkr_estimate_scale(
    *,
    symbol: str,
    price: Any,
    reference_price: Any,
) -> float | None:
    """Correct obvious IBKR fixed-point quote scale drift.

    If a quote arrives one or two decimal places away from the latest holding
    price, use the holding price as a sanity anchor and only correct when a
    decimal shift brings the quote back into a plausible range.
    """
    try:
        value = float(price)
        reference = float(reference_price)
    except (TypeError, ValueError):
        return None
    if value <= 0 or reference <= 0:
        return None

    def plausible(candidate: float) -> bool:
        ratio = candidate / reference
        return 0.35 <= ratio <= 2.75

    if plausible(value):
        return value

    candidates = [value / 10, value / 100, value * 10, value * 100]
    for candidate in candidates:
        if candidate > 0 and plausible(candidate):
            fixed = round(candidate, 4)
            meta = _US_ESTIMATE_QUOTE_CACHE.setdefault(str(symbol or "").strip().upper(), {})
            meta["price"] = fixed
            meta["scale_correction"] = {
                "original_price": value,
                "corrected_price": fixed,
                "reference_price": reference,
                "method": "latest_holding_price_sanity",
            }
            return fixed
    return value


def _latest_stored_yfinance_prices(
    symbols: List[str],
    *,
    as_of,
    fallback: Dict[str, float] | None = None,
) -> Dict[str, float]:
    """Return the latest stored Yahoo close for each symbol on or before as_of.

    Yahoo's quote endpoints can expose moving or stale quote prices around
    overseas market sessions. PAMS displays overseas holdings on a close-price
    basis, so valuation snapshots should prefer persisted OHLCV closes.
    """
    prices: Dict[str, float] = dict(fallback or {})
    clean_symbols = [str(s) for s in symbols if s]
    if not clean_symbols:
        return prices

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol) symbol, close
                FROM pams_ohlcv_daily
                WHERE market = 'YF'
                  AND symbol = ANY(%s::text[])
                  AND trade_date <= %s
                  AND close IS NOT NULL
                ORDER BY symbol, trade_date DESC
                """,
                (clean_symbols, as_of),
            )
            for row in cur.fetchall():
                close = row.get("close")
                if close is None:
                    continue
                try:
                    value = float(close)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    prices[str(row["symbol"])] = round(value, 6)
    return prices


