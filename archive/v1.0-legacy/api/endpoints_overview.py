# /api/overview endpoint for dashboard V1
import os
from typing import Any, Dict, List
import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from main import get_conn, _norm, _get_base_currency, json


def _norm_float(x: Any) -> float:
    """Safely convert to float for calculations."""
    try:
        return float(x) if x is not None else 0.0
    except Exception:
        return 0.0


def register_overview(app: FastAPI):
    @app.get("/api/overview")
    def overview(baseCurrency: str = "CNY"):
        """Dashboard V1 overview data (compatible with frontend expectations)."""
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Get latest snapshot date
                cur.execute("SELECT max(as_of) AS as_of FROM pams_holdings_snapshot")
                row = cur.fetchone()
                as_of = row["as_of"] if row else None
                if as_of is None:
                    return json({
                        "asOfDate": None,
                        "baseCurrency": baseCurrency,
                        "totalAssets": 0,
                        "totalLiabilities": 0,
                        "netWorth": 0,
                        "buckets": {},
                        "positions": [],
                        "fxUsed": {},
                    })

                base_currency = baseCurrency  # use requested base

                # Get FX rates for the base currency
                cur.execute(
                    """
                    SELECT quote_currency, rate
                      FROM pams_fx_rates_daily
                     WHERE as_of = %s AND base_currency = %s
                    """,
                    [as_of, base_currency],
                )
                fx_rates = {r["quote_currency"]: float(r["rate"]) for r in cur.fetchall()}
                # Add 1:1 for base currency itself
                fx_rates[base_currency] = 1.0

                # Fetch all holdings for the date
                cur.execute(
                    """
                    SELECT * FROM pams_holdings_snapshot WHERE as_of = %s ORDER BY market_value DESC NULLS LAST
                    """,
                    [as_of],
                )
                holdings = cur.fetchall()

                # Initialize buckets
                buckets = {
                    "cash": 0.0,
                    "fixed_income": 0.0,
                    "equity": 0.0,
                    "gold_commodities": 0.0,
                    "crypto": 0.0,
                    "esop": 0.0,
                    "receivable": 0.0,
                    "liability": 0.0,
                    "other": 0.0,
                }

                positions = []
                total_assets = 0.0
                total_liabilities = 0.0

                for h in holdings:
                    currency = h["currency"]
                    rate = fx_rates.get(currency, 1.0)
                    mv_base = _norm_float(h["market_value"]) * rate

                    # Account kind detection
                    account_name = h.get("account", "")
                    account_kind = "asset"
                    if account_name.startswith("liability_") or h["type"] == "liability_cc":
                        account_kind = "liability"

                    # Map to buckets
                    bucket = "other"
                    t = h["type"]

                    if t == "cash":
                        bucket = "cash"
                    elif t == "fund":
                        bucket = "fixed_income"  # or could split further
                    elif t in ("stock_cn", "stock_us", "stock_hk"):
                        bucket = "equity"
                    elif t == "gold":
                        bucket = "gold_commodities"
                    elif t == "crypto":
                        bucket = "crypto"
                    elif t == "equity_private":
                        bucket = "esop"
                    elif t.startswith("receivable"):
                        bucket = "receivable"
                    elif t == "liability_cc":
                        bucket = "liability"
                    elif t == "wealth":
                        bucket = "fixed_income"

                    if account_kind == "liability":
                        bucket = "liability"
                        # Liabilities are stored as negative values
                        liability_value = abs(mv_base)
                        buckets[bucket] += liability_value
                        total_liabilities += liability_value
                    else:
                        buckets[bucket] += mv_base
                        total_assets += mv_base

                    # Build position row
                    pos = {
                        "accountName": account_name,
                        "accountId": account_name,
                        "symbol": h["symbol"] or "",
                        "symbolType": t,
                        "quantity": h.get("quantity", None),
                        "costBasis": h.get("cost_basis", None),
                        "costCurrency": h.get("cost_currency", None),
                        "marketPrice": None,  # would need price history table
                        "accountCurrency": currency,
                        "marketValue": _norm_float(h["market_value"]),
                        "marketValueBase": mv_base,
                        "pnlUnrealized": _norm_float(h.get("holding_pnl")),
                        "pnlDaily": _norm_float(h.get("daily_pnl")),
                        "accountKind": account_kind,
                    }
                    positions.append(pos)

                net_worth = total_assets - total_liabilities

                return json({
                    "asOfDate": as_of.isoformat(),
                    "baseCurrency": base_currency,
                    "totalAssets": total_assets,
                    "totalLiabilities": total_liabilities,
                    "netWorth": net_worth,
                    "buckets": buckets,
                    "positions": positions,
                    "fxUsed": fx_rates,
                })
