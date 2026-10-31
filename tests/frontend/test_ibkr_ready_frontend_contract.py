"""Exercise existing IBKR display bindings offline with synthetic DTOs only."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "frontend/public/assets/js/holdings_categories.js").read_text(encoding="utf-8")
UTILS = (ROOT / "frontend/public/assets/js/utils.js").read_text(encoding="utf-8")
DAY = "2024-01-05"


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


def function(name, source=SOURCE):
    match = re.search(r"function " + name + r"\([\s\S]*?\n\}(?=\n|$)", source)
    assert match, name
    return match.group()


def payload(day=DAY, rate=-0.1, capital=770):
    return {
        "status": "ok", "as_of": day, "net_liquidation_cny": 900,
        "position_value_cny": 1200, "cash_cny": -300,
        "net_deposit_cny": 1000, "total_pnl_cny": -100, "return_ratio": rate,
        "daily_close": {"status": "ok", "fx_status": "available",
                        "source_account_id": "SYNTHETIC", "source_sha256": "a" * 64},
        "portfolio_account": {
            "status": "ok", "account_code": "ibkr", "ownership_basis": "owner_adjusted",
            "owner_ratio": 1, "currency": "CNY", "as_of": day,
            "source_account_id": "SYNTHETIC", "source_sha256": "a" * 64,
            "net_liquidation_cny": 700, "total_pnl_cny": -70,
            "return_capital_cny": capital, "cash_cny": -300,
            "position_value_cny": 1000, "return_ratio": rate,
        },
    }


def render(perf, *, holdings=True):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required; mandatory in final CI")
    functions = [
        "isoDateKey", "isSameOrAfterDate", "confirmedIbkrAccountSummary", "renderIbkrPerformance",
        "cnyValue", "accountNetLiquidationFromHoldings", "fxRateForCurrency", "accountPrimaryBalanceCny",
        "holdingReturnRateFromSummary", "categoryReturnOverride", "categoryMetric", "categoryByLabel",
        "makeMobileHeroProfiles", "MobilePortfolioHero", "mobileHeroMetaItem", "esc",
        "pctFromAmount", "signedPctText", "shareTone", "writeSectionFinancials", "fmtPctValue",
    ]
    script = function("safeNum", UTILS) + "\n" + "\n".join(function(name) for name in functions)
    script += """
const elements = {};
const document = {getElementById: id => elements[id] ||= {style:{}}};
const setKpiLabel = () => {};
const fmtUsd = String, fmtSignedUsd = String, fmtSignedPctFromRate = String;
const fmtCny = String, fmtSignedCny = String, formatOptionalMoney = String;
const formatSyncTimestamp = (_sync, asOf) => asOf;
const accountCashBreakdown = () => ({netCash:0, cashAssets:0, cashLiabilities:0});
"""
    script += "\nconst ibkrPerf = " + json.dumps(perf) + ";\n"
    script += "const usStock = {as_of:" + json.dumps(DAY) + "};\n"
    script += "const latestHoldings = " + json.dumps({"as_of": DAY, "rows": [
        {"account": "ibkr", "market_value_cny": 1600},
        {"account": "ibkr", "market_value_cny": -500, "asset_liability": "\u8d1f\u503a"},
    ] if holdings else []}) + ";\n"
    script += """
const fxRates = {}, accountOverview = {accounts:[]};
const usSummary = {totalValue:1600,totalCost:1700,totalPnl:-100,dailyPnl:1,
    dailyTotalPnl:1,fxDailyPnl:0,holdingReturnRate:0.125,activeTotalCost:1700,activeHoldingPnl:-100};
const ibkrSummary = renderIbkrPerformance(ibkrPerf, usStock.as_of);
const confirmedIbkrSummary = confirmedIbkrAccountSummary(ibkrPerf, usStock.as_of);
"""
    start = SOURCE.index("    const ibkrFlexCoversPositionDate")
    end = SOURCE.index("    if (ibkrUsesNetLiquidation)", start)
    script += SOURCE[start:end]
    script += """
const category = categoryMetric('\u7f8e\u80a1/\u6d77\u5916', usCategorySummary, null);
const profiles = makeMobileHeroProfiles({summary:{marketValue:2000},categories:[category],
    accountOverview,fxRates,aStockRowsSummary:{},aStockSummary:{},fundDisplaySummary:{},
    fundSummary:{},usSummary,ibkrSummary:usCategorySummary,preciousSummary:{},esopSummary:{}});
