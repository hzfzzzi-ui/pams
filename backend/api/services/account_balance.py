"""Read-only account balance basis shared by balances and performance.

Retains existing snapshot selection and cash-flow roll-forward semantics.
Profit, realized PnL and return-rate calculations remain in investment_returns.
"""
from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Any

from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE


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
    previous_net_gross: Decimal | None = None
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
        "holding_pnl_gross": holding_pnl_gross,
        "net_gross": net_gross,
        "net_personal": net_personal,
        "net_liquidation_source": net_liquidation_source,
        "net_liquidation_rollforward": net_liquidation_rollforward,
        "previous_net_gross": previous_net_gross,
    }
