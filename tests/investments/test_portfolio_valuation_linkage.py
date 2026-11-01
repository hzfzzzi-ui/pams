from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.portfolio_summary import apply_published_valuation_state_to_summary  # noqa: E402
from domain.investments.portfolio_valuation_state import (  # noqa: E402
    apply_holdings_category_pnl_to_section_return_series_payload,
    apply_summary_pnl_to_return_series_payload,
    apply_published_valuation_state_to_return_series_payload,
)
from domain.risk.risk_state import _positions_reconciled_to_published_nav  # noqa: E402


def test_apply_published_valuation_state_to_summary_overrides_total_only():
    payload = {
        "as_of": "2026-06-17",
        "base_currency": "CNY",
        "totals": {"market_value": "100", "holding_pnl": "12", "daily_pnl": "3"},
        "by_asset_class": [{"asset_class": "A-share", "market_value": "100"}],
        "children": [],
    }

    out = apply_published_valuation_state_to_summary(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
            "cash_cny": Decimal("80.00"),
            "source_hash": "abc",
        },
    )

    assert out["as_of"] == "2026-06-18"
    assert out["totals"]["market_value"] == "260.00"
    assert out["totals"]["holding_pnl"] == "12"
    assert out["by_asset_class"] == payload["by_asset_class"]
    assert out["valuation_state"]["source"] == "published"
    assert out["valuation_state"]["net_liquidation_value"] == "260.00"
    assert out["valuation_state"]["overlay_applied"] is True


def test_apply_published_valuation_state_to_summary_skips_stale_state():
    payload = {
        "as_of": "2026-06-19",
        "data_as_of_max": "2026-06-19",
        "base_currency": "CNY",
        "totals": {"market_value": "300", "holding_pnl": "12", "daily_pnl": "3"},
        "by_asset_class": [{"asset_class": "A-share", "market_value": "300"}],
        "children": [],
    }

    out = apply_published_valuation_state_to_summary(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
            "cash_cny": Decimal("80.00"),
            "source_hash": "abc",
        },
    )

    assert out["as_of"] == "2026-06-19"
    assert out["totals"]["market_value"] == "300"
    assert out["valuation_state"]["overlay_applied"] is False
    assert out["valuation_state"]["overlay_reason"] == "stale_valuation_state"
    assert out["valuation_state"]["target_as_of"] == "2026-06-19"


@pytest.mark.parametrize("target,adjustment", [(150.0, 50.0), (50.0, -50.0), (100.0, 0.0)])
def test_published_nav_keeps_unexplained_difference_out_of_exposure(target, adjustment):
    from domain.risk.core.risk_engine import gross_exposure, negative_cash_ratio, position_weight
    positions = pd.DataFrame([{"account_code": "broker", "account_name": "Broker",
        "asset_id": "QQQ", "asset_name": "QQQ", "asset_type": "us_stock", "market_value_cny": 100.0}])
    out, nav, meta = _positions_reconciled_to_published_nav(positions, {}, {
        "valuation_date": date(2026, 6, 18), "cycle": "manual_publish", "status": "published",
        "data_completeness": "complete", "net_liquidation_value": target,
        "assets_cny": target, "liabilities_cny": 0.0})
    assert nav == target and meta["raw_positions_nav_cny"] == 100.0
    assert meta["nav_reconciliation_cny"] == adjustment
    assert meta["reconciliation_status"] == ("matched" if adjustment == 0 else "unexplained")
    assert set(out["asset_id"]) == {"QQQ"}
    assert gross_exposure(out, {}, nav_override=nav)["gross_exposure_ratio"] == pytest.approx(100 / target)
    assert negative_cash_ratio(out, {}, nav_override=nav)["negative_cash_cny"] == 0
    assert position_weight(out, {}, nav_override=nav).iloc[0]["position_weight"] == pytest.approx(100 / target)


@pytest.mark.parametrize("value,completeness", [("NaN", "complete"), (100, "partial")])
def test_incomplete_or_invalid_published_valuation_is_unavailable(value, completeness):
    positions = pd.DataFrame([{"account_code": "broker", "asset_id": "QQQ", "asset_name": "QQQ",
                               "asset_type": "us_stock", "market_value_cny": 100.0}])
    _, _, meta = _positions_reconciled_to_published_nav(positions, {}, {
        "valuation_date": date(2026, 6, 18), "status": "published",
        "data_completeness": completeness, "net_liquidation_value": value})
    assert meta["reconciliation_status"] == "unavailable"
    assert meta["exposure_scope_status"] == "incomplete"


