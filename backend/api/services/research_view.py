"""Research page read adapters."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_watchlist_payload(cur, *, limit: int) -> dict[str, Any]:
    """Return the legacy /watchlist payload."""
    cur.execute(
        """
        SELECT market, symbol, name, thesis, status, created_at, updated_at
        FROM pams_watchlist
        ORDER BY updated_at DESC LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    return {"rows": [{key: _norm(value) for key, value in dict(row).items()} for row in rows]}


def build_policy_ips_payload(cur) -> dict[str, Any]:
    """Return the legacy /policy/ips payload."""
    cur.execute("SELECT value, updated_at FROM pams_policy WHERE key='ips_v1'")
    row = cur.fetchone()
    if not row:
        return {"key": "ips_v1", "value": None}
    return {"key": "ips_v1", "value": row["value"], "updated_at": _norm(row["updated_at"])}
