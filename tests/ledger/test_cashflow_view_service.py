from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.ledger.cashflow_view import build_cashflow_series_payload  # noqa: E402


def test_build_cashflow_series_payload_preserves_placeholder_shape():
    assert build_cashflow_series_payload(limit=30, currency="CNY") == {"rows": []}


def test_build_cashflow_series_payload_ignores_optional_params_for_now():
    assert build_cashflow_series_payload(limit=365, currency="USD") == {"rows": []}
