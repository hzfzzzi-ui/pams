from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.market.fund_nav_estimates import (  # noqa: E402
    deviation_status,
    estimate_value,
    infer_units,
    intraday_estimates_enabled,
    is_cash_like_fund_holding,
    fetch_confirmed_nav_tushare,
    load_latest_active_fund_holdings,
    normalize_tushare_fund_ts_code,
    parse_confirmed_nav_rows,
    parse_intraday_estimate,
    parse_tushare_fund_nav_frame,
    tushare_fund_ts_code_candidates,
)


def test_parse_confirmed_nav_rows_uses_unit_nav_not_accumulated_nav() -> None:
    script = """
    var apidata={content:"<table><tbody><tr>
    <td>2026-05-29</td><td>1.3679</td><td>1.3679</td><td>-4.78%</td>
    </tr></tbody></table>",records:1,pages:1};
    """

    rows = parse_confirmed_nav_rows(script)

    assert rows[0].nav_date.isoformat() == "2026-05-29"
    assert rows[0].unit_nav == Decimal("1.367900")
    assert rows[0].growth_pct == Decimal("-4.780000")


def test_parse_intraday_estimate_from_fundgz_jsonp() -> None:
    script = (
        'jsonpgz({"fundcode":"011613","name":"华夏科创50ETF联接C",'
        '"jzrq":"2026-05-29","dwjz":"1.3679","gsz":"1.3050",'
        '"gszzl":"-4.60","gztime":"2026-06-01 14:49"});'
    )

    estimate = parse_intraday_estimate(script)

    assert estimate is not None
    assert estimate.estimate_time.isoformat(sep=" ") == "2026-06-01 14:49:00"
    assert estimate.previous_nav_date.isoformat() == "2026-05-29"
    assert estimate.estimated_unit_nav == Decimal("1.305000")


def test_normalize_tushare_fund_ts_code_for_otc_fund() -> None:
    assert normalize_tushare_fund_ts_code("110020") == "110020.OF"
    assert normalize_tushare_fund_ts_code("016440.OF") == "016440.OF"
    assert tushare_fund_ts_code_candidates("501016") == ["501016.OF", "501016.SH"]


def test_parse_tushare_fund_nav_frame_uses_latest_end_date() -> None:
    frame = pd.DataFrame(
        [
            {
                "ts_code": "110020.OF",
                "ann_date": "20260611",
                "nav_date": "20260610",
                "unit_nav": "1.9200",
                "accum_nav": "1.9200",
            },
            {
                "ts_code": "110020.OF",
                "ann_date": "20260613",
                "nav_date": "20260612",
                "unit_nav": "1.9313",
                "accum_nav": "1.9313",
            },
        ]
    )

    nav = parse_tushare_fund_nav_frame(frame)

    assert nav is not None
    assert nav.nav_date.isoformat() == "2026-06-12"
    assert nav.unit_nav == Decimal("1.931300")
    assert nav.accumulated_nav == Decimal("1.931300")
    assert nav.source == "tushare_fund_nav"
    assert nav.ts_code == "110020.OF"


def test_fetch_confirmed_nav_tushare_falls_back_to_exchange_suffix_for_lof() -> None:
    class FakePro:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fund_nav(self, ts_code: str, fields: str):
            self.calls.append(ts_code)
            if ts_code == "501016.SH":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": ts_code,
                            "ann_date": "20260613",
                            "nav_date": "20260612",
                            "unit_nav": "1.1495",
                            "accum_nav": "1.1495",
                        }
                    ]
                )
            return pd.DataFrame(columns=["ts_code", "ann_date", "nav_date", "unit_nav", "accum_nav"])

    pro = FakePro()

    nav = fetch_confirmed_nav_tushare("501016", pro=pro)

    assert nav is not None
    assert nav.ts_code == "501016.SH"
    assert nav.nav_date.isoformat() == "2026-06-12"
    assert nav.unit_nav == Decimal("1.149500")
    assert pro.calls == ["501016.OF", "501016.SH"]


