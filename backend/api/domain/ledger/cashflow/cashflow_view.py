"""Cashflow page read adapters."""

from __future__ import annotations

from typing import Any


def build_cashflow_series_payload(*, limit: int, currency: str) -> dict[str, list[Any]]:
    """Return the legacy /cashflow/series placeholder payload."""
    _ = (limit, currency)
    return {"rows": []}
