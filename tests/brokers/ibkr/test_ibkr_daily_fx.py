"""Synthetic daily evidence; no credentials, real account IDs or ledger writes."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal as D
import hashlib
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/api"))
from domain.brokers.ibkr import ibkr_daily_close as close
from domain.brokers.ibkr import ibkr_daily_fx as fx
from domain.investments import valuation_nav
from domain.investments.return_attribution import build_return_attribution_from_positions

LOW, HIGH = date(2024, 10, 21), date(2024, 10, 22)


def report(*, fee="0", extra=None):
    root = ET.Element("FlexQueryResponse")
    for day, cash, starting in ((LOW, D("-30000"), D("-30000")),
                               (HIGH, D("-30000") + D(fee), D("-30000"))):
        stmt = ET.SubElement(root, "FlexStatement", accountId="SYNTHETIC", fromDate=day.isoformat(), toDate=day.isoformat())
        ET.SubElement(stmt, "AccountInformation", accountId="SYNTHETIC", currency="USD")
        equity = ET.SubElement(stmt, "EquitySummaryInBase")
        for when, amount in ((day - timedelta(days=1), starting), (day, cash)):
            fields = {key: "0" for key in close.COMPONENT_FIELDS}
            fields.update(cash=str(amount), stock="50000", total=str(amount + D(50000)))
            ET.SubElement(equity, "EquitySummaryByReportDateInBase", accountId="SYNTHETIC", currency="USD",
                          reportDate=when.isoformat(), **fields)
        positions = ET.SubElement(stmt, "OpenPositions")
        ET.SubElement(stmt, "Trades")
        ET.SubElement(positions, "OpenPosition", accountId="SYNTHETIC", currency="USD", conid="SYNTH1",
                      reportDate=day.isoformat(), levelOfDetail="SUMMARY", symbol="SYNTH",
                      positionValue="50000", fxRateToBase="1")
        cash_report = ET.SubElement(stmt, "CashReport")
        ET.SubElement(cash_report, "CashReportCurrency", accountId="SYNTHETIC", currency="BASE_SUMMARY",
                      levelOfDetail="BaseCurrency", fromDate=day.isoformat(), toDate=day.isoformat(), endingCash=str(cash))
        values = {key: "0" for key in (*fx.INCOME, *fx.EXPENSES, *fx.FUNDING, *fx.PRINCIPAL)}
        values["otherFees"] = fee if day == HIGH else "0"
        ET.SubElement(cash_report, "CashReportCurrency", accountId="SYNTHETIC", currency="USD",
                      levelOfDetail="Currency", fromDate=day.isoformat(), toDate=day.isoformat(),
                      startingCash=str(starting), endingCash=str(cash), **values, **(extra or {}))
        fields = {key: "0" for key in close.FLOW_FIELDS}
        ET.SubElement(stmt, "ChangeInNAV", accountId="SYNTHETIC", currency="USD", fromDate=day.isoformat(),
                      toDate=day.isoformat(), startingValue=str(starting + D(50000)),
                      endingValue=str(cash + D(50000)), **fields)
    return root


def evidence(root=None):
    text = ET.tostring(root if root is not None else report(), encoding="unicode")
    return {"status": "ok", "as_of": HIGH, "account_id": "SYNTHETIC", "xml_text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    fx._PAIRS.clear()
    def historical(cur, *, currency, base_currency, as_of):
        assert currency == "USD" and base_currency == "CNY"
        return {"status": "ok", "rate": D("7.1") if as_of == LOW else D("7"),
                "as_of": as_of.isoformat(), "source": "synthetic_dated_fx"}
    monkeypatch.setattr(fx.close_read, "historical_fx", historical)
    monkeypatch.setattr(valuation_nav, "daily_close_account_identity", lambda cur, **k: {"status": "ok", "as_of": str(k["as_of"])})


def run(root=None, **kwargs):
    return fx.build_ibkr_daily_fx(None, evidence=evidence(root), requested_as_of=HIGH, **kwargs)


def test_signed_financing_is_not_double_deducted_and_exact_bridge():
    result = run()
    assert result["status"] == "confirmed", result
    assert result["net_fx_pnl_cny"] == D(-2000)
    assert result["summary"]["position_fx_pnl_cny"] == D(-5000)
    assert result["summary"]["cash_fx_pnl_cny"] == D(3000)
    assert result["official_total_pnl_cny"] == D(-2000)
    assert result["summary"]["official_residual_cny"] == 0
    assert result["affects_nav"] is False and result["read_only"] is True


def test_fees_are_not_fx_and_inputs_unchanged():
    data = evidence(report(fee="-1.25"))
    before = deepcopy(data)
    result = fx.build_ibkr_daily_fx(None, evidence=data)
    assert result["status"] == "confirmed"
    assert result["summary"]["expense_pnl_cny"] == D("-8.75")
    assert result["net_fx_pnl_cny"] == D(-2000)
    assert result["summary"]["computed_pnl_cny"] == D("-2008.75")
    assert data == before


def test_owner_ratio_scales_all_amounts_not_rates():
    whole, half = run(), run(owner_ratio=D("0.5"))
    assert half["net_fx_pnl_cny"] == D(-1000)
    assert half["summary"]["headline_pnl_cny"] == whole["summary"]["headline_pnl_cny"] / 2
    assert half["currencies"][0]["opening_cash"] == D(-15000)
    assert half["currencies"][0]["opening_fx"]["rate"] == D("7.1")
    assert half["ownership_basis"] == "owner_adjusted"


@pytest.mark.parametrize("ratio", [D(0), D(-1), D(2), D("NaN")])
def test_invalid_owner_is_not_normalized(ratio):
    assert run(owner_ratio=ratio)["status"] == "pending"


def test_missing_fx_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(fx.close_read, "historical_fx", lambda *a, **k: {"status": "unavailable", "reasons": ["expired_historical_fx"]})
    result = run()
    assert result["status"] == "pending"
    assert result["net_fx_pnl_cny"] is None
    assert result["official_total_pnl_cny"] is None


@pytest.mark.parametrize("field,value", [("positionValue", "NaN"), ("fxRateToBase", "2"),
                                         ("levelOfDetail", "LOT"), ("reportDate", "2024-10-23")])
def test_bad_position_evidence_fails_closed(field, value):
    root = report()
    list(root.iter("OpenPosition"))[-1].set(field, value)
    assert run(root)["status"] == "pending"


def test_unknown_nonzero_cash_field_is_explicit_partial():
    result = run(report(extra={"futureCashCategory": "2"}))
    assert result["status"] == "partial"
    assert result["unclassified_cash_fields"] == ["futureCashCategory"]
    assert result["summary"]["headline_pnl_cny"] is None


def test_cash_residual_not_absorbed_into_fx():
    root = report()
    row = [r for r in root.iter("CashReportCurrency") if r.get("currency") == "USD"][-1]
    row.set("otherFees", "-1")
    result = run(root)
    assert result["status"] == "partial"
    assert result["summary"]["unexplained_cash_cny"] == D(7)
    assert result["net_fx_pnl_cny"] == D(-2000)


def test_unsupported_fx_trades_are_not_security_price_income():
    root = report()
    ET.SubElement(list(root.iter("Trades"))[-1], "Trade", accountId="SYNTHETIC", assetCategory="CASH")
    assert run(root)["status"] == "pending"


def test_missing_intermediate_day_with_same_balances_not_confirmed():
    root = report()
    first = list(root.iter("FlexStatement"))[0]
    first.set("fromDate", (LOW - timedelta(days=1)).isoformat())
    first.set("toDate", (LOW - timedelta(days=1)).isoformat())
    result = run(root)
    assert result["status"] == "pending"
    assert result["reasons"] == ["opening_valuation_date_gap"]


def test_nonzero_principal_without_trade_detail_has_no_price_amount():
    root = report()
    row = [r for r in root.iter("CashReportCurrency") if r.get("currency") == "USD"][-1]
    row.set("netTradesSales", "10")
    result = run(root)
    assert result["reasons"] == ["unproven_security_trade_principal"]
    assert result["net_fx_pnl_cny"] is None and "summary" not in result


def test_supported_stock_principal_proven_from_same_day_trades():
    root = report()
    row = [r for r in root.iter("CashReportCurrency") if r.get("currency") == "USD"][-1]
    row.set("netTradesSales", "10")
    row.set("otherFees", "-10")
    ET.SubElement(list(root.iter("Trades"))[-1], "Trade", accountId="SYNTHETIC", currency="USD",
                  assetCategory="STK", levelOfDetail="EXECUTION", tradeDate=HIGH.isoformat(),
                  proceeds="10", tradeID="SYNTH_TRADE")
    result = run(root)
    assert result["status"] == "confirmed", result
    assert result["summary"]["price_pnl_cny"] == D(70)
    assert result["summary"]["expense_pnl_cny"] == D(-70)
    assert result["net_fx_pnl_cny"] == D(-2000)


def test_digest_checked_even_with_cache_hit():
    data = evidence()
    assert fx.build_ibkr_daily_fx(None, evidence=data)["status"] == "confirmed"
    data["xml_text"] += " "
    assert fx.build_ibkr_daily_fx(None, evidence=data)["status"] == "pending"
    assert all(len(key) == 3 and data["xml_text"] not in key for key in fx._PAIRS)


def test_older_report_cannot_appear_as_requested_future_confirmation():
    result = fx.build_ibkr_daily_fx(None, evidence=evidence(), requested_as_of=HIGH + timedelta(days=1))
    assert result["as_of"] == HIGH.isoformat()
    assert result["requested_as_of"] != result["as_of"]
    assert fx.build_ibkr_daily_fx(None, evidence=evidence(), requested_as_of=LOW)["status"] == "pending"


def test_missing_report_does_not_read_db():
    assert fx.build_ibkr_daily_fx(object(), evidence={"status": "unavailable"})["status"] == "pending"


def test_wrong_account_identity_cannot_produce_money(monkeypatch):
    monkeypatch.setattr(valuation_nav, "daily_close_account_identity", lambda cur, **k: {"status": "unconfirmed"})
    result = run()
    assert result["net_fx_pnl_cny"] is None
    assert result["reasons"] == ["unproven_account_identity"]


def test_accrual_change_is_separate_not_fx_or_stock_price():
    root = report()
    nav = list(root.iter("EquitySummaryByReportDateInBase"))[-1]
    nav.set("interestAccruals", "-5")
    nav.set("total", "19995")
    list(root.iter("ChangeInNAV"))[-1].set("endingValue", "19995")
    result = run(root)
    assert result["status"] == "confirmed", result
    assert result["accrual_other_change_cny"] == D(-35)
    assert result["net_fx_pnl_cny"] == D(-2000)
    assert result["official_total_pnl_cny"] == D(-2035)
    assert result["accrual_fx_status"] == "not_separately_attributed"


def test_cny_fund_does_not_add_underlying_fx_and_keeps_legacy_metrics():
    result = build_return_attribution_from_positions([
        {"account_code": "fund", "asset_name": "CNY QDII", "symbol": "SYNTHFUND", "asset_type": "fund",
         "currency": "CNY", "market_value_cny": 110, "cost_cny": 100, "holding_pnl_cny": 10,
         "daily_pnl_cny": 2, "daily_fx_pnl_cny": 0}], {"asset_mapping": [], "asset_lookthrough": []})
    row = result["dimensions"]["currency"][0]
    assert row["name"] == "CNY" and row["daily_fx_pnl_cny"] == 0
    assert row["fx_display"]["show"] is False
    assert result["total_pnl_cny"] == 10 and result["daily_pnl_cny"] == 2
