"""Reference-data read adapters."""

from __future__ import annotations

from typing import Any


def build_categories_payload(cur) -> dict[str, dict[str, list[Any]]]:
    """Return the legacy /api/categories payload."""
    cur.execute("SELECT to_regclass('public.pams_asset_categories') IS NOT NULL AS exists")
    row = cur.fetchone() or {}
    table_exists = bool(row.get("exists"))

    if not table_exists:
        cur.execute(
            """
            SELECT major_category, minor_category
            FROM pams_holdings_snapshot
            WHERE major_category IS NOT NULL
              AND minor_category IS NOT NULL
            GROUP BY major_category, minor_category
            ORDER BY major_category, minor_category
            """
        )
        rows = cur.fetchall()
        return _group_categories(rows)

    cur.execute(
        """
        SELECT major_category, minor_category
        FROM pams_asset_categories
        ORDER BY major_category, minor_category
        """
    )
    rows = cur.fetchall()

    return _group_categories(rows)


def _group_categories(rows) -> dict[str, dict[str, list[Any]]]:
    categories: dict[str, list[Any]] = {}
    for row in rows:
        major = row["major_category"]
        categories.setdefault(major, []).append(row["minor_category"])

    return {"categories": categories}