def test_positions_reconciled_to_published_nav_skips_stale_state():
    positions = pd.DataFrame(
        [
            {
                "as_of": "2026-06-19",
                "account_code": "broker",
                "account_name": "Broker",
                "asset_id": "QQQ",
                "asset_name": "QQQ",
                "asset_type": "us_stock",
                "market_value_cny": 100.0,
            }
        ]
    )

    out, nav, meta = _positions_reconciled_to_published_nav(
        positions,
        {},
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": 150.0,
            "assets_cny": 150.0,
            "liabilities_cny": 0.0,
        },
    )

    assert nav == 100.0
    assert "VALUATION_STATE_ADJUSTMENT" not in set(out["asset_id"])
    assert meta["overlay_applied"] is False
    assert meta["overlay_reason"] == "stale_valuation_state"
    assert meta["raw_positions_nav_cny"] == 100.0
    assert meta["nav_reconciliation_cny"] == 0.0


def test_apply_published_valuation_state_to_return_series_payload_overlays_terminal_value_only():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-17", "market_value": "100", "total_pnl": "8"},
            {"date": "2026-06-18", "market_value": "110", "total_pnl": "9"},
        ],
    }

    out = apply_published_valuation_state_to_return_series_payload(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
        },
    )

    assert out["rows"][0]["market_value"] == "100"
    assert out["rows"][1]["market_value"] == "260.00"
    assert out["rows"][1]["net_liquidation_value"] == "260.00"
    assert out["rows"][1]["total_pnl"] == "9"
    assert out["valuation_state"]["overlay_applied"] is True


def test_apply_published_valuation_state_to_return_series_payload_skips_date_mismatch():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-18", "market_value": "100", "total_pnl": "8"},
            {"date": "2026-06-19", "market_value": "110", "total_pnl": "9"},
        ],
    }

    out = apply_published_valuation_state_to_return_series_payload(
        payload,
        {
            "valuation_date": date(2026, 6, 18),
            "cycle": "manual_publish",
            "status": "published",
            "data_completeness": "complete",
            "net_liquidation_value": Decimal("260.00"),
            "assets_cny": Decimal("380.00"),
            "liabilities_cny": Decimal("120.00"),
        },
    )

    assert out["rows"][1]["market_value"] == "110"
    assert out["rows"][1].get("net_liquidation_value") is None
    assert out["valuation_state"]["overlay_applied"] is False
    assert out["valuation_state"]["overlay_reason"] == "date_mismatch"


def test_apply_summary_pnl_to_return_series_payload_uses_summary2_terminal_pnl():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {
                "date": "2026-06-20",
                "market_value": "1000",
                "cost": "900",
                "position_pnl": "80",
                "realized_income": "5",
                "total_pnl": "85",
                "return_rate": "0.094444444444444444",
            },
            {
                "date": "2026-06-21",
                "market_value": "1100",
                "cost": "1000",
                "position_pnl": "90",
                "realized_income": "5",
                "total_pnl": "95",
                "return_rate": "0.095",
            },
        ],
    }

    out = apply_summary_pnl_to_return_series_payload(
        payload,
        {
            "as_of": "2026-06-21",
            "totals": {
                "market_value": "1200",
                "holding_pnl": "123.45",
                "current_float_pnl": "100.00",
                "closed_realized_pnl": "23.45",
            },
        },
    )

    assert out["rows"][0]["total_pnl"] == "85"
    assert out["rows"][1]["market_value"] == "1200"
    assert out["rows"][1]["net_liquidation_value"] == "1200"
    assert out["rows"][1]["total_pnl"] == "123.45"
    assert out["rows"][1]["position_pnl"] == "100.00"
    assert out["rows"][1]["realized_income"] == "23.45"
    assert out["rows"][1]["return_rate"] == "0.12345"
    assert out["rows"][1]["return_pnl_source"] == "summary2.latest"
    assert out["return_pnl_state"]["overlay_applied"] is True


def test_apply_summary_pnl_to_return_series_payload_skips_date_mismatch():
    payload = {
        "method": "holding_return_snapshot",
        "rows": [
            {"date": "2026-06-20", "market_value": "1000", "total_pnl": "85"},
            {"date": "2026-06-21", "market_value": "1100", "total_pnl": "95"},
        ],
    }

    out = apply_summary_pnl_to_return_series_payload(
        payload,
        {
            "as_of": "2026-06-22",
            "totals": {"holding_pnl": "123.45"},
        },
    )

    assert out["rows"][1]["total_pnl"] == "95"
    assert out["return_pnl_state"]["overlay_applied"] is False
    assert out["return_pnl_state"]["overlay_reason"] == "date_mismatch"


