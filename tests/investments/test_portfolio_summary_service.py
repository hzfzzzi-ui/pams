from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.investments.portfolio_summary import (  # noqa: E402
    apply_return_attribution_pnl_to_summary,
    build_holdings_by_account_payload,
    build_holdings_summary_payload,
    build_summary2_from_holdings_latest_payload,
)


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_build_holdings_summary_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "major_category": "A股",
                    "holdings_count": 2,
                    "total_value_base": Decimal("100.25"),
                    "total_cost_base": Decimal("90.00"),
                    "total_pnl_base": Decimal("10.25"),
                },
                {
                    "major_category": "美股",
                    "holdings_count": 1,
                    "total_value_base": Decimal("50.00"),
                    "total_cost_base": Decimal("45.00"),
                    "total_pnl_base": Decimal("5.00"),
                },
            ],
            [],
        ]
    )

    payload = build_holdings_summary_payload(
        cur,
        base_currency="CNY",
        view_as_of=date(2026, 6, 1),
        owner_ratio_sql="1",
        owner_ratio_params=[],
    )

    assert set(payload) == {"as_of", "base_currency", "total", "by_category"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["total"] == {"value": "150.25", "cost": "135.0", "pnl": "15.25"}
    assert payload["by_category"][0]["total_value_base"] == "100.25"
    sql, params = cur.executed[1]
    assert "FROM latest_known_holding hs" in sql
    assert "latest_position" not in sql
    assert params == (date(2026, 6, 1), "CNY", date(2026, 6, 1))


def test_build_holdings_by_account_payload_preserves_legacy_shape():
    cur = FakeCursor(
        [
            {"as_of": date(2026, 5, 31)},
            [
                {
                    "account_code": "ibkr",
                    "display_name": "IBKR",
                    "account_currency": "USD",
                    "holdings_count": 3,
                    "total_value_base": Decimal("200.00"),
                }
            ],
            [],
        ]
    )

    payload = build_holdings_by_account_payload(
        cur,
        base_currency="CNY",
        view_as_of=date(2026, 6, 1),
        owner_ratio_sql="1",
        owner_ratio_params=[],
    )

    assert set(payload) == {"as_of", "base_currency", "by_account"}
    assert payload["as_of"] == "2026-06-01"
    assert payload["by_account"][0]["account_code"] == "ibkr"
    assert payload["by_account"][0]["total_value_base"] == "200.00"
    sql, params = cur.executed[1]
    assert "FROM latest_known_holding hs" in sql
    assert "latest_position" not in sql
    assert params == (date(2026, 6, 1), "CNY", date(2026, 6, 1))


def test_build_summary2_from_holdings_latest_payload_nets_liabilities():
    payload = build_summary2_from_holdings_latest_payload(
        {
            "as_of": "2026-06-16",
            "rows": [
                {
                    "account": "ibkr",
                    "asset_liability": "资产",
                    "major_category": "美股",
                    "symbol": "QQQ",
                    "market_value_cny": "300.00",
                    "holding_pnl_cny": "10.00",
                    "daily_total_pnl_cny": "3.00",
                },
                {
                    "account": "ibkr",
                    "asset_liability": "负债",
                    "major_category": "借款",
                    "symbol": "CASH_USD",
                    "market_value_cny": "-120.00",
                    "holding_pnl_cny": "0",
                    "daily_total_pnl_cny": "0",
                },
            ],
        },
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "ibkr",
                "display_name": "IBKR",
                "account_type": "brokerage",
                "currency": "CNY",
                "amount_cny": "180.00",
                "include_in_nav": True,
            },
            {
                "account_code": "ccb_7332",
                "display_name": "建行7332",
                "account_type": "funding",
                "currency": "CNY",
                "amount_cny": "80.00",
                "include_in_nav": True,
            },
        ],
    )

    assert payload["totals"]["market_value"] == "260.00"
    assert {row["asset_class"]: row["market_value"] for row in payload["by_asset_class"]} == {
        "美股": "300.00",
        "现金": "80.00",
        "LIABILITY": "-120.00",
    }


def test_build_summary2_from_holdings_latest_payload_adds_represented_account_residual():
    payload = build_summary2_from_holdings_latest_payload(
        {
            "as_of": "2026-06-17",
            "rows": [
                {
                    "account": "tonghuashun",
                    "asset_liability": "资产",
                    "major_category": "A股",
                    "symbol": "601899",
                    "market_value_cny": "240.00",
                    "holding_pnl_cny": "12.00",
                    "daily_total_pnl_cny": "2.00",
                },
            ],
        },
        base_currency="CNY",
        account_balances=[
            {
                "account_code": "tonghuashun",
                "display_name": "同花顺",
                "account_type": "brokerage",
                "currency": "CNY",
                "amount_cny": "246.18",
                "include_in_nav": True,
            },
        ],
    )

    assert payload["totals"]["market_value"] == "246.18"
    assert {row["asset_class"]: row["market_value"] for row in payload["by_asset_class"]} == {
        "A股": "240.00",
        "现金": "6.18",
    }