const profile = profiles['\u7f8e\u80a1/\u6d77\u5916'];
const html = MobilePortfolioHero(profile, usStock.as_of);
writeSectionFinancials(usCategorySummary, 'value', 'pnl', 'daily', 'rate');
console.log(JSON.stringify({confirmed:confirmedIbkrSummary,summary:usCategorySummary,
    category,profile,html,section:Object.fromEntries(['value','pnl','daily','rate'].map(id=>[id,elements[id].textContent]))}));
"""
    result = subprocess.run([node, "-"], input=script, check=False, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def hero_total_rate(result):
    return re.findall(r"<em[^>]*>(.*?)</em>", result["html"])[-1]


@pytest.mark.parametrize("day", ["2024-01-04", None, "invalid"])
def test_stale_or_undated_close_cannot_supply_current_account_rate(day):
    result = render(payload(day))
    assert result["confirmed"] is None
    assert result["summary"]["accountReturnRate"] is None
    assert result["summary"]["accountReturnRateUndefined"] is True
    assert result["category"]["accountReturnRateUndefined"] is True
    assert result["profile"]["totalRateUndefined"] is True
    assert hero_total_rate(result) == "--"
    assert DAY in result["profile"]["totalRateTitle"]
    if day == "2024-01-04":
        assert day in result["profile"]["totalRateTitle"]
        assert result["category"]["accountReturnRateEvidenceAsOf"] == day
    else:
        assert result["category"]["accountReturnRateEvidenceAsOf"] is None
    assert result["section"]["rate"] == "--"
    assert result["profile"]["value"] == 1100
    assert result["profile"]["totalPnl"] == -100
    assert result["profile"]["miniMetrics"][0]["value"] == "1600"
    assert result["profile"]["miniMetrics"][1]["value"] == "-500"


@pytest.mark.parametrize("rate", [-0.1, 0])
def test_same_day_confirmed_rate_and_personal_amounts_stay_directly_bound(rate):
    result = render(payload(rate=rate))
    assert result["summary"]["accountReturnRate"] == rate
    assert result["summary"]["accountReturnRateUndefined"] is False
    assert result["profile"]["value"] == 700
    assert result["profile"]["totalPnl"] == -70
    assert DAY in result["profile"]["totalRateTitle"]
    assert result["category"]["accountReturnRateAsOf"] == DAY
    assert hero_total_rate(result) == ("+0.00%" if rate == 0 else "-10.00%")


def test_undefined_confirmed_rate_is_not_coerced_to_a_zero_on_the_mobile_card():
    result = render(payload(rate=None, capital=0))
    assert result["confirmed"]["totalCost"] == 0
    assert result["summary"]["totalValue"] == 700
    assert result["summary"]["totalPnl"] == -70
    assert result["category"]["accountReturnRate"] is None
    assert hero_total_rate(result) == "--"
    assert result["section"]["rate"] == "--"


def test_same_day_legacy_account_formula_is_preserved():
    data = payload()
    data.pop("daily_close")
    result = render(data)
    assert result["profile"]["value"] == 1300
    assert result["profile"]["totalPnl"] == 300
    assert hero_total_rate(result) == "+30.00%"


def test_positions_only_fallback_keeps_its_own_rate_not_an_old_account_rate():
    result = render(payload("2024-01-04"), holdings=False)
    assert result["summary"]["accountReturnRate"] is None
    assert result["summary"]["accountReturnRateUndefined"] is False
    assert hero_total_rate(result) == "+12.50%"


def test_unverified_close_date_is_not_advertised_as_official_rate_evidence():
    data = payload("2024-01-04")
    data["portfolio_account"]["source_sha256"] = "b" * 64
    result = render(data)
    assert hero_total_rate(result) == "--"
    assert result["category"]["accountReturnRateEvidenceAsOf"] is None
    assert "2024-01-04" not in result["profile"]["totalRateTitle"]
    assert DAY in result["profile"]["totalRateTitle"]


def test_investment_entry_uses_a_distinct_binding_module_cache_key():
    html = (ROOT / "frontend/public/investments.html").read_text(encoding="utf-8")
    assert "holdings_categories.js?v=20261031-investment-charts-v1" in html