def test_infer_units_and_estimate_value_from_anchor_nav() -> None:
    units = infer_units(
        market_value=Decimal("44683.70"),
        quantity=Decimal("0"),
        price=Decimal("0"),
        fallback_nav=Decimal("1.367900"),
    )

    assert units == Decimal("32665.911251")
    assert estimate_value(units, Decimal("1.305000")) == Decimal("42629.01")


def test_deviation_status_requires_pct_and_amount_threshold() -> None:
    assert (
        deviation_status(
            Decimal("150"),
            Decimal("10000"),
            pct_threshold=Decimal("0.01"),
            amount_threshold=Decimal("200"),
        )
        == "ok"
    )
    assert (
        deviation_status(
            Decimal("250"),
            Decimal("10000"),
            pct_threshold=Decimal("0.01"),
            amount_threshold=Decimal("200"),
        )
        == "needs_review"
    )


def test_cash_like_fund_holdings_are_excluded_from_nav_estimates() -> None:
    assert is_cash_like_fund_holding({"symbol": "020509", "name": "sample"})
    assert is_cash_like_fund_holding({"symbol": "000001", "name": "\u67d0\u67d0\u8d27\u5e01E"})
    assert not is_cash_like_fund_holding({"symbol": "011613", "name": "equity feeder"})


def test_load_latest_active_fund_holdings_ranks_each_fund_independently() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.query = ""
            self.rows = [
                {
                    "account_code": "ccb_yhs300_fund",
                    "symbol": "016440",
                    "name": "华夏中证红利质量ETF发起式联接A",
                    "market_value": Decimal("5043.55"),
                },
                {
                    "account_code": "ccb_yhs300_fund",
                    "symbol": "110020",
                    "name": "易沪深300",
                    "market_value": Decimal("23495.03"),
                    "asset_id": "ETF_110020",
                },
                {
                    "account_code": "tencent_licaitong",
                    "symbol": "020509",
                    "name": "嘉实活期宝货币E",
                    "market_value": Decimal("0.56"),
                },
            ]

        def execute(self, query: str) -> None:
            self.query = query

        def fetchall(self) -> list[dict]:
            return self.rows

    cur = FakeCursor()

    holdings = load_latest_active_fund_holdings(cur)

    assert "latest_fund_snapshot" in cur.query
    assert "INTERVAL '14 days'" in cur.query
    assert "PARTITION BY h.account_code, h.symbol, h.name" in cur.query
    assert "pams_asset_master" in cur.query
    assert "COALESCE(h.asset_id, matched_asset.asset_id) AS asset_id" in cur.query
    assert {row["symbol"] for row in holdings} == {"016440", "110020"}
    assert next(row for row in holdings if row["symbol"] == "110020")["asset_id"] == "ETF_110020"


def test_intraday_estimates_are_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("PAMS_FUND_NAV_USE_INTRADAY_ESTIMATE", raising=False)
    assert intraday_estimates_enabled() is False

    monkeypatch.setenv("PAMS_FUND_NAV_USE_INTRADAY_ESTIMATE", "true")
    assert intraday_estimates_enabled() is True