def test_apply_holdings_category_pnl_to_section_return_series_payload_uses_category_rows():
    payload = {
        "start": "2026-01-01",
        "summary": {"total_return": "10", "total_return_rate": "0.1", "annualized_return_rate": "0.2"},
        "rows": [
            {"date": "2026-06-20", "market_value": "90", "display_return_rate": "0.1"},
            {"date": "2026-06-21", "market_value": "100", "display_return_rate": "0.2"},
        ],
    }
    holdings_payload = {
        "as_of": "2026-06-21",
        "rows": [
            {
                "market_value_cny": "120",
                "cost_cny": "100",
                "holding_pnl_cny": "20",
                "daily_total_pnl_cny": "2",
                "is_closed": False,
            },
            {
                "market_value_cny": "0",
                "cost_cny": "50",
                "holding_pnl_cny": "5",
                "daily_total_pnl_cny": "0",
                "is_closed": True,
            },
        ],
    }

    out = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings_payload, source="test")

    first = out["rows"][0]
    latest = out["rows"][-1]
    assert first == payload["rows"][0]
    assert latest["market_value"] == "120"
    assert latest["total_pnl"] == "25"
    assert latest["position_pnl"] == "20"
    assert latest["realized_income"] == "5"
    assert latest["cost"] == "150"
    assert latest["return_rate"] == "0.1666666666666666666666666667"
    assert latest["display_return_rate"] == "0.1666666666666666666666666667"
    assert latest["daily_return_rate"] is None
    assert latest["daily_return_amount"] == "2"
    assert out["summary"]["total_return"] == "25"
    assert out["summary"]["total_return_rate"] == "0.1666666666666666666666666667"
    assert out["summary"]["annualized_return_rate"] is None
    assert out["section_pnl_state"]["overlay_applied"] is True
    assert out["section_pnl_state"]["row_count"] == 2
    assert out["section_pnl_state"]["historical_values_recalibrated"] is False


def test_apply_holdings_category_pnl_to_section_return_series_payload_skips_date_mismatch():
    payload = {"rows": [{"date": "2026-06-21", "market_value": "100", "total_pnl": "10"}]}
    holdings_payload = {"as_of": "2026-06-22", "rows": [{"holding_pnl_cny": "25", "cost_cny": "100"}]}

    out = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings_payload)

    assert out["rows"][-1]["total_pnl"] == "10"
    assert out["section_pnl_state"]["overlay_applied"] is False
    assert out["section_pnl_state"]["overlay_reason"] == "date_mismatch"


def test_category_daily_pnl_keeps_known_value_but_unknown_if_any_member_missing():
    payload={'rows':[{'date':'2026-06-21','daily_return_amount':'99','daily_return_rate':'0.1'}]}
    holdings={'as_of':'2026-06-21','rows':[
        {'market_value_cny':'120','cost_cny':'100','holding_pnl_cny':'20','daily_pnl_cny':'2'},
        {'market_value_cny':'40','cost_cny':'40','holding_pnl_cny':'0','daily_pnl_cny':None}]}
    result=apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)
    latest=result['rows'][-1]
    assert latest['market_value']=='160' and latest['total_pnl']=='20'
    assert latest['daily_return_amount'] is None and latest['daily_return_rate'] is None
    holdings['rows'][1]['daily_pnl_cny']='0'
    assert apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)['rows'][-1]['daily_return_amount']=='2'
    holdings['rows'][0]['daily_pnl_cny']='0'
    assert apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)['rows'][-1]['daily_return_amount']=='0'


def test_category_unknown_cost_or_value_never_preserves_stale_income_or_invents_value():
    payload={'summary':{'total_return':'100'},'rows':[{'date':'2026-06-21','market_value':'999',
             'daily_return_amount':'99','daily_return_rate':'0.2'}]}
    holdings={'as_of':'2026-06-21','rows':[
        {'market_value_cny':'120','cost_cny':None,'holding_pnl_cny':None,'daily_pnl_cny':None}]}
    result=apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)
    assert result['rows'][-1]['market_value']=='120' and result['summary']['total_return'] is None
    assert result['rows'][-1]['daily_return_amount'] is None and result['rows'][-1]['daily_return_rate'] is None
    holdings['rows'][0]['market_value_cny']=None
    assert apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)['rows'][-1]['market_value'] is None


