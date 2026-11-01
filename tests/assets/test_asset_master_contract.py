from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "backend" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from domain.assets.asset_master import (  # noqa: E402
    ASSET_MASTER_FIELD_PRIORITY,
    ASSET_MASTER_REQUIRED_COLUMNS,
    ASSET_MASTER_REQUIRED_UNIQUE_KEYS,
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
    ensure_asset_master_schema,
    load_asset_master_lookup,
    sync_asset_master_from_lookthrough_config,
    validate_asset_master_whitelists,
)


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql)
        self.params = params

    def fetchall(self) -> list[dict]:
        if self.statements and "pg_attribute" in self.statements[-1]:
            return [{"attname": name} for name in ASSET_MASTER_REQUIRED_COLUMNS[self.params[0]]]
        return []

    def fetchone(self) -> dict:
        return {"has_unique": True}


class ReadOnlyLookupCursor(FakeCursor):
    def __init__(self) -> None:
        super().__init__()
        self._results: list[list[dict]] = [[], [], [], []]

    def fetchall(self) -> list[dict]:
        return self._results.pop(0) if self._results else []


def test_asset_master_schema_declares_core_tables() -> None:
    schema_sql = (ROOT / "database/postgres/schema/023_asset_master.sql").read_text(encoding="utf-8")
    for table_name in [
        "pams_asset_master",
        "pams_asset_alias",
        "asset_classifications",
        "pams_fund_underlying_position",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in schema_sql

    for column_name in [
        "asset_subtype",
        "economic_region",
        "industry_sub_bucket",
        "parent_theme_id",
        "underlying_weight",
    ]:
        assert column_name in schema_sql


def test_asset_schema_validation_only_reads_metadata() -> None:
    cur = FakeCursor()
    ensure_asset_master_schema(cur)
    assert len(cur.statements) == len(ASSET_MASTER_REQUIRED_COLUMNS) + len(ASSET_MASTER_REQUIRED_UNIQUE_KEYS)
    assert all(statement.lstrip().upper().startswith("SELECT ") for statement in cur.statements)


def test_asset_master_lookup_can_skip_schema_writes_for_read_paths() -> None:
    cur = ReadOnlyLookupCursor()

    lookup = load_asset_master_lookup(cur, ensure_schema=False)

    assert lookup == {"assets": {}, "alias_to_id": {}}
    joined = "\n".join(cur.statements).upper()
    assert "CREATE TABLE" not in joined
    assert "INSERT " not in joined
    assert "UPDATE " not in joined
    assert "SELECT * FROM PAMS_ASSET_MASTER" in joined


def test_gold_spot_benchmark_migration_seeds_non_holding_price_asset() -> None:
    sql = Path("database/postgres/schema/034_gold_spot_benchmark.sql").read_text(
        encoding="utf-8"
    )

    assert "BENCH_GOLD_SPOT_AU9999" in sql
    assert "Au99.99.SGE" in sql
    assert "benchmark_only" in sql
    assert "is_holding" in sql
    assert "akshare_sge_daily:Au99.99" in sql


def test_asset_master_whitelist_allows_europe_sweden_and_sek() -> None:
    assert "欧股" in VALID_MARKETS
    assert {"欧洲", "瑞典"}.issubset(VALID_REGIONS)
    assert "SEK" in VALID_CURRENCIES


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
    assert korea["currency"] == "KRW"
    assert sweden["asset_type"] == "欧股个股"
    assert sweden["market"] == "欧股"
    assert sweden["economic_region"] == "瑞典"
    assert sweden["listing_region"] == "瑞典"
    assert sweden["currency"] == "SEK"
    sk_hynix = _asset_record_from_raw(
        {"symbol": "000660.KS", "asset_name": "SK Hynix", "asset_type": "stock_kr",
         "currency": "KRW", "region": "Korea"}, source="test",
    )
    assert (sk_hynix["asset_type"], sk_hynix["market"], sk_hynix["economic_region"], sk_hynix["currency"]) == (
        "韩股个股", "韩股", "韩国", "KRW",
    )


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


def test_asset_schema_unavailable_returns_sanitized_503(monkeypatch) -> None:
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from endpoints import analysis
    from domain.assets.asset_master import AssetMasterSchemaUnavailable

    connection = MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(analysis, "get_conn", lambda: connection)
    def fail(*args, **kwargs):
        raise AssetMasterSchemaUnavailable("sensitive schema pams_asset_master")
    monkeypatch.setattr(analysis, "build_lookthrough_payload", fail)
    with pytest.raises(HTTPException) as exc:
        analysis.sync_asset_master_from_current_portfolio("CNY")
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "asset_schema_unavailable", "message": "资产主数据结构不可用，同步未执行"}
    assert "pams_asset_master" not in str(exc.value.detail)
    assert connection.__exit__.call_args.args[0] is AssetMasterSchemaUnavailable
    connection.commit.assert_not_called()


