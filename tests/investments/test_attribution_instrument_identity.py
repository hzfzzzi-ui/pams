"""Keep same-name securities separate without changing attribution totals."""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))
from domain.investments.return_attribution import (  # noqa: E402
    _aggregate_dimension,
    _attribution_instrument_key,
    _underlying_rows,
    build_return_attribution_from_positions,
)


def unit(symbol, market, pnl, *, name="同名证券", account="one", source_symbol="fund"):
    return {
        "name": name,
        "symbol": symbol,
        "market": market,
        "currency": "CNY" if market == "A股" else "HKD",
        "source": "fund_lookthrough",
        "source_symbol": source_symbol,
        "source_asset_name": "合成基金",
        "underlying_name": name,
        "underlying_symbol": symbol,
        "account": account,
        "pnl_cny": pnl,
        "daily_pnl_cny": pnl / 2,
        "daily_price_pnl_cny": pnl / 4,
        "daily_fx_pnl_cny": pnl / 4,
        "market_value_cny": abs(pnl) * 10,
    }


def rows(units):
    return _underlying_rows(units, total_pnl=35, total_daily_pnl=17.5, total_market_value=350)


def test_same_name_separates_markets_and_same_security_combines_in_any_order():
    source = [unit("688981", "A股", 20), unit("688981", "A股", 5, account="two", source_symbol="other_fund"),
              unit("00981.HK", "港股", 10)]
    for ordered in itertools.permutations(source):
        output = rows(list(ordered))
        by_symbol = {row["symbol"]: row for row in output}
        assert set(by_symbol) == {"688981", "00981.HK"}
        assert by_symbol["688981"]["market"] == "A股"
        assert by_symbol["00981.HK"]["market"] == "港股"
        assert by_symbol["688981"]["pnl_cny"] == pytest.approx(25)
        assert by_symbol["00981.HK"]["pnl_cny"] == pytest.approx(10)
        assert len(by_symbol["688981"]["source_details"]) == 2
        assert by_symbol["688981"]["name"] == by_symbol["00981.HK"]["name"] == "同名证券"
        assert by_symbol["688981"]["display_name"] == "同名证券 (A股 688981)"
        assert by_symbol["00981.HK"]["display_name"] == "同名证券 (港股 00981.HK)"
        for field, expected in (("pnl_cny", 35), ("daily_pnl_cny", 17.5),
                                ("daily_price_pnl_cny", 8.75), ("daily_fx_pnl_cny", 8.75),
                                ("market_value_cny", 350)):
            assert sum(row[field] for row in output) == pytest.approx(expected)


def test_same_symbol_across_different_markets_is_not_merged():
    output = rows([unit("123456", "A股", 20), unit("123456", "港股", 10)])
    assert len(output) == 2
    assert {row["market"] for row in output} == {"A股", "港股"}
    assert {row["display_name"] for row in output} == {"同名证券 (A股 123456)", "同名证券 (港股 123456)"}


def test_same_market_and_symbol_combine_even_when_source_names_differ():
    entries = [unit("688981", "A股", 20, name="名称一"), unit("688981", "A股", 5, name="名称二")]
    for ordered in (entries, list(reversed(entries))):
        output = rows(ordered)
        assert len(output) == 1
        assert output[0]["symbol"] == "688981"
        assert output[0]["name"] == "名称一"
        assert output[0]["pnl_cny"] == pytest.approx(25)
        assert len(output[0]["source_details"]) == 2


@pytest.mark.parametrize("market,symbols", [("港股", ("00981.HK", "981")),
                                            ("A股", ("688981.SH", "sh688981"))])
def test_equivalent_market_qualified_formats_combine_stably(market, symbols):
    entries = [unit(symbols[0], market, 20), unit(symbols[1], market, 5)]
    outputs = [rows(list(order)) for order in itertools.permutations(entries)]
    assert all(len(output) == 1 and output[0]["pnl_cny"] == pytest.approx(25) for output in outputs)
    assert outputs[0][0]["symbol"] == outputs[1][0]["symbol"]


