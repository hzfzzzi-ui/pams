"""Unit tests for asset-type capital-return (投入本金口径) time series."""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "backend" / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from domain.investments.returns import capital_return_series as crs


def test_is_ibkr_external_transfer_distinguishes_deposits_from_dividends_and_interest():
    assert crs._is_ibkr_external_transfer({"description": "CASH RECEIPTS / ELECTRONIC FUND TRANSFER"}) is True
    assert crs._is_ibkr_external_transfer({"description": "ELECTRONIC FUND DISBURSEMENT"}) is True
    assert crs._is_ibkr_external_transfer({"description": "NVDA CASH DIVIDEND USD 0.10"}) is False
    assert crs._is_ibkr_external_transfer({"description": "USD DEBIT INT FOR OCT-2026"}) is False
    assert crs._is_ibkr_external_transfer({"description": "KRW CUSTODY FEE ON STK"}) is False


def test_ibkr_leveraged_account_series_computes_capital_return_and_twr(monkeypatch):
    d1, d2, d3 = date(2026, 2, 11), date(2026, 2, 12), date(2026, 2, 13)
    histories = {
        "ibkr": {
            "rows": {
                d1: {"personal_contribution": Decimal("100000")},
                d2: {"personal_contribution": Decimal("90000")},
                d3: {"personal_contribution": Decimal("110000")},
            }
        }
    }
    monkeypatch.setattr(
        crs,
        "load_cash_rows",
        lambda cur, **kw: [
            {
                "transaction_date": d1,
                "amount_cny": Decimal("100000"),
                "description": "CASH RECEIPTS / ELECTRONIC FUND TRANSFER",
                "source_evidence_status": "verified",
            },
            {
                "transaction_date": d3,
                "amount_cny": Decimal("50000"),
                "description": "CASH RECEIPTS / ELECTRONIC FUND TRANSFER",
                "source_evidence_status": "verified",
            },
            {
                "transaction_date": d2,
                "amount_cny": Decimal("-500"),
                "description": "USD DEBIT INT FOR FEB-2026",
                "source_evidence_status": "verified",
            },
        ],
    )
    series = crs._ibkr_leveraged_account_series(
        None, start=d1, end=d3, base_currency="CNY", histories=histories
    )
    assert series[d1]["cumulative_capital"] == Decimal("100000")
    assert series[d1]["total_pnl"] == Decimal("0")
    assert series[d2]["total_pnl"] == Decimal("-10000")
    # d3: deposited +50k -> total capital 150k, NLV 110k -> total PnL -40k (-26.67%)
    assert series[d3]["cumulative_capital"] == Decimal("150000")
    assert series[d3]["total_pnl"] == Decimal("-40000")
    assert series[d3]["twr"] < Decimal("0")


def test_combine_capital_series_merges_stocks_without_dropout_cliff():
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    a_stock = {
        d1: {"market_value": Decimal("100"), "cumulative_capital": Decimal("100"), "total_pnl": Decimal("0")},
        d2: {"market_value": Decimal("80"), "cumulative_capital": Decimal("100"), "total_pnl": Decimal("-20")},
    }
    ibkr = {
        d1: {"market_value": Decimal("200"), "cumulative_capital": Decimal("200"), "total_pnl": Decimal("0")},
        d2: {"market_value": Decimal("120"), "cumulative_capital": Decimal("200"), "total_pnl": Decimal("-80")},
        d3: {"market_value": Decimal("130"), "cumulative_capital": Decimal("200"), "total_pnl": Decimal("-70")},
    }
    payload = crs._combine_capital_series(
        [a_stock, ibkr], start=d1, end=d3, section="stocks", base_currency="CNY", coverage_notes=[]
    )
    assert payload["status"] == "available"
    assert payload["end"] == "2026-01-02"  # stops at common_end so d3 doesn't drop a_stock
    assert len(payload["rows"]) == 2
    last = payload["rows"][-1]
    assert Decimal(last["cumulative_capital"]) == Decimal("300")
    assert Decimal(last["total_pnl"]) == Decimal("-100")
    assert abs(Decimal(last["capital_return_rate"]) - Decimal("-0.3333333")) < Decimal("0.0001")
    assert last["return_basis"] == "capital_return"
    assert last["time_weighted_return_rate"] is not None
