"""Run the real holding and hero renderers offline against synthetic DTOs."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function load(file, globals) {
  const source = fs.readFileSync('frontend/public/assets/js/' + file, 'utf8')
    .replace(/^\uFEFF/, '').replace(/^import .*;\r?$/gm, '').split(/\r?\nmain\(\)\./)[0];
  const context = vm.createContext(globals);
  vm.runInContext(source, context, {filename: file});
  return context;
}
function element() {
  return {innerHTML: '', children: [], dataset: {}, listeners: {},
    classList: {toggle() {}},
    appendChild(node) {this.children.push(node);},
    addEventListener(name, listener) {this.listeners[name] = listener;}};
}
const table = element(), mobile = element();
let buttons = [], sheet = '';
mobile.querySelectorAll = () => buttons;
const rowContext = load('holdings.js', {
  URLSearchParams,
  window: {location: {search: ''}, matchMedia: () => ({matches: false})},
  document: {createElement: element,
    getElementById: id => ({rows: table, mobileHoldingList: mobile})[id]},
  typeLabel: value => value, accountLabel: value => value,
  pnlPct: () => 0, openBottomSheet: (_title, html) => {sheet = html;},
});
const before = JSON.stringify(input);
const renders = input.rows.map(row => {
  const button = element();
  button.dataset.mobileHoldingIndex = '0';
  buttons = [button];
  table.children = [];
  rowContext.renderRows([row]);
  button.listeners.click();
  return {mobile: mobile.innerHTML, table: table.children[0].innerHTML,
    detail: table.children[1].innerHTML, sheet,
    native: rowContext.dailyNativeText(row), fx: rowContext.dailyFxText(row),
    badge: rowContext.holdingDailyStatusBadge(row),
    pending: rowContext.holdingDailyPnlPending(row)};
});
const aggregate = rowContext.aggregateMobileHoldingRows(input.aggregateRows || []);
const safeNum = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const heroContext = load('holdings_categories.js', {
  safeNum, fmt2: value => safeNum(value).toFixed(2),
  window: {addEventListener() {}},
  accountCashBreakdown: () => ({netCash: 0, cashAssets: 0, cashLiabilities: 0}),
});
const profileInput = {
  summary: {marketValue: 900, dailyPnl: input.daily, totalPnl: -70, twr: -0.1},
  categories: [{label: '\u7f8e\u80a1/\u6d77\u5916', dailyPnl: input.daily, totalPnl: -70}],
  usRows: input.rows, usSummary: {totalValue: 1000},
  recentUsReturn: input.recent,
  ibkrSummary: {flexCoversPositionDate: true, totalValue: 900,
    positionValue: 1000, cashValue: -100, totalPnl: -70},
  aStockRowsSummary: {}, aStockSummary: {}, fundDisplaySummary: {},
  fundSummary: {}, preciousSummary: {}, esopSummary: {},
};
const profileBefore = JSON.stringify(profileInput);
const profiles = heroContext.makeMobileHeroProfiles(profileInput);
const us = profiles['\u7f8e\u80a1/\u6d77\u5916'];
const all = profiles['\u5168\u90e8'];
const html = heroContext.MobilePortfolioHero(us, '2024-01-05');
const allHtml = heroContext.MobilePortfolioHero(all, '2024-01-05');
console.log(JSON.stringify({renders, aggregate, profile: us, allProfile: all, html, allHtml,
  unchanged: before === JSON.stringify(input) && profileBefore === JSON.stringify(profileInput)}));
"""


def holding(**fields):
    return {
        "symbol": "TEST", "name": "Synthetic holding", "account": "ibkr",
        "major_category": "\u7f8e\u80a1", "minor_category": "\u7f8e\u80a1\u57fa\u91d1",
        "currency": "USD", "market_value_cny": 1200, "cost_cny": 1000,
        "holding_pnl_cny": 250, "realized_trade_pnl_cny": 50,
        "daily_pnl_cny": 0, "daily_total_pnl_cny": 0, "daily_pnl_native": 0,
        "daily_fx_pnl_cny": 0, "quantity": 100, "current_price": 12,
        "previous_price": 12, "buy_price": 10, **fields,
    }


