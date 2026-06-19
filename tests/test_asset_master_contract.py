from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from services.asset_master import (  # noqa: E402
    ASSET_MASTER_FIELD_PRIORITY,
    SCHEMA_SQL,
    VALID_CURRENCIES,
    VALID_MARKETS,
    VALID_REGIONS,
    _apply_asset_to_raw,
    _asset_record_from_raw,
    _find_asset,
    asset_id_for,
    build_asset_master_missing_mapping_report,
    build_asset_master_payload,
    build_lookthrough_seed_units,
    sync_asset_master_from_lookthrough_config,
    validate_asset_master_whitelists,
)


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql)


def test_asset_master_schema_declares_core_tables() -> None:
    for table_name in [
        "pams_asset_master",
        "pams_asset_alias",
        "pams_asset_industry_map",
        "pams_asset_theme_map",
        "pams_fund_underlying_position",
        "pams_asset_fixed_income_profile",
        "pams_asset_real_asset_profile",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in SCHEMA_SQL

    for column_name in [
        "asset_subtype",
        "economic_region",
        "industry_sub_bucket",
        "parent_theme_id",
        "underlying_weight",
        "credit_risk_bucket",
        "rental_income_annual",
    ]:
        assert column_name in SCHEMA_SQL


def test_holding_asset_identity_migration_backfills_asset_id_contract() -> None:
    sql = Path("database/postgres/schema/029_holding_asset_identity_backfill.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS asset_id VARCHAR" in sql
    assert "fk_pams_holdings_snapshot_asset_id" in sql
    assert "idx_pams_holdings_snapshot_asset_id" in sql
    assert "ASSET_020640" in sql
    assert "ASSET_WECHAT_BALANCE" in sql
    assert "ASSET_CREDIT_CARD_CNY" in sql
    assert "A_110020" in sql
    assert "ETF_110020" in sql
    assert "ETF_NFLX" in sql
    assert "ASSET_NFLX" in sql


def test_benchmark_asset_migration_seeds_gold_and_sse_comparators() -> None:
    sql = Path("database/postgres/schema/033_benchmark_assets_and_gold_proxy.sql").read_text(
        encoding="utf-8"
    )

    assert "BENCH_GOLD_ETF_518880" in sql
    assert "BENCH_SSE_COMPOSITE" in sql
    assert "benchmark_only" in sql
    assert "pams_asset_market_daily" in sql
    assert "pit_index_daily:000001.SH" in sql
    assert "CCB_GOLD_ACCUM" in sql


def test_gold_spot_benchmark_migration_seeds_non_holding_price_asset() -> None:
    sql = Path("database/postgres/schema/034_gold_spot_benchmark.sql").read_text(
        encoding="utf-8"
    )

    assert "BENCH_GOLD_SPOT_AU9999" in sql
    assert "Au99.99.SGE" in sql
    assert "benchmark_only" in sql
    assert "is_holding" in sql
    assert "akshare_sge_daily:Au99.99" in sql


def test_europe_sweden_mapping_migration_seeds_sive_and_sek_cash() -> None:
    sql = Path("database/postgres/schema/049_europe_sweden_asset_mapping.sql").read_text(
        encoding="utf-8"
    )

    assert "'ASSET_SIVE'" in sql
    assert "'欧股个股'" in sql
    assert "'欧股'" in sql
    assert "'瑞典'" in sql
    assert "'SEK'" in sql
    assert "'ASSET_CASH_SEK'" in sql
    assert "UPPER(COALESCE(symbol, '')) = 'SIVE'" in sql
    assert "UPPER(COALESCE(symbol, '')) = 'CASH_SEK'" in sql


def test_asset_master_whitelist_allows_europe_sweden_and_sek() -> None:
    assert "欧股" in VALID_MARKETS
    assert {"欧洲", "瑞典"}.issubset(VALID_REGIONS)
    assert "SEK" in VALID_CURRENCIES


def test_korea_asset_normalization_migration_updates_korean_equities() -> None:
    sql = Path("database/postgres/schema/050_korea_overseas_asset_normalization.sql").read_text(
        encoding="utf-8"
    )

    assert "'ASSET_005930_KS'" in sql
    assert "'ASSET_000660_KS'" in sql
    assert "'韩股个股'" in sql
    assert "'韩股'" in sql
    assert "'韩国'" in sql
    assert "'KRW'" in sql
    assert "'海外个股'" not in sql


def test_asset_master_normalizes_english_regions_to_whitelist() -> None:
    korea = _asset_record_from_raw(
        {
            "symbol": "005930.KS",
            "asset_name": "Samsung Electronics",
            "asset_type": "stock_kr",
            "currency": "KRW",
            "region": "Korea",
        },
        source="test",
    )
    sweden = _asset_record_from_raw(
        {
            "symbol": "SIVE",
            "asset_name": "Sivers Semiconductors",
            "asset_type": "stock_eu",
            "currency": "SEK",
            "region": "Sweden",
        },
        source="test",
    )

    assert korea["asset_type"] == "韩股个股"
    assert korea["market"] == "韩股"
    assert korea["economic_region"] == "韩国"
    assert korea["listing_region"] == "韩国"
    assert sweden["economic_region"] == "瑞典"
    assert sweden["listing_region"] == "瑞典"


def test_asset_master_id_canonicalizes_known_aliases() -> None:
    assert asset_id_for("GOOG", "Alphabet", "stock_us") == asset_id_for("GOOGL", "Alphabet", "stock_us")
    assert asset_id_for("03690.HK", "Meituan", "stock_hk") == asset_id_for("3690", "Meituan", "stock_hk")


def test_asset_master_unknown_currency_does_not_override_position_currency() -> None:
    record = _asset_record_from_raw(
        {"symbol": "NVDA", "asset_name": "NVIDIA", "asset_type": "stock_us", "currency": "other"},
        source="test",
    )
    assert record["currency"] == ""

    enriched = _apply_asset_to_raw(
        {"symbol": "NVDA", "asset_name": "NVIDIA", "currency": "USD"},
        {"asset_id": "ASSET_NVDA", "asset_type": "stock_us", "currency": "\u5176\u4ed6"},
    )
    assert enriched["currency"] == "USD"

    enriched = _apply_asset_to_raw(
        {"symbol": "NVDA", "asset_name": "NVIDIA", "currency": "USD"},
        {"asset_id": "ASSET_NVDA", "asset_type": "stock_us", "currency": "CNY"},
    )
    assert enriched["currency"] == "CNY"


def test_asset_master_lookup_prefers_asset_type_specific_id() -> None:
    lookup = {
        "assets": {
            "A_110020": {"asset_id": "A_110020", "asset_type": "A-stock"},
            "ETF_110020": {"asset_id": "ETF_110020", "asset_type": "fund"},
        },
        "alias_to_id": {"110020": "A_110020"},
    }

    found = _find_asset({"symbol": "110020", "asset_name": "CSI 300 Fund", "asset_type": "fund_cn"}, lookup)
    assert found["asset_id"] == "ETF_110020"

    found = _find_asset(
        {"symbol": "110020", "asset_name": "CSI 300 Fund", "asset_type": "a_share_stock", "asset_class": "fund_cn"},
        lookup,
    )
    assert found["asset_id"] == "ETF_110020"

    found = _find_asset({"symbol": "110020", "asset_name": "Stock 110020", "asset_type": "stock_cn"}, lookup)
    assert found["asset_id"] == "A_110020"


def test_asset_master_payload_exposes_field_contract() -> None:
    asset_id = asset_id_for("GOOG", "Alphabet", "stock_us")
    lookup = {
        "assets": {
            asset_id: {
                "asset_id": asset_id,
                "display_name": "Alphabet",
                "canonical_name": "Alphabet",
                "primary_symbol": "GOOGL",
                "asset_type": "stock_us",
                "confidence": "medium",
                "industry": {"industry_bucket": "TMT"},
                "themes": [{"theme_level": "parent", "theme_id": "ai_compute"}],
            }
        },
        "alias_to_id": {"goog": asset_id, "googl": asset_id, asset_id.casefold(): asset_id},
    }

    payload = build_asset_master_payload(
        positions=[{"symbol": "GOOG", "asset_name": "Alphabet"}],
        units=[
            {
                "holding_symbol": "QQQ",
                "holding_name": "Invesco QQQ",
                "holding_asset_class": "fund_cn",
                "holding_market_value_cny": 1000,
                "lookthrough_type": "INDEX_PROXY",
                "underlying_symbol": "NVDA",
                "underlying_name": "NVIDIA",
                "underlying_weight": 0.08,
                "value_cny": 80,
                "underlying_asset_class": "stock_us",
                "industry_bucket": "TMT",
                "underlying_currency": "USD",
            }
        ],
        lookup=lookup,
    )

    assert payload["status"] == "ok"
    assert payload["current_assets"] == 1
    assert payload["current_theme_mapped"] == 1
    assert {item["table"] for item in payload["field_contract"]} == {
        "asset_master",
        "asset_alias",
        "asset_industry_map",
        "asset_theme_map",
        "fund_underlying_position",
    }
    assert payload["fund_underlying_top10"][0]["fund_symbol"] == "QQQ"
    assert payload["fund_underlying_top10"][0]["underlyings"][0]["symbol"] == "NVDA"


def test_asset_master_field_priority_declares_classification_source_order() -> None:
    assert ASSET_MASTER_FIELD_PRIORITY["source_precedence"][:2] == [
        "manual asset_master rows",
        "lookthrough.yaml asset_mapping seed",
    ]
    assert "industry_map" in ASSET_MASTER_FIELD_PRIORITY["classification"]
    assert "theme_map" in ASSET_MASTER_FIELD_PRIORITY["classification"]


def test_lookthrough_yaml_seed_units_include_residual_and_sync_entry() -> None:
    config = {
        "as_of": "2026-06-03",
        "asset_mapping": [
            {
                "asset_name": "Seed Fund",
                "symbol": "FUND1",
                "asset_class": "fund_cn",
                "region": "China",
                "currency": "CNY",
                "industry": "科技",
                "theme": "AI算力",
            }
        ],
        "asset_lookthrough": [
            {
                "parent_symbol": "FUND1",
                "parent_asset_name": "Seed Fund",
                "parent_asset_type": "fund_cn",
                "lookthrough_type": "TOP10",
                "holdings": [
                    {
                        "underlying_name": "NVIDIA",
                        "underlying_symbol": "NVDA",
                        "underlying_weight": 0.8,
                        "underlying_region": "United States",
                        "underlying_currency": "USD",
                        "underlying_industry": "Semiconductors",
                        "underlying_theme": "AI compute",
                    }
                ],
            }
        ],
    }

    units = build_lookthrough_seed_units(config)
    assert [unit["lookthrough_type"] for unit in units] == ["TOP10", "PRODUCT_TAG"]
    assert units[1]["underlying_name"] == "未穿透残差"

    cur = FakeCursor()
    result = sync_asset_master_from_lookthrough_config(cur, config=config, load_lookup=False)

    assert result["status"] == "ok"
    assert result["asset_mapping_rows"] == 1
    assert result["fund_underlying_rows"] == 2
    assert any("pams_fund_underlying_position" in sql for sql in cur.statements)


def test_asset_master_whitelist_validation_blocks_invalid_industry_and_theme() -> None:
    lookup = {
        "assets": {
            "BAD": {
                "asset_id": "BAD",
                "canonical_name": "Bad Asset",
                "primary_symbol": "BAD",
                "asset_type": "美股个股",
                "market": "美股",
                "economic_region": "美国",
                "currency": "USD",
                "industry": {"industry_bucket": "美元资产"},
                "themes": [{"theme_level": "parent", "theme_name": "美元资产"}],
            }
        },
        "alias_to_id": {"bad": "BAD"},
    }

    problems = validate_asset_master_whitelists(lookup)
    assert problems["invalid_industry"][0]["value"] == "美元资产"
    assert problems["invalid_theme"][0]["value"] == "美元资产"

    try:
        validate_asset_master_whitelists(lookup, strict=True)
        assert False, "expected invalid whitelist values to raise in strict mode"
    except ValueError as exc:
        assert "whitelist validation failed" in str(exc)


def test_missing_mapping_report_flags_unknown_asset_and_unlooked_fund() -> None:
    equity_id = asset_id_for("NVDA", "NVIDIA", "美股个股")
    fund_id = asset_id_for("FUND1", "Seed Fund", "ETF / 基金")
    lookup = {
        "assets": {
            equity_id: {
                "asset_id": equity_id,
                "canonical_name": "NVIDIA",
                "primary_symbol": "NVDA",
                "asset_type": "美股个股",
                "market": "美股",
                "economic_region": "美国",
                "currency": "USD",
                "industry": None,
                "themes": [],
            },
            fund_id: {
                "asset_id": fund_id,
                "canonical_name": "Seed Fund",
                "primary_symbol": "FUND1",
                "asset_type": "ETF / 基金",
                "market": "基金",
                "economic_region": "中国大陆",
                "currency": "CNY",
                "industry": {"industry_bucket": "未穿透 / 未映射"},
                "themes": [],
            },
        },
        "alias_to_id": {
            "nvda": equity_id,
            equity_id.casefold(): equity_id,
            "fund1": fund_id,
            fund_id.casefold(): fund_id,
        },
    }

    report = build_asset_master_missing_mapping_report(
        positions=[
            {"asset_name": "NVIDIA", "symbol": "NVDA", "asset_type": "stock_us", "currency": "USD"},
            {"asset_name": "UnknownCo", "symbol": "UNKNOWN", "asset_type": "stock_us", "currency": "USD"},
            {"asset_name": "Seed Fund", "symbol": "FUND1", "asset_type": "fund_cn", "currency": "CNY"},
        ],
        units=[],
        lookup=lookup,
    )

    assert report["summary"]["missing_asset_master"] == 1
    assert report["missing_asset_master"][0]["symbol"] == "UNKNOWN"
    assert report["summary"]["missing_industry"] == 1
    assert report["missing_industry"][0]["symbol"] == "NVDA"
    assert report["summary"]["missing_theme"] >= 1
    assert report["summary"]["missing_fund_underlying"] == 1
    assert report["missing_fund_underlying"][0]["symbol"] == "FUND1"
