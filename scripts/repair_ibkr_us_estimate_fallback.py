"""Repair IBKR US snapshots polluted by non-IBKR estimate quotes.

The US overnight refresh must not let medium-confidence Yahoo estimates
overwrite IBKR positions when TWS / Client Portal is unavailable. This script
repairs historical rows by reverting affected snapshots to the latest stored
Yahoo close and deleting the rejected estimate audit rows.
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import psycopg2


def _connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def repair(start: date) -> dict[str, int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ibkr_us_symbols AS (
                    SELECT DISTINCT symbol
                    FROM pams_holdings_snapshot
                    WHERE account_code = 'ibkr'
                      AND currency = 'USD'
                      AND symbol IS NOT NULL
                      AND symbol <> ''
                ),
                affected AS (
                    SELECT DISTINCT as_of, symbol
                    FROM pams_asset_market_daily
                    WHERE source = 'yahoo_estimate'
                      AND as_of >= %s
                      AND symbol IN (SELECT symbol FROM ibkr_us_symbols)
                ),
                latest_close AS (
                    SELECT h.id, d.close
                    FROM pams_holdings_snapshot h
                    JOIN affected a
                      ON a.as_of = h.as_of
                     AND a.symbol = h.symbol
                    JOIN LATERAL (
                        SELECT close
                        FROM pams_ohlcv_daily d
                        WHERE d.market = 'YF'
                          AND d.symbol = h.symbol
                          AND d.trade_date <= h.as_of
                          AND d.close IS NOT NULL
                        ORDER BY d.trade_date DESC
                        LIMIT 1
                    ) d ON TRUE
                    WHERE h.account_code = 'ibkr'
                )
                UPDATE pams_holdings_snapshot h
                SET current_price = latest_close.close,
                    market_value = ROUND((h.quantity * latest_close.close)::numeric, 2),
                    updated_at = now()
                FROM latest_close
                WHERE h.id = latest_close.id
                """,
                (start,),
            )
            repaired_snapshots = cur.rowcount

            cur.execute(
                """
                DELETE FROM pams_asset_market_daily
                WHERE source = 'yahoo_estimate'
                  AND as_of >= %s
                  AND symbol IN (
                      SELECT DISTINCT symbol
                      FROM pams_holdings_snapshot
                      WHERE account_code = 'ibkr'
                        AND currency = 'USD'
                        AND symbol IS NOT NULL
                        AND symbol <> ''
                  )
                """,
                (start,),
            )
            deleted_estimate_rows = cur.rowcount

        conn.commit()

    return {
        "repaired_snapshots": repaired_snapshots,
        "deleted_estimate_rows": deleted_estimate_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=_parse_date, default=date(2026, 6, 18))
    args = parser.parse_args()
    print(repair(args.start))


if __name__ == "__main__":
    main()
