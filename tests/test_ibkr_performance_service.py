from __future__ import annotations

import ast
from decimal import Decimal
from datetime import date
from contextlib import nullcontext
import hashlib
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from collectors.ibkr_flex import IBKRFlexError  # noqa: E402
from endpoints import ibkr  # noqa: E402
from services import ibkr_performance as service  # noqa: E402
from services import risk_state  # noqa: E402


# AST fingerprints captured from main fdae2a1 before moving the functions.
# Selection intentionally changes in the publication PR; monetary/FX parsers stay frozen.
BASELINE_FUNCTIONS = {
    "get_conn": "c85e2736a5582dbee0b4149324705c010d065c8d00a35e105b0873bae1036159",
    "_norm": "11dc300a34cda207f9e829c2f1cc6fed077083249d80496ad802a42996cf3388",
    "_dec": "a0f3ea41ec85d93e536192e81dd87fd5150fa528168eeb42b56dcf1ae0444a96",
    "_ib_date": "746e5401c43ef48501ebc6e840a9c6f11af567b3c8d3fa96e508039fd09ec534",
    "_latest_usd_cny_rate": "7e56914bdb8a3fd935a94be84239bed5c2482a8c397d87ced9ffb0dd53f72eaf",
    "_parse_flex_performance": "fd85b84f13f853c2acd1b2f5e3f8607cc4948f96c18b1cf548dfef485416d405",
}

XML = """<FlexQueryResponse>
  <CashReportCurrency currency="USD" toDate="20261022" endingCash="999" />
  <CashReportCurrency currency="BASE_SUMMARY" toDate="20261020" endingCash="99" />
  <CashReportCurrency currency="BASE_SUMMARY" toDate="20261021"
    endingCash="-500" depositWithdrawalsYTD="1,000" />
  <OpenPosition reportDate="20261020" positionValue="9999" />
  <OpenPosition reportDate="20261021" positionValue="1000" fxRateToBase="1"
    fifoPnlUnrealized="100" />
  <OpenPosition reportDate="20261021" positionValue="-200" fxRateToBase="2"
    fifoPnlUnrealized="-20" />
  <Trade tradeDate="20261020" fifoPnlRealized="25" fxRateToBase="2" />
  <Trade tradeDate="20261022" fifoPnlRealized="9999" />
</FlexQueryResponse>"""

EXPECTED = {
    "status": "ok", "as_of": "2026-10-21", "base_currency": "USD",
    "source_file": "report.xml", "net_deposit_base": 1000.0,
    "net_liquidation_base": 100.0, "total_pnl_base": -900.0,
    "return_ratio": -0.9, "gross_exposure_base": 1400.0,
    "leverage_ratio": 14.0, "negative_cash_base": 500.0,
    "negative_cash_ratio": 5.0, "cash_base": -500.0,
    "position_value_base": 600.0, "unrealized_pnl_base": 60.0,
    "realized_pnl_base": 50.0, "other_pnl_base": -1010.0,
    "position_count": 2,
}


@pytest.mark.parametrize("name,fingerprint", BASELINE_FUNCTIONS.items())
def test_moved_function_ast_is_identical_to_baseline(name, fingerprint):
    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    if name == "_parse_flex_performance":
        # Permit only the read-only display opt-out; preserve the original formula hash.
        assert len(node.args.kwonlyargs) == len(node.args.kw_defaults) == 1
        assert node.args.kwonlyargs[0].arg == "include_display_fx"
        assert ast.dump(node.args.kwonlyargs[0].annotation) == "Name(id='bool', ctx=Load())"
        assert isinstance(node.args.kw_defaults[0], ast.Constant) and node.args.kw_defaults[0].value is True
        node.args.kwonlyargs, node.args.kw_defaults = [], []
        assignment = next(n for n in node.body if isinstance(n, ast.Assign)
                          and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "usd_cny")
        assert isinstance(assignment.value, ast.IfExp)
        assert isinstance(assignment.value.test, ast.Name) and assignment.value.test.id == "include_display_fx"
        assert isinstance(assignment.value.orelse, ast.Constant) and assignment.value.orelse.value is None
        assignment.value = assignment.value.body
    assert hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() == fingerprint
    assert getattr(ibkr, name) is getattr(service, name)


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(XML, encoding="utf-8")
    return path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ibkr.router, prefix="/api")
    with TestClient(app) as value:
        yield value