def test_category_zero_cost_does_not_invent_rate():
    payload={'rows':[{'date':'2026-06-21','daily_return_rate':'0.2'}]}
    holdings={'as_of':'2026-06-21','rows':[{'market_value_cny':'0','cost_cny':'0',
               'holding_pnl_cny':'0','daily_pnl_cny':'0'}]}
    result=apply_holdings_category_pnl_to_section_return_series_payload(payload,holdings)
    assert result['rows'][-1]['market_value']=='0' and result['rows'][-1]['daily_return_amount']=='0'
    assert result['rows'][-1]['return_rate'] is None and result['rows'][-1]['daily_return_rate'] is None
    assert result['summary']['total_return_rate'] is None


def test_category_overlay_preserves_negative_and_zero_terminal_history_without_interpolation():
    payload = {'start': '2026-06-19', 'rows': [
        {'date': '2026-06-19', 'display_return_rate': '-0.123456789', 'total_pnl': '-12.3456789',
         'daily_return_rate': '-0.04', 'source_tag': 'original'},
        {'date': '2026-06-20', 'display_return_rate': '0', 'total_pnl': '0'},
        {'date': '2026-06-21', 'display_return_rate': '0', 'total_pnl': '0'},
    ]}
    holdings = {'as_of': '2026-06-21', 'rows': [
        {'market_value_cny': '120', 'cost_cny': '100', 'holding_pnl_cny': '20', 'daily_pnl_cny': '0'}]}
    result = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings)
    assert result['rows'][:-1] == payload['rows'][:-1]
    assert result['rows'][-1]['return_rate'] == '0.2'
    assert result['rows'][-1]['daily_return_rate'] is None
    assert result['rows'][-1]['daily_return_amount'] == '0'
    assert result['summary']['annualized_return_rate'] is None


def test_category_unknown_current_cost_preserves_known_historical_rows():
    payload = {'summary': {'total_return': '10'}, 'rows': [
        {'date': '2026-06-20', 'cost': '50', 'total_pnl': '-5', 'display_return_rate': '-0.1',
         'daily_return_rate': '-0.02'},
        {'date': '2026-06-21', 'cost': '50', 'total_pnl': '10'}]}
    holdings = {'as_of': '2026-06-21', 'rows': [
        {'market_value_cny': '120', 'cost_cny': None, 'holding_pnl_cny': None, 'daily_pnl_cny': '2'}]}
    result = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings)
    assert result['rows'][0] == payload['rows'][0]
    assert result['rows'][-1]['total_pnl'] is None
    assert result['rows'][-1]['market_value'] == '120'
    assert result['rows'][-1]['daily_return_amount'] == '2'
    assert result['rows'][-1]['daily_return_rate'] is None


def test_category_overlay_preserves_existing_rates_when_terminal_basis_matches():
    payload = {'summary': {'total_return': '20', 'annualized_return_rate': '0.3'}, 'rows': [
        {'date': '2026-06-20', 'display_return_rate': '-0.1'},
        {'date': '2026-06-21', 'market_value': '120', 'cost': '100', 'total_pnl': '20',
         'return_rate': '0.2', 'display_return_rate': '0.2', 'daily_return_amount': '2',
         'daily_return_rate': '0.01', 'time_weighted_return_rate': '0.19'}]}
    holdings = {'as_of': '2026-06-21', 'rows': [
        {'market_value_cny': '120', 'cost_cny': '100', 'holding_pnl_cny': '20', 'daily_pnl_cny': '2'}]}
    result = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings)
    assert result['rows'][0] == payload['rows'][0]
    assert result['rows'][-1]['daily_return_rate'] == '0.01'
    assert result['rows'][-1]['time_weighted_return_rate'] == '0.19'
    assert result['summary']['annualized_return_rate'] == '0.3'
    assert result['section_pnl_state']['daily_and_annualized_rate_reason'] == 'original_rates_retained_basis_unverified'


@pytest.mark.parametrize('holdings_date,terminal_date', [(None, '2026-06-21'), ('2026-06-21', None),
                                                          ('not-a-date', '2026-06-21')])
def test_category_overlay_refuses_unverified_dates(holdings_date, terminal_date):
    payload = {'rows': [{'date': terminal_date, 'total_pnl': '10'}]}
    holdings = {'as_of': holdings_date, 'rows': [
        {'market_value_cny': '120', 'cost_cny': '100', 'holding_pnl_cny': '20'}]}
    result = apply_holdings_category_pnl_to_section_return_series_payload(payload, holdings)
    assert result['rows'] == payload['rows']
    assert result['section_pnl_state']['overlay_reason'] == 'date_unverified'
