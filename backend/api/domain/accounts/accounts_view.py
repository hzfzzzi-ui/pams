"""Account read adapters."""

from __future__ import annotations

from decimal import Decimal
from datetime import date as Date
from typing import Any

from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_accounts_payload(cur, *, active: bool) -> dict[str, list[dict[str, Any]]]:
    """Return the legacy /api/accounts payload."""
    if active:
        cur.execute(
            """
            SELECT account_code, display_name, base_currency, is_asset
            FROM pams_accounts
            WHERE active = true
            ORDER BY display_name
            """
        )
    else:
        cur.execute(
            """
            SELECT account_code, display_name, base_currency, is_asset, active
            FROM pams_accounts
            ORDER BY display_name
            """
        )
    rows = cur.fetchall()
    return {"accounts": [{key: _norm(value) for key, value in dict(row).items()} for row in rows]}


def build_account_detail_payload(cur, *, account_code: str) -> dict[str, Any] | None:
    """Return the legacy /api/accounts/{account_code} payload, or None when missing."""
    cur.execute(
        """
        SELECT account_code, display_name, base_currency, is_asset, active, created_at
        FROM pams_accounts
        WHERE account_code = %s
        """,
        (account_code,),
    )
    row = cur.fetchone()

    if not row:
        return None

    cur.execute(
        f"""
        WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT COUNT(*) as holdings_count, ROUND(SUM(market_value)::numeric, 2) as total_value
        FROM pams_holdings_snapshot hs
        JOIN latest_account la
          ON la.account_code = hs.account_code
         AND la.account_as_of = hs.as_of
        WHERE hs.account_code = %s
        """,
        (Date.today(), account_code),
    )
    stats = cur.fetchone()

    account_data = {key: _norm(value) for key, value in dict(row).items()}
    account_data["holdings_count"] = stats["holdings_count"]
    account_data["total_value"] = str(stats["total_value"]) if stats["total_value"] else "0"
    return account_data
