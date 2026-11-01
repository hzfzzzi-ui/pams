"""Read-only account balance basis shared by balances and performance.

Retains existing snapshot selection and cash-flow roll-forward semantics.
Profit, realized PnL and return-rate calculations remain in investment_returns.
"""
from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Any

from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from domain.investments.snapshot_coverage import load_account_snapshot_coverage


def dec(value: Any) -> Decimal:
    """Convert a possibly-empty numeric value to Decimal."""
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def norm(value: Any) -> Any:
    """Normalize values for legacy JSON responses."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _cash_source_schema_available(cur) -> bool:
    cur.execute("""SELECT to_regclass('public.source_items') IS NOT NULL AS items,
        to_regclass('public.source_batches') IS NOT NULL AS batches,
        EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.pams_investment_trade_fact')
          AND attname='source_item_id' AND NOT attisdropped) AS links""")
    row = cur.fetchone() or {}
    return bool(row.get('items') and row.get('batches') and row.get('links'))


def _cash_delta_cny(cur, account_code: str, start: Any, end: Any, *, external_only: bool = False) -> Decimal | None:
    """Sum verified observed cash events with same-day FX; preserve gaps.

    Even a zero result does not prove that the period's sources are complete.
    """
    extra_filter = " AND f.trade_type IN ('deposit','withdraw')" if external_only else ""
    cur.execute("""SELECT CASE WHEN COUNT(*) FILTER (WHERE f.net_cash IS NULL OR (f.net_cash <> 0 AND fx.rate IS NULL)
        OR i.source_item_id IS NULL OR i.review_status IS DISTINCT FROM 'confirmed'
        OR b.status IS DISTINCT FROM 'published' OR b.account_code IS DISTINCT FROM f.account_code) > 0 THEN NULL
        ELSE COALESCE(SUM(CASE WHEN f.net_cash=0 THEN 0 ELSE f.net_cash * fx.rate END), 0) END AS cash_delta,
        COALESCE(array_agg(f.trade_id), '{}') AS fact_ids
        FROM pams_investment_trade_fact f
        LEFT JOIN source_items i ON i.source_item_id=f.source_item_id
        LEFT JOIN source_batches b ON b.batch_id=i.batch_id
        LEFT JOIN LATERAL (SELECT CASE WHEN f.currency='CNY' THEN 1::numeric ELSE (
            SELECT rate FROM pams_fx_rates_daily r WHERE r.base_currency='CNY'
            AND r.quote_currency=f.currency AND r.as_of=f.trade_date AND r.rate > 0
            AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
            AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1
        ) END AS rate) fx ON TRUE
        WHERE f.account_code=%s AND f.affects_cash AND f.trade_date > %s AND f.trade_date <= %s
          AND COALESCE(LOWER(f.review_status), 'auto') NOT IN ('ignored','cancelled','canceled','void')""" + extra_filter,
        (account_code, start, end))
    row = cur.fetchone() or {}
    if row.get('cash_delta') is None:
        return None
    from domain.imports.source_fact_evidence import NativeFactVerifier
    verifier = NativeFactVerifier(cur)
    for fact_id in row.get('fact_ids') or []:
        if verifier.verify(table='pams_investment_trade_fact',fact_id=fact_id).get('status') != 'verified':
            return None
    return dec(row['cash_delta'])


def load_account_balance_basis(
    cur,
    *,
    account_code: str,
    owner_ratio: Decimal,
    net_liquidation_gross: Decimal | None = None,
    net_liquidation: Decimal | None = None,
    net_liquidation_as_of: Any | None = None,
) -> dict[str, Any] | None:
    """Read current/previous account values without computing investment returns."""
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
    if load_account_snapshot_coverage(cur, {(account_code, as_of)}).get((account_code, as_of), {}).get("status") != "complete":
        return None

    cur.execute(
        """
        SELECT
            h.account_code,
            h.as_of,
            h.major_category,
            h.minor_category,
            h.asset_liability,
            h.symbol,
            h.name,
            h.currency,
            CASE WHEN h.market_value=0 THEN 0 ELSE
                                (CASE WHEN h.asset_liability='负债' THEN -ABS(h.market_value) ELSE h.market_value END) * fx.rate
            END AS market_value_cny,
            CASE WHEN h.holding_pnl=0 THEN 0 ELSE h.holding_pnl * fx.rate END AS holding_pnl_cny
        FROM pams_holdings_snapshot h
        LEFT JOIN LATERAL (SELECT CASE WHEN h.currency = 'CNY' THEN 1::numeric ELSE (
            SELECT r.rate FROM pams_fx_rates_daily r WHERE r.base_currency='CNY'
                AND r.quote_currency=h.currency AND r.as_of=h.as_of AND r.rate > 0
                AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1
        ) END AS rate) fx ON TRUE
        WHERE h.account_code = %s
          AND h.as_of = %s
        """,
        (account_code, as_of),
    )
    rows = [dict(row) for row in cur.fetchall()]

    if not rows or any(row.get("market_value_cny") is None for row in rows):
        return None

    position_value_gross = sum(dec(row.get("market_value_cny")) for row in rows)
    holding_pnl_gross = (None if any(row.get("holding_pnl_cny") is None for row in rows)
                         else sum(dec(row["holding_pnl_cny"]) for row in rows))
    # Account cash is a subset of the same complete snapshot, not the
    # difference between NAV and its already cash-inclusive total.
    cash_value_gross = (sum((dec(row["market_value_cny"]) for row in rows
                             if str(row["major_category"]) == "现金"), Decimal("0"))
                        if all(row.get("major_category") is not None for row in rows) else None)
    classified = all(row.get("major_category") is not None and row.get("asset_liability") is not None for row in rows)
    securities_value_gross = (sum((dec(row["market_value_cny"]) for row in rows
                                   if row["major_category"] != "现金" and row["asset_liability"] != "负债"), Decimal("0"))
                              if classified else None)
    other_liabilities_gross = (sum((dec(row["market_value_cny"]) for row in rows
                                    if row["major_category"] != "现金" and row["asset_liability"] == "负债"), Decimal("0"))
                               if classified else None)
    net_gross = net_liquidation_gross if net_liquidation_gross is not None else position_value_gross
    net_liquidation_source = "current_snapshot"
    net_liquidation_rollforward: dict[str, Any] | None = None
    rolled_forward_net_liquidation = False
    previous_net_gross: Decimal | None = None
    previous_as_of = None
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
        if not _cash_source_schema_available(cur):
            return None
        cur.execute(
            f"""
            WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
            SELECT MAX(la.account_as_of) AS snapshot_as_of,
                   CASE WHEN COUNT(*) FILTER (WHERE hs.market_value IS NULL OR (hs.market_value<>0 AND fx.rate IS NULL)) > 0 THEN NULL
                        ELSE SUM(CASE WHEN hs.market_value=0 THEN 0 ELSE
                            (CASE WHEN hs.asset_liability='负债' THEN -ABS(hs.market_value) ELSE hs.market_value END) * fx.rate
                            END) END AS market_value
            FROM pams_holdings_snapshot hs
            JOIN latest_account la
              ON la.account_code = hs.account_code
             AND la.account_as_of = hs.as_of
            LEFT JOIN LATERAL (SELECT CASE WHEN hs.currency='CNY' THEN 1::numeric ELSE (
                SELECT rate FROM pams_fx_rates_daily r WHERE r.base_currency='CNY'
                AND r.quote_currency=hs.currency AND r.as_of=hs.as_of AND r.rate > 0
                AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1
            ) END AS rate) fx ON TRUE
            WHERE hs.account_code = %s
            """,
            (anchor_as_of, account_code),
        )
        anchor_row = cur.fetchone() or {}
        if anchor_row.get("market_value") is None:
            return None
        anchor_snapshot_date = anchor_row.get("snapshot_as_of")
        if not anchor_snapshot_date or load_account_snapshot_coverage(cur, {(account_code, anchor_snapshot_date)}).get(
                (account_code, anchor_snapshot_date), {}).get("status") != "complete":
            return None
        anchor_position_value_gross = dec(anchor_row.get("market_value"))
        cash_delta_gross = _cash_delta_cny(cur, account_code, anchor_as_of, as_of)
        position_delta_gross = position_value_gross - anchor_position_value_gross
        # Both verified account snapshots include cash. Their total-value
        # difference already includes deposits, withdrawals and trade cash;
        # adding transaction cash a second time would double-count it.
        # Still verify transaction sources above, and expose their sum as an
        # explanatory component rather than another change to account NAV.
        net_gross = net_liquidation_gross + position_delta_gross
        net_liquidation_source = "rolled_forward_manual_balance"
        rolled_forward_net_liquidation = True
        net_liquidation_rollforward = {
            "anchor_as_of": str(anchor_as_of)[:10],
            "anchor_net_liquidation_gross_cny": norm(net_liquidation_gross),
            "anchor_position_market_value_gross_cny": norm(anchor_position_value_gross),
            "position_delta_gross_cny": norm(position_delta_gross),
            "cash_delta_gross_cny": norm(cash_delta_gross),
            "cash_delta_in_snapshot": True,
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
        if previous_as_of is not None and load_account_snapshot_coverage(cur, {(account_code, previous_as_of)}).get(
                (account_code, previous_as_of), {}).get("status") != "complete":
            previous_as_of = None
        if previous_as_of is not None:
            cur.execute(
                """
                SELECT CASE WHEN COUNT(*) FILTER (WHERE h.market_value IS NULL OR (h.market_value<>0 AND fx.rate IS NULL)) > 0 THEN NULL
                            ELSE SUM(CASE WHEN h.market_value=0 THEN 0 ELSE
                                (CASE WHEN h.asset_liability='负债' THEN -ABS(h.market_value) ELSE h.market_value END) * fx.rate
                                END) END AS market_value
                FROM pams_holdings_snapshot h
                LEFT JOIN LATERAL (SELECT CASE WHEN h.currency='CNY' THEN 1::numeric ELSE (
                    SELECT rate FROM pams_fx_rates_daily r WHERE r.base_currency='CNY'
                    AND r.quote_currency=h.currency AND r.as_of=h.as_of AND r.rate > 0
                    AND r.rate::text NOT IN ('NaN','Infinity','-Infinity')
                AND NULLIF(BTRIM(r.source),'') IS NOT NULL LIMIT 1
                ) END AS rate) fx ON TRUE
                WHERE h.account_code = %s AND h.as_of = %s
                """,
                (account_code, previous_as_of),
            )
            previous_position_row = cur.fetchone() or {}
            if previous_position_row.get("market_value") is None:
                previous_as_of = None
            else:
                previous_position_value_gross = dec(previous_position_row["market_value"])
                previous_net_gross = (
                    net_liquidation_gross
                    + (previous_position_value_gross - anchor_position_value_gross)
                )

    if rolled_forward_net_liquidation:
        net_personal = net_gross * owner_ratio
    elif net_liquidation is not None:
        net_personal = net_liquidation
    else:
        net_personal = net_gross * owner_ratio
    return {
        "as_of": as_of,
        "rows": rows,
        "position_value_gross": position_value_gross,
        "cash_value_gross": cash_value_gross,
        "securities_value_gross": securities_value_gross,
        "other_liabilities_gross": other_liabilities_gross,
        "holding_pnl_gross": holding_pnl_gross,
        "net_gross": net_gross,
        "net_personal": net_personal,
        "net_liquidation_source": net_liquidation_source,
        "net_liquidation_rollforward": net_liquidation_rollforward,
        "previous_net_gross": previous_net_gross,
        "previous_as_of": previous_as_of,
    }