def test_apply_return_attribution_pnl_to_summary_splits_total_and_current_float():
    payload = {
        "as_of": "2026-06-18",
        "base_currency": "CNY",
        "totals": {"market_value": "300.00", "holding_pnl": "12.00", "daily_pnl": "3.00"},
        "by_asset_class": [],
        "children": [],
    }
    attribution = {
        "total_pnl_cny": "17.00",
        "dimensions": {
            "holding": [
                {"name": "Current", "market_value_cny": "300.00", "pnl_cny": "12.00"},
                {"name": "Closed", "market_value_cny": "0", "pnl_cny": "5.00"},
            ]
        },
    }

    out = apply_return_attribution_pnl_to_summary(payload, attribution)

    assert out["totals"]["holding_pnl"] == "17.00"
    assert out["totals"]["current_float_pnl"] == "12.00"
    assert out["totals"]["closed_realized_pnl"] == "5.00"


def test_apply_return_attribution_pnl_to_summary_prefers_canonical_total():
    payload = {
        "as_of": "2026-06-19",
        "base_currency": "CNY",
        "totals": {"market_value": "300.00", "holding_pnl": "12.00", "daily_pnl": "0"},
        "by_asset_class": [],
        "children": [],
    }
    attribution = {
        "total_pnl_cny": "17.00",
        "canonical_total_pnl_cny": "20.00",
        "dimensions": {
            "holding": [
                {"name": "Current", "market_value_cny": "300.00", "pnl_cny": "12.00"},
            ]
        },
    }

    out = apply_return_attribution_pnl_to_summary(payload, attribution)

    assert out["totals"]["holding_pnl"] == "20.00"
    assert out["totals"]["current_float_pnl"] == "12.00"
    assert out["totals"]["closed_realized_pnl"] == "8.00"


def test_summary2_unknown_stale_cash_and_empty_are_not_zero():
    from domain.investments.portfolio_summary import build_summary2_from_holdings_latest_payload as build
    empty=build({'as_of':None,'rows':[]},base_currency='CNY',account_balances=[])
    assert empty['totals']=={'market_value':None,'holding_pnl':None,'daily_pnl':None}
    assert empty['daily_pnl_status']=='unknown'
    row={'account':'demo','currency':'CNY','major_category':'股票','market_value_cny':'100',
         'holding_pnl_cny':'10','daily_pnl_cny':None}
    unknown=build({'as_of':'2026-10-30','rows':[row]},base_currency='CNY')
    assert unknown['totals']=={'market_value':'100','holding_pnl':'10','daily_pnl':None}
    assert unknown['by_asset_class'][0]['daily_pnl'] is None and unknown['children'][0]['daily_pnl'] is None
    zero=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'0','daily_pnl_status':'confirmed'}]},base_currency='CNY')
    assert zero['totals']['daily_pnl']=='0' and zero['daily_pnl_status']=='confirmed'
    stale=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'0','daily_pnl_stale':True}]},base_currency='CNY')
    assert stale['totals']['daily_pnl'] is None
    pending=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'3','daily_pnl_status':'pending'}]},base_currency='CNY')
    assert pending['totals']['daily_pnl'] is None and pending['children'][0]['daily_pnl_status']=='unknown'
    estimated=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'3','daily_pnl_status':'estimated'}]},base_currency='CNY')
    assert estimated['totals']['daily_pnl']=='3' and estimated['daily_pnl_status']=='estimated'
    malformed=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'NaN','daily_pnl_status':'confirmed'}]},base_currency='CNY')
    assert malformed['totals']['daily_pnl'] is None and malformed['children'][0]['daily_pnl_status']=='unknown'
    cash={'account_code':'bank','account_type':'funding','currency':'CNY','amount_cny':'20','include_in_nav':True}
    mixed=build({'as_of':'2026-10-30','rows':[{**row,'daily_pnl_cny':'5'}]},base_currency='CNY',account_balances=[cash])
    assert mixed['totals']['market_value']=='120' and mixed['totals']['daily_pnl'] is None
    assert mixed['totals']['holding_pnl'] is None
    cash_child=next(item for item in mixed['children'] if item['account']=='bank')
    assert cash_child['market_value']=='20' and cash_child['daily_pnl'] is None
    missing=build({'as_of':'2026-10-30','rows':[{**row,'market_value_cny':None}]},base_currency='CNY')
    assert missing['totals']['market_value'] is None and missing['value_status']=='incomplete'
    confirmed_cash_zero=build({'as_of':'2026-10-30','rows':[]},base_currency='CNY',account_balances=[{**cash,'amount_cny':'0'}])
    assert confirmed_cash_zero['totals']['market_value']=='0' and confirmed_cash_zero['totals']['daily_pnl'] is None


def test_summary2_stale_valuation_cannot_override_empty_or_fx_unknown():
    from domain.investments.portfolio_summary import apply_published_valuation_state_to_summary as apply
    candidate={'valuation_date':'2026-10-30','net_liquidation_value':'999'}
    for payload in ({'totals':{'market_value':None}},
                    {'totals':{'market_value':None},'fx_status':'incomplete'},
                    {'totals':{'market_value':None},'value_status':'incomplete'}):
        result=apply(payload,candidate)
        assert result['totals']['market_value'] is None and not result['valuation_state']['overlay_applied']