def render(rows, *, daily=0, aggregate_rows=(), recent=None):
    node = shutil.which("node")
    assert node, "Node is required for frontend behavior verification"
    result = subprocess.run(
        [node, "-e", SCRIPT], cwd=ROOT,
        input=json.dumps({"rows": rows, "daily": daily, "aggregateRows": aggregate_rows, "recent": recent}),
        text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["unchanged"], "Display rendering must not change input amounts or status fields"
    return data


def recent_return():
    return {"as_of": "2026-10-27", "status": "confirmed", "label": "10/27收益",
            "status_label": "10/27已确认", "current_label": "10/28预估待更新",
            "fields": {"daily_pnl_native": "6", "daily_pnl_cny": "42.6", "daily_fx_pnl_cny": "-1.6",
                       "daily_total_pnl_cny": "41", "price_change_pct": "0.06", "previous_price": "10"}}


def test_mobile_metadata_is_below_name_and_quantity_padding_is_removed():
    data = render([holding(quantity="75.010", daily_return_status="confirmed",
                           valuation_updated_at="2026-10-28T20:30:00Z")])
    html = data["renders"][0]["mobile"]
    name, metadata = html.split('<span class="mobileHoldingMeta">', 1)
    assert "miniBadge" not in name
    assert "今日收益已确认" in metadata
    assert "mobileHoldingQuote" in metadata and "mobileHoldingSync" in metadata
    assert "75.01</strong>" in metadata and "75.010" not in metadata
    assert ">同步 " not in metadata


def test_last_confirmed_display_does_not_replace_today_or_recalculate_account_amount():
    recent = {"as_of": "2026-10-27", "label": "10/27收益", "amount_cny": "777.77", "partial": True,
              "title": "美股 2026-10-27 已确认；2026-10-28 预估待更新"}
    data = render([holding(daily_return_status="pending", daily_pnl_stale=True,
                           recent_confirmed_return=recent_return())], recent=recent)
    row = data["renders"][0]
    assert row["pending"] is True  # Canonical current status and old table remain pending.
    for html in (row["mobile"], row["sheet"]):
        assert "10/27收益" in html and "10/27已确认" in html
        assert "+¥41.00" in html and "10/28预估待更新" in html
        assert "今日收益已确认" not in html
    assert data["profile"]["dailyStatus"] == "historical"
    assert data["profile"]["dailyPnl"] == 777.77
    assert "10/27收益（部分）" in data["html"] and "+¥777.77" in data["html"]
    assert '>--</em>' in data["html"]
    assert data["allProfile"]["dailyPnl"] == 0  # No historical amount in today's portfolio summary.
    assert "待更新" in row["table"] and "+¥41.00" not in row["table"]


def test_current_estimate_or_partial_current_account_takes_precedence_over_old_close():
    data = render([holding(daily_return_status="estimated", daily_pnl_cny=23, daily_total_pnl_cny=23,
                           recent_confirmed_return=recent_return())], daily=23,
                  recent={"amount_cny": "999", "label": "10/27收益"})
    assert data["profile"]["dailyStatus"] == "estimated"
    assert data["profile"]["dailyPnl"] == 23
    assert "10/27收益" not in data["renders"][0]["mobile"]
    data = render([holding(daily_return_status="confirmed", daily_total_pnl_cny=23),
                   holding(daily_return_status="pending", recent_confirmed_return=recent_return())], daily=23,
                  recent={"amount_cny": "999", "label": "10/27收益"})
    assert data["profile"]["dailyStatus"] == "partial" and data["profile"]["dailyPnl"] == 23


def test_multiple_account_aggregate_never_uses_only_first_accounts_prior_return():
    data = render([holding(daily_return_status="pending")], aggregate_rows=[
        holding(account="a", major_category="A股", minor_category="A股基金", daily_return_status="pending", recent_confirmed_return=recent_return()),
        holding(account="b", major_category="A股", minor_category="A股基金", daily_return_status="pending", recent_confirmed_return=recent_return()),
    ])
    aggregate = data["aggregate"][0]
    assert aggregate["__isAggregated"] is True and aggregate["daily_total_pnl_cny"] == 0


@pytest.mark.parametrize("fields", [
    {"daily_return_status": "pending"},
    {"daily_pnl_stale": True},
    {"daily_return_status": "pending", "daily_pnl_status": "confirmed"},
    {"daily_return_status": "confirmed", "daily_pnl_stale": True},
    {"daily_return_status": "estimated", "daily_pnl_stale": True},
    {"daily_return_status": "pending", "daily_pnl_cny": 27, "daily_total_pnl_cny": 30,
     "daily_pnl_native": 4, "daily_fx_pnl_cny": 3},
])
def test_pending_or_stale_is_not_a_green_zero_in_any_renderer(fields):
    data = render([holding(**fields)])
    row = data["renders"][0]
    assert row["pending"] is True
    assert row["badge"] == ""
    for template in ("mobile", "table", "detail", "sheet"):
        html = row[template]
        assert "\u5f85\u66f4\u65b0" in html
        assert "+\u00a50.00" not in html
        assert "+0.00%" not in html
        assert ("\u00a51,000.00" if template == "mobile" else "\u00a51,200.00") in html
        assert "+\u00a5250.00" in html
    assert '<strong class="mono " style="color:var(--muted)">\u5f85\u66f4\u65b0</strong>' in row["mobile"]
    assert 'mono " style="color:var(--muted)">--</small>' in row["mobile"]
    assert 'class="mono muted"' not in row["mobile"]
    assert "-- USD" in row["mobile"] and "-- USD" in row["sheet"]
    assert ("\u8bc1\u5238\u6c47\u5151\u4f30\u7b97" in row["sheet"]) == (fields.get("daily_fx_pnl_cny", 0) != 0)
    for template in ("detail", "sheet"):
        assert '<strong>\u6536\u76ca\u72b6\u6001</strong><span>\u5f85\u66f4\u65b0</span>' in row[template]
    assert data["profile"]["dailyStatus"] == "pending"
    assert data["profile"]["dailyPnl"] == 0
    for html in (data["html"], data["allHtml"]):
        assert '<strong class="" style="color:var(--muted)">\u5f85\u66f4\u65b0</strong>' in html
        assert '<em class="" style="color:var(--muted)">--</em>' in html
        assert 'class="muted"' not in html


@pytest.mark.parametrize("status,label", [("confirmed", "\u4eca\u65e5\u6536\u76ca\u5df2\u786e\u8ba4"), ("estimated", "\u9884\u4f30\u6536\u76ca")])
@pytest.mark.parametrize("amount", [0, 23, -23])
def test_valid_zero_estimates_and_confirmations_keep_amounts_and_backend_status(status, label, amount):
    data = render([holding(daily_return_status=status, daily_pnl_cny=amount,
                           daily_total_pnl_cny=amount, daily_pnl_stale=False)], daily=amount)
    row = data["renders"][0]
    money = f"{'+' if amount >= 0 else '-'}\u00a5{abs(amount):.2f}"
    assert row["pending"] is False
    assert label in row["badge"]
    for html in (row["mobile"], row["table"], row["detail"], row["sheet"], data["html"]):
        assert money in html
        assert "\u5f85\u66f4\u65b0" not in html
    assert data["profile"]["dailyStatus"] == status
    assert data["profile"]["dailyPnl"] == amount
    assert data["profile"]["totalPnl"] == -70
    assert f'title="{label}"' in data["html"]


def test_confirmed_amount_does_not_hide_another_pending_position():
    data = render([holding(daily_return_status="confirmed", daily_pnl_cny=42,
                           daily_total_pnl_cny=42), holding(daily_return_status="pending")], daily=42)
    assert data["profile"]["dailyStatus"] == "partial"
    assert data["profile"]["dailyPnl"] == 42
    assert data["allProfile"]["dailyPnl"] == 42
    assert '\u4eca\u65e5\u76c8\u4e8f\uff08\u90e8\u5206\uff09' in data["html"]
    assert '+\u00a542.00' in data["html"]
    assert '>--</em>' in data["html"]


@pytest.mark.parametrize("cash_fields", [
    {"major_category": "\u73b0\u91d1"},
    {"minor_category": "\u73b0\u91d1/\u6d3b\u671f"},
    {"type": "\u73b0\u91d1"},
])
def test_negative_cash_quality_does_not_mark_account_pending(cash_fields):
    cash = holding(**cash_fields, market_value_cny=-100, daily_return_status="pending", daily_pnl_stale=True)
    data = render([holding(daily_return_status="confirmed"), cash])
    assert data["profile"]["dailyStatus"] == "confirmed"
    assert data["allProfile"]["dailyStatus"] == ""
    assert "+\u00a50.00" in data["html"]
    assert "\u5f85\u66f4\u65b0" not in data["html"]
    assert "-\u00a5100.00" in data["html"]


def test_closed_history_does_not_mask_current_account_and_unknown_is_not_confirmation():
    data = render([holding(daily_return_status="confirmed"), holding(is_closed=True, daily_return_status="pending")])
    assert data["profile"]["dailyStatus"] == "confirmed"
    assert render([holding(), holding(daily_return_status="confirmed")])["profile"]["dailyStatus"] == ""
    assert render([])["profile"]["dailyStatus"] == ""


@pytest.mark.parametrize("first_status,second_status,expected", [
    ("confirmed", "pending", "pending"),
    ("pending", "confirmed", "pending"),
    ("confirmed", "estimated", "estimated"),
])
def test_multiaccount_fund_aggregation_keeps_quality_separate_from_sum(first_status, second_status, expected):
    rows = [holding(major_category="\u0041\u80a1", minor_category="\u0041\u80a1\u57fa\u91d1",
                    daily_return_status=status, daily_return_status_label=label, daily_pnl_cny=amount,
                    daily_total_pnl_cny=amount)
            for status, label, amount in [(first_status, "First source label", 20), (second_status, "Second source label", 10)]]
    data = render([], aggregate_rows=rows)
    merged = data["aggregate"][0]
    assert merged["daily_pnl_cny"] == 30
    assert merged["daily_total_pnl_cny"] == 30
    assert merged["daily_return_status"] == expected
    display = render([merged])["renders"][0]
    if expected == "pending":
        assert "\u5f85\u66f4\u65b0" in display["mobile"]
        assert "+\u00a530.00" not in display["sheet"]
    else:
        assert "\u9884\u4f30\u6536\u76ca" in display["badge"]
        assert "+\u00a530.00" in display["sheet"]


@pytest.mark.parametrize("currency", ["CNY", "USD", "KRW"])
@pytest.mark.parametrize("fx", [0, 0.005, 3, -3])
def test_pending_keeps_native_and_fx_placeholders_only_where_lines_already_exist(currency, fx):
    fields = {"currency": currency, "daily_fx_pnl_cny": fx}
    valid = render([holding(**fields, daily_return_status="confirmed")])["renders"][0]
    pending = render([holding(**fields, daily_return_status="pending")])["renders"][0]
    assert bool(valid["native"]) == bool(pending["native"]) == (currency != "CNY")
    assert bool(valid["fx"]) == bool(pending["fx"]) == (currency != "CNY" and abs(fx) > 0.005)
    if pending["native"]:
        assert pending["native"] == f"-- {currency}"
    if pending["fx"]:
        assert pending["fx"] == "\u8bc1\u5238\u6c47\u5151 --"
    for label in ["\u672c\u5e01\u4eca\u65e5", "\u6c47\u7387\u5f71\u54cd"]:
        assert (label in valid["sheet"]) == (label in pending["sheet"])
    assert valid["mobile"].count('class="mobileHoldingFxImpact"') == pending["mobile"].count('class="mobileHoldingFxImpact"')