@pytest.mark.parametrize("fx", [None, Decimal("7"), Decimal("0")])
def test_parser_and_http_preserve_all_fields_and_amounts(report, monkeypatch, client, fx):
    monkeypatch.setattr(service, "_latest_usd_cny_rate", lambda: fx)
    selected = service.report_state.PublishedReport(report, date(2026, 10, 21), 1,
        hashlib.sha256(report.read_bytes()).hexdigest(), None)
    monkeypatch.setattr(service, "get_conn", lambda: nullcontext(None))
    monkeypatch.setattr(service.report_state, "select_report", lambda *args: (selected, None))
    expected = dict(EXPECTED)
    if fx is not None:
        expected["fx_rate_to_cny"] = float(fx)
        for key in ("net_deposit", "net_liquidation", "total_pnl", "gross_exposure",
                    "negative_cash", "cash", "position_value", "unrealized_pnl",
                    "realized_pnl", "other_pnl"):
            expected[key + "_cny"] = float(Decimal(str(expected[key + "_base"])) * fx)
    before = report.read_bytes()
    assert service._parse_flex_performance(report) == expected
    response = client.get("/api/ibkr/performance")
    assert response.status_code == 200
    payload = response.json()
    assert payload.pop("report_quality")["statement_as_of"] == expected["as_of"]
    assert payload == expected
    assert report.read_bytes() == before


def test_position_date_fallback_and_invalid_numbers(report, monkeypatch):
    report.write_text(XML.replace('toDate="20261021"', 'toDate="20261022"')
                     .replace('positionValue="1000"', 'positionValue="invalid"'), encoding="utf-8")
    monkeypatch.setattr(service, "_latest_usd_cny_rate", lambda: None)
    result = service._parse_flex_performance(report)
    assert result["as_of"] == "2026-10-21"
    assert result["position_value_base"] == -400.0
    assert result["net_liquidation_base"] == -900.0
    assert result["leverage_ratio"] == result["negative_cash_ratio"] == 0


def test_no_positions_and_no_deposits(report, monkeypatch):
    report.write_text('<R><CashReportCurrency currency="BASE_SUMMARY" '
                      'toDate="20261021" endingCash="100" /></R>', encoding="utf-8")
    monkeypatch.setattr(service, "_latest_usd_cny_rate", lambda: None)
    result = service._parse_flex_performance(report)
    assert result["position_count"] == 0
    assert result["net_liquidation_base"] == 100.0
    assert result["return_ratio"] == 0


def test_latest_report_selection_preserves_env_and_default_root(tmp_path, monkeypatch):
    monkeypatch.delenv("PAMS_DATA_DIR", raising=False)
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "get_conn", lambda: nullcontext(None))
    calls = []
    def select(conn, raw):
        calls.append(raw)
        return None, None
    monkeypatch.setattr(service.report_state, "select_report", select)
    assert service._latest_flex_xml_path() is None
    assert calls[-1] == tmp_path / "data/raw/ibkr"
    monkeypatch.setenv("PAMS_DATA_DIR", str(tmp_path / "other"))
    assert service._latest_flex_xml_path() is None
    assert calls[-1] == tmp_path / "other/raw/ibkr"


def test_http_error_contract_preserved(report, monkeypatch, client):
    monkeypatch.setattr(ibkr, "read_published_performance", lambda: None)
    response = client.get("/api/ibkr/performance")
    assert response.status_code == 404
    assert response.json() == {"detail": "No published IBKR Flex report is available"}
    monkeypatch.setattr(ibkr, "read_published_performance", lambda: service._parse_flex_performance(report))
    report.write_text("<R/>", encoding="utf-8")
    response = client.get("/api/ibkr/performance")
    assert response.status_code == 422
    assert response.json() == {"detail": "IBKR Flex report has no BASE_SUMMARY cash report"}
    report.write_text("<broken", encoding="utf-8")
    response = client.get("/api/ibkr/performance")
    assert response.status_code == 500
    assert "detail" in response.json()


def test_risk_uses_service_without_endpoint_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "endpoints.ibkr", None)
    monkeypatch.setattr(service, "read_published_performance", lambda: dict(EXPECTED))
    assert risk_state._latest_ibkr_performance() == EXPECTED
    monkeypatch.setattr(service, "read_published_performance", lambda: None)
    assert risk_state._latest_ibkr_performance() is None

    def fail():
        raise IBKRFlexError("unavailable")

    monkeypatch.setattr(service, "read_published_performance", fail)
    assert risk_state._latest_ibkr_performance() is None
    for path in (Path(service.__file__), Path(risk_state.__file__)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.ImportFrom) and (n.module or "").startswith("endpoints")
                       for n in ast.walk(tree))