def test_late_confirmed_nav_advances_snapshot_on_the_nav_date(monkeypatch):
    from contextlib import nullcontext
    from datetime import date
    from types import SimpleNamespace
    from domain.market import fund_nav_estimates as nav

    prior, confirmed, today = (date(2026, 11, day) for day in (12, 13, 14))
    holding = {"symbol": "123456", "name": "Example", "account_code": "example",
               "as_of": prior, "market_value": Decimal("100"), "quantity": Decimal("100"),
               "current_price": Decimal("1")}
    writes = []
    conn = SimpleNamespace(cursor=lambda **kwargs: nullcontext(object()), commit=lambda: None)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr("domain.imports.isolation.require_external_access", lambda: None)
    monkeypatch.setattr(nav, "current_china_fund_date", lambda: today)
    monkeypatch.setattr(nav, "ensure_fund_nav_estimate_table", lambda cur: None)
    monkeypatch.setattr(nav, "load_latest_active_fund_holdings", lambda cur: [holding])
    monkeypatch.setattr(nav, "_stored_nav_prices", lambda *args: {})
    monkeypatch.setattr(nav, "lock_holding_accounts", lambda *args: None)
    monkeypatch.setattr(nav, "fetch_eastmoney_nav_history", lambda *args, **kwargs: [nav.ConfirmedNav(
        nav_date=confirmed, unit_nav=Decimal("1.20"), accumulated_nav=None,
        growth_pct=None, source="synthetic")])
    monkeypatch.setattr(nav, "existing_estimate", lambda *args, **kwargs: None)
    monkeypatch.setattr(nav, "upsert_estimate_row", lambda *args, **kwargs: None)
    monkeypatch.setattr(nav, "upsert_asset_market_daily_nav", lambda *args, **kwargs: None)
    monkeypatch.setattr(nav, "insert_or_update_generated_snapshot",
                        lambda cur, **kwargs: writes.append(kwargs) or True)

    nav.refresh_fund_nav_estimates(conn, use_intraday_estimates=False)

    assert [(row["as_of"], row["market_value"], row["update_existing"]) for row in writes] == [
        (confirmed, Decimal("120.00"), False),
    ]


def test_fund_history_checks_eastmoney_when_tushare_lags(monkeypatch):
    from datetime import date
    from domain.market import fund_nav_estimates as nav

    day1, gap, day2, day3 = (date(2026, 11, d) for d in (10, 11, 12, 13))
    tushare = [nav.ConfirmedNav(day1, Decimal("1"), None, None, "tushare_fund_nav"),
               nav.ConfirmedNav(day2, Decimal("1.2"), None, None, "tushare_fund_nav")]
    eastmoney = [nav.ConfirmedNav(gap, Decimal("1.1"), None, None),
                 nav.ConfirmedNav(day2, Decimal("99"), None, None),
                 nav.ConfirmedNav(day3, Decimal("1.3"), None, None)]
    monkeypatch.setattr(nav, "fetch_tushare_nav_history", lambda *args, **kwargs: tushare)
    monkeypatch.setattr(nav, "fetch_eastmoney_nav_history", lambda *args, **kwargs: eastmoney)
    rows, errors = nav.fetch_confirmed_nav_history("123456", session=None, pro=object(),
        start=day1, end=day3, missing_days={gap, day3})
    assert not errors
    assert rows == [tushare[0], eastmoney[0], tushare[1], eastmoney[2]]


def test_tushare_never_uses_announcement_date_as_nav_date():
    frame = pd.DataFrame([{"ts_code": "123456.OF", "ann_date": "20261113", "unit_nav": "1.34"}])
    assert parse_tushare_fund_nav_frame(frame) is None


def test_eastmoney_history_uses_published_unit_nav_and_its_own_date(monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from domain.market import fund_nav_estimates as nav

    monkeypatch.setattr("domain.imports.isolation.require_external_access", lambda: None)
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        "ErrCode": 0, "Data": {"LSJZList": [
            {"FSRQ": "2026-11-13", "DWJZ": "1.1701", "LJJZ": "1.3000", "JZZZL": "-1.20"},
            {"FSRQ": "2026-11-12", "DWJZ": "", "LJJZ": "1.3000"},
            {"FSRQ": "2026-11-16", "DWJZ": "2.0000"},
        ]},
    })
    start, end = date(2026, 11, 11), date(2026, 11, 15)
    rows = nav.fetch_eastmoney_nav_history(
        "024239", session=SimpleNamespace(get=lambda *args, **kwargs: response), start=start, end=end,
    )
    assert len(rows) == 1
    assert (rows[0].nav_date, rows[0].unit_nav, rows[0].source) == (
        date(2026, 11, 13), Decimal("1.1701"), "eastmoney_f10",
    )