def test_market_qualified_collision_does_not_match_existing_other_exchange() -> None:
    lookup = {
        "assets": {"A_600000": {"asset_id": "A_600000", "primary_symbol": "600000.SZ", "asset_type": "A股个股"}},
        "alias_to_id": {},
    }
    assert _find_asset({"symbol": "600000.SH", "asset_type": "stock_cn"}, lookup) is None
    assert _find_asset({"symbol": "600000", "asset_type": "stock_cn"}, lookup) is None
    assert _find_asset({"asset_master_id": "A_600000", "symbol": "600000.SH"}, lookup) is None
    assert _find_asset({"symbol": "600000.SZ", "asset_type": "stock_cn"}, lookup)["asset_id"] == "A_600000"


def test_asset_master_sync_conflict_returns_sanitized_409(monkeypatch) -> None:
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from endpoints import analysis
    from domain.assets.asset_master import AmbiguousAssetIdentityError

    connection = MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(analysis, "get_conn", lambda: connection)
    def fail(*args, **kwargs):
        raise AmbiguousAssetIdentityError("sensitive asset_id 600000.SH")
    monkeypatch.setattr(analysis, "build_lookthrough_payload", fail)
    with pytest.raises(HTTPException) as exc:
        analysis.sync_asset_master_from_current_portfolio("CNY")
    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "ambiguous_asset_identity", "message": "资产身份存在歧义，同步未执行"}
    assert "600000" not in str(exc.value.detail)
    assert connection.__exit__.call_args.args[0] is AmbiguousAssetIdentityError
    connection.commit.assert_not_called()


def test_conflicting_equal_priority_aliases_are_not_assigned() -> None:
    cur = ReadOnlyLookupCursor()
    cur._results = [
        [{"asset_id": "A"}, {"asset_id": "B"}],
        [{"asset_id": "A", "alias_type": "name", "alias_value": "Shared", "source": "test", "priority": 1},
         {"asset_id": "B", "alias_type": "name", "alias_value": "Shared", "source": "test", "priority": 1}],
        [], [],
    ]
    lookup = load_asset_master_lookup(cur, ensure_schema=False)
    assert "shared" not in lookup["alias_to_id"]
    assert _find_asset({"asset_name": "Shared"}, lookup) is None
    assert _find_asset({"asset_master_id": "A", "asset_name": "Shared"}, lookup)["asset_id"] == "A"


def test_conflicting_distinct_aliases_do_not_depend_on_set_order() -> None:
    lookup = {"assets": {"A": {"asset_id": "A"}, "B": {"asset_id": "B"}},
              "alias_to_id": {"symbol-a": "A", "shared": "B"}}
    assert _find_asset({"symbol": "SYMBOL-A", "asset_name": "Shared"}, lookup) is None


def test_unique_exact_symbol_takes_precedence_over_ambiguous_name() -> None:
    lookup = {"assets": {
                  "HK": {"asset_id": "HK", "primary_symbol": "00001.HK", "asset_type": "港股个股"},
                  "CN": {"asset_id": "CN", "primary_symbol": "600001.SH", "asset_type": "A股个股"}},
              "alias_to_id": {"00001.hk": "HK"}, "ambiguous_aliases": {"shared"}}
    assert _find_asset({"symbol": "00001.HK", "asset_name": "Shared"}, lookup)["asset_id"] == "HK"
    assert _find_asset({"symbol": "UNKNOWN", "asset_name": "Shared"}, lookup) is None


def test_duplicate_exact_symbols_remain_unresolved() -> None:
    lookup = {"assets": {"A": {"asset_id": "A", "primary_symbol": "DUP"},
                         "B": {"asset_id": "B", "primary_symbol": "DUP"}},
              "alias_to_id": {}}
    assert _find_asset({"symbol": "DUP"}, lookup) is None
