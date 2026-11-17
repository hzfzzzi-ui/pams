"""Exercise existing IBKR display bindings offline with synthetic DTOs only."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from .holdings_source import read_holdings_source
SOURCE = read_holdings_source(ROOT / "frontend/public", "holdings_categories.js")


def test_mobile_card_uses_archive_period_fields():
    assert "cumulative: officialIbkr?.display_period_pnl_base" in SOURCE
    assert "const ibkrNetValue = officialIbkr?.amount_base" in SOURCE


def test_investment_entry_uses_a_distinct_binding_module_cache_key():
    html = (ROOT / "frontend/public/investments.html").read_text(encoding="utf-8")
    assert "holdings_categories.js" in html