def test_unqualified_exchange_or_unknown_market_does_not_guess_identity():
    output = rows([unit("688981", "A股", 20), unit("688981.SH", "A股", 5),
                   unit("EXAMPLE", "其他", 3), unit("EXAMPLE", "其他", 2)])
    assert len(output) == 4
    assert sum(row["pnl_cny"] for row in output) == pytest.approx(30)


def test_conflicting_exchange_suffixes_remain_distinct():
    output = rows([unit("688981.SH", "A股", 20), unit("688981.SZ", "A股", 10)])
    assert len(output) == 2
    assert {row["symbol"] for row in output} == {"688981.SH", "688981.SZ"}


def test_missing_symbols_do_not_infer_identity_from_name():
    output = rows([unit("", "A股", 20), unit("", "A股", 10)])
    assert len(output) == 2
    assert sum(row["pnl_cny"] for row in output) == pytest.approx(30)


def test_unidentified_residuals_remain_a_shared_non_security_bucket():
    output = rows([unit("", "基金", 20, name="未穿透残差"), unit("", "基金", 10, name="未穿透残差")])
    assert len(output) == 1
    assert output[0]["pnl_cny"] == pytest.approx(30)


def test_grouping_conserves_amount_before_existing_200_row_limit():
    entries = [unit(str(600000 + i), "A股", 1, name=f"合成证券{i}") for i in range(201)]
    grouped = _aggregate_dimension(entries, lambda item: item["name"], total_pnl=201,
                                   total_daily_pnl=100.5, total_market_value=2010,
                                   group_key_fn=_attribution_instrument_key)
    assert len(grouped) == 201
    assert sum(row["pnl_cny"] for row in grouped) == pytest.approx(201)
    assert len(rows(entries)) == 200  # Existing response cap; omitted rows are not a financial total.


def test_fund_lookthrough_keeps_same_name_ah_rows_and_total_pnl():
    mappings = [
        {"asset_name": name, "symbol": symbol, "asset_class": "fund", "currency": "CNY",
         "region": "China", "risk_bucket": "CN_EQUITY", "mapping_confidence": "HIGH"}
        for name, symbol in (("基金一", "FUND_A"), ("基金二", "FUND_B"))
    ]
    a = {"underlying_name": "同名证券", "underlying_symbol": "688981", "underlying_region": "China",
         "underlying_currency": "CNY", "underlying_weight": 0.5}
    h = {"underlying_name": "同名证券", "underlying_symbol": "00981.HK", "underlying_region": "Hong Kong",
         "underlying_currency": "HKD", "underlying_weight": 0.5}
    config = {"asset_mapping": mappings, "asset_lookthrough": [
        {"parent_symbol": "FUND_A", "parent_asset_name": "基金一", "lookthrough_type": "FULL_HOLDINGS",
         "source_type": "FUND_REPORT", "confidence": "HIGH", "as_of": "2026-10-30", "holdings": [a, h]},
        {"parent_symbol": "FUND_B", "parent_asset_name": "基金二", "lookthrough_type": "FULL_HOLDINGS",
         "source_type": "FUND_REPORT", "confidence": "HIGH", "as_of": "2026-10-30",
         "holdings": [{**a, "underlying_weight": 1.0}]},
    ]}
    positions = [
        {"asset_name": "基金一", "symbol": "FUND_A", "account_code": "one", "asset_type": "fund",
         "currency": "CNY", "market_value_cny": 100, "holding_pnl_cny": 10},
        {"asset_name": "基金二", "symbol": "FUND_B", "account_code": "two", "asset_type": "fund",
         "currency": "CNY", "market_value_cny": 200, "holding_pnl_cny": 20},
    ]
    for ordered in (positions, list(reversed(positions))):
        payload = build_return_attribution_from_positions(ordered, config, as_of="2026-10-30")
        by_symbol = {row["symbol"]: row for row in payload["dimensions"]["underlying"]}
        assert set(by_symbol) == {"688981", "00981.HK"}
        assert by_symbol["688981"]["pnl_cny"] == pytest.approx(25)
        assert by_symbol["00981.HK"]["pnl_cny"] == pytest.approx(5)
        assert len(by_symbol["688981"]["source_details"]) == 2
        assert sum(row["pnl_cny"] for row in by_symbol.values()) == pytest.approx(payload["total_pnl_cny"])
