"""Exercise existing IBKR display bindings offline with synthetic DTOs only."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from .holdings_source import read_holdings_source
SOURCE = read_holdings_source(ROOT / "frontend/public", "holdings_categories.js")


def test_sync_center_uses_automatic_source_flow_without_date_relabel_or_daily_approval():
    source = (ROOT / "backend/api/endpoints/sync_center.py").read_text(encoding="utf-8")
    start = source.index('id="ibkr-flex"')
    block = source[start:source.index('id="investment-ledger"', start)]
    assert 'action="后台自动同步，失败自动重试"' in block
    assert 'trigger_body={"align_to_latest_snapshot": False}' in block
    assert "正常同步无需逐次确认" in block
    assert "历史账目诊断独立保留" in block
    assert "reconciliation['details']" in block
    assert "有当日授权" not in block


def test_mobile_card_uses_archive_period_fields():
    assert "cumulative: officialIbkr?.display_period_pnl_base" in SOURCE
    assert "const ibkrNetValue = officialIbkr?.amount_base" in SOURCE


def test_investment_entry_uses_a_distinct_binding_module_cache_key():
    html = (ROOT / "frontend/public/investments.html").read_text(encoding="utf-8")
    assert "holdings_categories.js" in html
