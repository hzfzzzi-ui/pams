"""Asset master data service for portfolio exposure analysis.

The exposure engine remains pure and rule-backed.  This module adds a database
layer in front of it: current holdings and fund underlyings are synced into a
canonical asset master, then raw analysis rows are enriched from that master
before exposure calculations run.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date as Date
from decimal import Decimal
from typing import Any

import psycopg2.extras

from domain.assets.exposure_engine import (
    AI_SUB_THEME_DEFINITIONS,
    ASSET_TYPES,
    FORBIDDEN_INDUSTRY_LABELS,
    FORBIDDEN_THEME_LABELS,
    INDUSTRY_BUCKETS,
    THEME_DEFINITIONS,
    UNMAPPED_INDUSTRY,
    UNMAPPED_THEME_NAME,
    _canonical_symbol,
    _currency,
    _identity_keys,
    _num,
    _role,
    _text,
    _ai_sub_theme_rules,
    resolveAssetType,
    resolveIndustryBucket,
    resolveThemeExposures,
    resolve_market,
    resolve_region,
)


EQUITY_TYPES = {"A股个股", "港股个股", "美股个股", "欧股个股", "韩股个股"}
NON_THEME_TYPES = {"现金", "债券 / 货币基金", "非上市股权", "房地产 / 实物资产", "应收款 / 待到账"}
VALID_CURRENCIES = {"CNY", "HKD", "USD", "SEK", "KRW", "MXN"}
VALID_MARKETS = {"A股", "港股", "美股", "欧股", "韩股", "基金", "现金", "货币基金 / 债券", "黄金 / 商品", "非上市 / 内部", "其他"}
VALID_REGIONS = {"中国大陆", "香港", "美国", "欧洲", "瑞典", "韩国", "全球", "其他"}
VALID_THEME_NAMES = {name for name, _family in THEME_DEFINITIONS.values()}
VALID_AI_SUB_THEME_NAMES = set(AI_SUB_THEME_DEFINITIONS.values())
REAL_LOOKTHROUGH_TYPES = {"FULL_HOLDINGS", "TOP10", "INDEX_PROXY", "MANUAL"}

ASSET_MASTER_FIELD_PRIORITY = {
    "identity": [
        "asset_id",
        "primary_symbol",
        "canonical_name",
        "display_name",
        "asset_alias",
    ],
    "classification": [
        "asset_type",
        "market",
        "economic_region",
        "currency",
        "industry_map",
        "theme_map",
    ],
    "source_precedence": [
        "manual asset_master rows",
        "lookthrough.yaml asset_mapping seed",
        "lookthrough.yaml fund underlying seed",
        "holdings/imported system rows",
        "exposure_engine rule fallback",
    ],
    "ownership": {
        "pams_asset_master": "identity, asset type, market, region, currency, valuation and liquidity",
        "pams_asset_industry_map": "industry only; never market, region, currency, or theme labels",
        "pams_asset_theme_map": "theme only; multi-label and may overlap",
        "pams_fund_underlying_position": "fund-to-underlying weights including residual rows",
    },
}


ASSET_MASTER_REQUIRED_COLUMNS = {
    "pams_asset_master": frozenset({
        "asset_id", "canonical_name", "display_name", "primary_symbol", "exchange", "isin", "cusip",
        "asset_type", "asset_subtype", "market", "listing_region", "economic_region", "currency",
        "valuation_method", "liquidity_profile", "income_type", "exposure_policy", "status", "source",
        "confidence", "raw_json", "last_verified_at", "created_at", "updated_at",
    }),
    "pams_asset_alias": frozenset({"asset_id", "alias_type", "alias_value", "source", "priority"}),
    "pams_asset_industry_map": frozenset({
        "asset_id", "industry_system", "industry_leaf", "industry_sub_bucket", "industry_bucket",
        "classification_scope", "source", "confidence", "evidence", "as_of", "is_primary", "updated_at",
    }),
    "pams_asset_theme_map": frozenset({
        "asset_id", "theme_id", "theme_name", "parent_theme_id", "theme_level", "family", "score", "role",
        "source", "confidence", "evidence", "as_of",
    }),
    "pams_fund_underlying_position": frozenset({
        "fund_asset_id", "underlying_asset_id", "underlying_name", "underlying_symbol", "underlying_weight",
        "underlying_asset_type", "as_of", "effective_source", "coverage_type", "confidence", "source", "raw_json",
    }),
}


ASSET_MASTER_REQUIRED_UNIQUE_KEYS = {
    "pams_asset_master": ("asset_id",),
    "pams_asset_alias": ("asset_id", "alias_type", "alias_value"),
    "pams_asset_industry_map": ("asset_id", "industry_system", "classification_scope", "is_primary"),
}


class AssetMasterSchemaUnavailable(RuntimeError):
    """Required asset schema must be provisioned independently of application requests."""


def ensure_asset_master_schema(cur) -> None:
    """Check required tables and columns without creating or changing database objects."""
    for table, required in ASSET_MASTER_REQUIRED_COLUMNS.items():
        cur.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attnum > 0 AND NOT attisdropped",
            (table,),
        )
        columns = {row["attname"] if isinstance(row, dict) else row[0] for row in cur.fetchall()}
        missing = required - columns
        if missing:
            raise AssetMasterSchemaUnavailable(
                f"asset schema {table} is missing or incomplete: {', '.join(sorted(missing))}"
            )
    for table, key_columns in ASSET_MASTER_REQUIRED_UNIQUE_KEYS.items():
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_index AS i
                WHERE i.indrelid = to_regclass(%s) AND i.indisunique AND i.indisvalid
                  AND i.indimmediate AND i.indpred IS NULL AND i.indexprs IS NULL
                  AND ARRAY(
                      SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                      JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                      WHERE k.ord <= i.indnkeyatts ORDER BY a.attname::text
                  ) = %s::text[]
            ) AS has_unique
            """,
            (table, sorted(key_columns)),
        )
        row = cur.fetchone()
        has_unique = row["has_unique"] if isinstance(row, dict) else (row[0] if row else False)
        if not has_unique:
            raise AssetMasterSchemaUnavailable(f"asset schema {table} is missing a required unique key")


def _norm_key(value: Any) -> str:
    return _text(value).casefold()


def _safe_id_part(value: Any) -> str:
    raw = _text(value).upper()
    raw = re.sub(r"[^A-Z0-9]+", "_", raw).strip("_")
    return raw[:90]


def asset_id_for(symbol: Any, name: Any, asset_type: Any = "") -> str:
    canonical = _canonical_symbol(symbol, name)
    base = _safe_id_part(canonical) or _safe_id_part(symbol)
    if not base:
        digest = hashlib.sha1(_text(name).encode("utf-8")).hexdigest()[:16]
        base = f"NAME_{digest.upper()}"
    type_part = _safe_id_part(asset_type)[:24] or "ASSET"
    return f"{type_part}_{base}"[:120]


def _valid_currency(value: Any) -> str:
    currency = _currency(value)
    return currency if currency in VALID_CURRENCIES else ""


def _raw_currency(raw: dict[str, Any]) -> str:
    for key in ("currency", "underlying_currency", "holding_currency", "trading_currency", "settlement_currency", "base_currency"):
        currency = _valid_currency(raw.get(key))
        if currency:
            return currency
    return ""


def _asset_subtype(raw: dict[str, Any], asset_type: str) -> str:
    text = " ".join(_text(raw.get(k)) for k in ("asset_type", "asset_class", "asset_name", "name", "underlying_name", "industry", "theme")).casefold()
    if asset_type == "ETF / 基金":
        if "货币" in text or "money" in text:
            return "货币基金"
        if "债" in text or "bond" in text or "treasury" in text:
            return "债券基金"
        if "黄金" in text or "gold" in text:
            return "商品基金"
        if "混合" in text:
            return "混合基金"
        return "股票ETF / 基金"
    if asset_type == "债券 / 货币基金":
        return "短久期 / 货币"
    if asset_type == "房地产 / 实物资产":
        return "房地产"
    if asset_type == "非上市股权":
        return "ESOP / 非上市股权"
    return ""


def _valuation_method(asset_type: str) -> str:
    return {
        "现金": "cash_balance",
        "债券 / 货币基金": "amortized_cost_or_nav",
        "ETF / 基金": "nav_or_market_price",
        "黄金 / 商品": "market_price",
        "房地产 / 实物资产": "appraisal",
        "非上市股权": "manual",
        "应收款 / 待到账": "receivable",
    }.get(asset_type, "market_price")


def _liquidity_profile(asset_type: str) -> str:
    return {
        "现金": "daily",
        "债券 / 货币基金": "daily",
        "ETF / 基金": "daily_or_t1",
        "非上市股权": "illiquid",
        "房地产 / 实物资产": "illiquid",
        "应收款 / 待到账": "pending",
    }.get(asset_type, "daily")


def _income_type(asset_type: str) -> str:
    return {
        "债券 / 货币基金": "interest",
        "非上市股权": "distribution",
        "房地产 / 实物资产": "rent",
        "现金": "interest",
    }.get(asset_type, "none")


def _exposure_policy(asset_type: str) -> str:
    return {
        "A股个股": "listed_equity",
        "港股个股": "listed_equity",
        "美股个股": "listed_equity",
        "欧股个股": "listed_equity",
        "韩股个股": "listed_equity",
        "ETF / 基金": "fund_lookthrough",
        "现金": "cash",
        "债券 / 货币基金": "fixed_income",
        "黄金 / 商品": "commodity",
        "非上市股权": "private_equity",
        "房地产 / 实物资产": "real_asset",
        "应收款 / 待到账": "receivable",
    }.get(asset_type, "other")


def _industry_sub_bucket(leaf: Any, bucket: Any, name: Any = "") -> str:
    key = " ".join([_text(leaf), _text(name)]).casefold()
    if bucket == "科技TMT":
        if any(x in key for x in ("semiconductor", "半导体", "电子", "芯片", "hbm", "存储")):
            return "半导体 / 硬件"
        if any(x in key for x in ("software", "cloud", "软件", "云")):
            return "云计算 / 软件"
        if any(x in key for x in ("internet", "互联网", "平台", "meituan", "美团")):
            return "互联网平台"
        if any(x in key for x in ("通信", "telecom", "光模块", "cpo")):
            return "通信设备 / 光通信"
        return "科技综合"
    if bucket == "周期资源":
        if any(x in key for x in ("gold", "黄金", "铜", "copper", "有色", "metal")):
            return "黄金 / 铜 / 有色"
        if any(x in key for x in ("oil", "gas", "煤", "能源", "石油")):
            return "能源 / 油气"
        if "化工" in key:
            return "化工"
        return "资源综合"
    if bucket == "高端制造":
        if any(x in key for x in ("汽车", "automobile", "新能源车")):
            return "汽车 / 新能源车"
        if any(x in key for x in ("电力设备", "电气", "储能")):
            return "电力设备 / 储能"
        if any(x in key for x in ("机械", "设备", "自动化")):
            return "机械 / 自动化"
        if any(x in key for x in ("军工", "aerospace", "defense")):
            return "军工 / 航空航天"
        return "高端制造综合"
    if bucket == "大消费":
        if any(x in key for x in ("食品", "饮料", "beverage", "food")):
            return "食品饮料"
        if any(x in key for x in ("retail", "零售", "互联网", "平台", "美团")):
            return "零售 / 本地生活"
        if any(x in key for x in ("农", "牧", "养殖")):
            return "农林牧渔"
        return "消费服务"
    if bucket == "金融地产":
        if any(x in key for x in ("银行", "bank")):
            return "银行"
        if any(x in key for x in ("证券", "券商", "broker")):
            return "证券 / 非银"
        if "房地产" in key:
            return "房地产"
        return "金融综合"
    if bucket == "稳定公用":
        if any(x in key for x in ("公用", "电力", "utility")):
            return "公用事业"
        if any(x in key for x in ("交通", "航运", "物流", "transport")):
            return "交通运输"
        return "稳定公用综合"
    if bucket == "医药医疗":
        return "医药生物 / 医疗服务"
    return ""


def _date_or_none(value: Any) -> Any:
    if isinstance(value, Date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return Date.fromisoformat(text[:10])
    except ValueError:
        return None


def _asset_record_from_raw(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    name = _text(raw.get("asset_name") or raw.get("name") or raw.get("underlying_name") or raw.get("symbol") or raw.get("underlying_symbol"))
    symbol = _text(raw.get("symbol") or raw.get("underlying_symbol") or raw.get("asset_id"))
    asset_type = resolveAssetType(raw)
    market = _text(raw.get("market")) or resolve_market(raw, asset_type)
    currency = _raw_currency(raw)
    region_raw = _text(raw.get("economic_region") or raw.get("region") or raw.get("underlying_region"))
    region = resolve_region({**raw, "region": region_raw, "currency": currency}, market)
    listing_region_raw = _text(raw.get("listing_region") or raw.get("region") or raw.get("underlying_region"))
    listing_region = resolve_region({**raw, "region": listing_region_raw, "currency": currency}, market) if listing_region_raw else region
    asset_id = asset_id_for(symbol, name, asset_type)
    confidence = _text(raw.get("mapping_confidence") or raw.get("confidence") or "medium").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    return {
        "asset_id": asset_id,
        "canonical_name": name or symbol or asset_id,
        "display_name": _text(raw.get("display_name") or name or symbol),
        "primary_symbol": symbol,
        "exchange": _text(raw.get("exchange")),
        "isin": _text(raw.get("isin")),
        "cusip": _text(raw.get("cusip")),
        "asset_type": asset_type,
        "asset_subtype": _text(raw.get("asset_subtype")) or _asset_subtype(raw, asset_type),
        "market": market,
        "listing_region": listing_region,
        "economic_region": region,
        "currency": currency,
        "valuation_method": _text(raw.get("valuation_method")) or _valuation_method(asset_type),
        "liquidity_profile": _text(raw.get("liquidity_profile")) or _liquidity_profile(asset_type),
        "income_type": _text(raw.get("income_type")) or _income_type(asset_type),
        "exposure_policy": _text(raw.get("exposure_policy")) or _exposure_policy(asset_type),
        "status": _text(raw.get("status")) or "active",
        "source": source,
        "confidence": confidence,
        "raw_json": _json_ready(dict(raw)),
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def _aliases_for_raw(raw: dict[str, Any], asset: dict[str, Any]) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for alias_type, value, priority in (
        ("symbol", asset.get("primary_symbol"), 10),
        ("canonical_symbol", _canonical_symbol(asset.get("primary_symbol"), asset.get("canonical_name")), 15),
        ("name", asset.get("canonical_name"), 20),
        ("display_name", asset.get("display_name"), 25),
        ("asset_id", raw.get("asset_id"), 30),
    ):
        text = _text(value)
        if text:
            out.append((alias_type, text, priority))
    for value in raw.get("aliases") or []:
        text = _text(value)
        if text:
            out.append(("alias", text, 40))
    return list(dict.fromkeys(out))


def _upsert_asset(cur, raw: dict[str, Any], *, source: str = "system") -> str:
    asset = _asset_record_from_raw(raw, source=source)
    cur.execute(
        """
        INSERT INTO pams_asset_master (
            asset_id, canonical_name, display_name, primary_symbol, exchange, isin, cusip,
            asset_type, asset_subtype, market, listing_region, economic_region, currency,
            valuation_method, liquidity_profile, income_type, exposure_policy, status,
            source, confidence, raw_json, last_verified_at
        )
        VALUES (
            %(asset_id)s, %(canonical_name)s, %(display_name)s, %(primary_symbol)s, %(exchange)s, %(isin)s, %(cusip)s,
            %(asset_type)s, %(asset_subtype)s, %(market)s, %(listing_region)s, %(economic_region)s, %(currency)s,
            %(valuation_method)s, %(liquidity_profile)s, %(income_type)s, %(exposure_policy)s, %(status)s,
            %(source)s, %(confidence)s, %(raw_json)s, NOW()
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            canonical_name = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.canonical_name
                ELSE COALESCE(NULLIF(EXCLUDED.canonical_name, ''), pams_asset_master.canonical_name)
            END,
            display_name = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.display_name
                ELSE COALESCE(NULLIF(EXCLUDED.display_name, ''), pams_asset_master.display_name)
            END,
            primary_symbol = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.primary_symbol
                ELSE COALESCE(NULLIF(EXCLUDED.primary_symbol, ''), pams_asset_master.primary_symbol)
            END,
            asset_type = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.asset_type
                ELSE EXCLUDED.asset_type
            END,
            asset_subtype = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.asset_subtype
                ELSE COALESCE(NULLIF(EXCLUDED.asset_subtype, ''), pams_asset_master.asset_subtype)
            END,
            market = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.market
                ELSE COALESCE(NULLIF(EXCLUDED.market, ''), pams_asset_master.market)
            END,
            listing_region = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.listing_region
                ELSE COALESCE(NULLIF(EXCLUDED.listing_region, ''), pams_asset_master.listing_region)
            END,
            economic_region = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.economic_region
                ELSE COALESCE(NULLIF(EXCLUDED.economic_region, ''), pams_asset_master.economic_region)
            END,
            currency = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.currency
                WHEN EXCLUDED.currency IN ('CNY', 'HKD', 'USD', 'SEK', 'KRW', 'MXN') THEN EXCLUDED.currency
                WHEN pams_asset_master.currency IN ('CNY', 'HKD', 'USD', 'SEK', 'KRW', 'MXN') THEN pams_asset_master.currency
                ELSE NULLIF(EXCLUDED.currency, '')
            END,
            valuation_method = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.valuation_method
                ELSE COALESCE(NULLIF(EXCLUDED.valuation_method, ''), pams_asset_master.valuation_method)
            END,
            liquidity_profile = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.liquidity_profile
                ELSE COALESCE(NULLIF(EXCLUDED.liquidity_profile, ''), pams_asset_master.liquidity_profile)
            END,
            income_type = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.income_type
                ELSE COALESCE(NULLIF(EXCLUDED.income_type, ''), pams_asset_master.income_type)
            END,
            exposure_policy = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.exposure_policy
                ELSE COALESCE(NULLIF(EXCLUDED.exposure_policy, ''), pams_asset_master.exposure_policy)
            END,
            confidence = CASE
                WHEN pams_asset_master.source = 'manual' THEN pams_asset_master.confidence
                ELSE EXCLUDED.confidence
            END,
            raw_json = CASE
                WHEN pams_asset_master.source = 'manual' AND EXCLUDED.source <> 'manual'
                    THEN EXCLUDED.raw_json || pams_asset_master.raw_json
                ELSE pams_asset_master.raw_json || EXCLUDED.raw_json
            END,
            last_verified_at = CASE
                WHEN pams_asset_master.source = 'manual' AND EXCLUDED.source IS DISTINCT FROM 'manual'
                    THEN pams_asset_master.last_verified_at
                ELSE NOW()
            END,
            updated_at = NOW()
        """,
        {**asset, "raw_json": psycopg2.extras.Json(asset["raw_json"])},
    )
    for alias_type, alias_value, priority in _aliases_for_raw(raw, asset):
        cur.execute(
            """
            INSERT INTO pams_asset_alias (asset_id, alias_type, alias_value, source, priority)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (asset_id, alias_type, alias_value) DO UPDATE SET
                priority = LEAST(pams_asset_alias.priority, EXCLUDED.priority)
            """,
            (asset["asset_id"], alias_type, alias_value, source, priority),
        )
    _upsert_industry(cur, asset["asset_id"], raw, asset, source=source)
    _replace_system_themes(cur, asset["asset_id"], raw, asset, source=source)
    return asset["asset_id"]


def _upsert_industry(cur, asset_id: str, raw: dict[str, Any], asset: dict[str, Any], *, source: str) -> None:
    industry = resolveIndustryBucket(
        {
            **raw,
            "assetType": asset["asset_type"],
            "industry_bucket": raw.get("industry_bucket"),
            "industry": raw.get("industry") or raw.get("underlying_industry"),
        }
    )
    bucket = _text(raw.get("industry_bucket") or industry.get("bucket"))
    if bucket not in INDUSTRY_BUCKETS:
        bucket = "未穿透 / 未映射"
    leaf = _text(raw.get("industry_leaf") or raw.get("industry") or raw.get("underlying_industry") or industry.get("leaf"))
    sub_bucket = _text(raw.get("industry_sub_bucket")) or _industry_sub_bucket(leaf, bucket, asset.get("canonical_name"))
    scope = "fund_underlying_equity" if raw.get("parent_symbol") else _exposure_policy(asset["asset_type"])
    cur.execute(
        """
        INSERT INTO pams_asset_industry_map (
            asset_id, industry_system, industry_leaf, industry_sub_bucket, industry_bucket,
            classification_scope, source, confidence, evidence, as_of, is_primary
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (asset_id, industry_system, classification_scope, is_primary) DO UPDATE SET
            industry_leaf = EXCLUDED.industry_leaf,
            industry_sub_bucket = EXCLUDED.industry_sub_bucket,
            industry_bucket = EXCLUDED.industry_bucket,
            source = EXCLUDED.source,
            confidence = EXCLUDED.confidence,
            evidence = EXCLUDED.evidence,
            as_of = EXCLUDED.as_of,
            updated_at = NOW()
        WHERE pams_asset_industry_map.source IS DISTINCT FROM 'manual' OR EXCLUDED.source = 'manual'
        """,
        (
            asset_id,
            _text(raw.get("industry_system")) or ("SW" if raw.get("sw_l1") else "manual"),
            leaf,
            sub_bucket,
            bucket,
            scope,
            source,
            asset.get("confidence") or "medium",
            _text(raw.get("evidence")) or _text(industry.get("reason")),
            _date_or_none(raw.get("as_of")),
        ),
    )


def _replace_system_themes(cur, asset_id: str, raw: dict[str, Any], asset: dict[str, Any], *, source: str) -> None:
    cur.execute(
        "DELETE FROM pams_asset_theme_map WHERE asset_id = %s AND source IN ('system', 'config', 'lookthrough_config', 'rule')",
        (asset_id,),
    )
    if asset["asset_type"] in NON_THEME_TYPES:
        return
    for theme in resolveThemeExposures({**raw, "assetType": asset["asset_type"]}):
        cur.execute(
            """
            INSERT INTO pams_asset_theme_map (
                asset_id, theme_id, theme_name, parent_theme_id, theme_level, family,
                score, role, source, confidence, evidence, as_of
            )
            VALUES (%s, %s, %s, NULL, 'parent', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                theme["themeId"],
                theme["themeName"],
                theme["family"],
                theme["score"],
                theme["role"],
                source,
                asset.get("confidence") or theme["confidence"],
                "seeded from asset mapping/rules",
                _date_or_none(raw.get("as_of")),
            ),
        )
    for sub_id, score in _ai_sub_theme_rules({**raw, "assetType": asset["asset_type"]}):
        cur.execute(
            """
            INSERT INTO pams_asset_theme_map (
                asset_id, theme_id, theme_name, parent_theme_id, theme_level, family,
                score, role, source, confidence, evidence, as_of
            )
            VALUES (%s, %s, %s, 'ai_compute', 'child', '科技成长', %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                sub_id,
                AI_SUB_THEME_DEFINITIONS.get(sub_id, sub_id),
                score,
                _role(score),
                source,
                asset.get("confidence") or "medium",
                "AI chain sub-theme seed",
                _date_or_none(raw.get("as_of")),
            ),
        )


def _raw_from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_name": mapping.get("asset_name"),
        "symbol": mapping.get("symbol"),
        "asset_type": mapping.get("asset_class"),
        "asset_class": mapping.get("asset_class"),
        "region": mapping.get("region"),
        "currency": mapping.get("currency"),
        "industry": mapping.get("industry"),
        "theme": mapping.get("theme"),
        "aliases": mapping.get("aliases") or [],
        "mapping_confidence": mapping.get("mapping_confidence"),
        "as_of": mapping.get("as_of"),
        "raw_mapping": mapping,
    }


def _raw_from_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_name": position.get("asset_name") or position.get("name"),
        "symbol": position.get("symbol") or position.get("asset_id"),
        "asset_id": position.get("asset_id"),
        "asset_type": position.get("mapped_asset_class") or position.get("asset_class_override") or position.get("asset_type") or position.get("asset_class"),
        "asset_class": position.get("mapped_asset_class") or position.get("asset_class_override") or position.get("asset_class"),
        "region": position.get("region"),
        "currency": position.get("currency"),
        "industry": position.get("industry"),
        "theme": position.get("theme"),
        "sw_l1": position.get("sw_l1"),
        "as_of": position.get("as_of"),
        "source_type": position.get("source_type"),
    }


def _raw_from_unit_underlying(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_name": unit.get("underlying_name"),
        "symbol": unit.get("underlying_symbol"),
        "asset_type": unit.get("underlying_asset_class"),
        "region": unit.get("underlying_region"),
        "currency": unit.get("underlying_currency"),
        "industry": unit.get("underlying_industry"),
        "theme": unit.get("underlying_theme"),
        "sw_l1": unit.get("sw_l1"),
        "as_of": unit.get("as_of"),
        "parent_symbol": unit.get("holding_symbol"),
    }


def _raw_from_unit_parent(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_name": unit.get("holding_name"),
        "symbol": unit.get("holding_symbol"),
        "asset_type": unit.get("holding_asset_class") or "fund",
        "currency": unit.get("holding_currency"),
        "as_of": unit.get("as_of"),
    }


def build_lookthrough_seed_units(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten lookthrough.yaml fund seed rows into syncable unit records."""

    units: list[dict[str, Any]] = []
    config_as_of = config.get("as_of")
    for parent in config.get("asset_lookthrough") or []:
        lookthrough_type = _text(parent.get("lookthrough_type") or "PRODUCT_TAG").upper()
        parent_symbol = _text(parent.get("parent_symbol"))
        parent_name = _text(parent.get("parent_asset_name") or parent_symbol)
        parent_asset_class = _text(parent.get("parent_asset_type") or "fund")
        parent_as_of = parent.get("as_of") or config_as_of
        holdings = parent.get("holdings") or []
        if not holdings and (parent.get("underlying_name") or parent.get("underlying_symbol")):
            holdings = [parent]

        weight_sum = Decimal("0")
        for holding in holdings:
            weight = Decimal(str(_num(holding.get("underlying_weight"))))
            weight_sum += max(weight, Decimal("0"))
            units.append(
                {
                    "holding_name": parent_name,
                    "holding_symbol": parent_symbol,
                    "holding_asset_class": parent_asset_class,
                    "holding_currency": parent.get("parent_currency"),
                    "as_of": parent_as_of,
                    "lookthrough_type": lookthrough_type,
                    "source_type": parent.get("source_type"),
                    "confidence": _text(parent.get("confidence") or holding.get("confidence") or "medium").lower(),
                    "underlying_name": holding.get("underlying_name"),
                    "underlying_symbol": holding.get("underlying_symbol"),
                    "underlying_weight": weight,
                    "underlying_region": holding.get("underlying_region"),
                    "underlying_currency": holding.get("underlying_currency"),
                    "underlying_industry": holding.get("underlying_industry"),
                    "underlying_theme": holding.get("underlying_theme"),
                    "underlying_asset_class": holding.get("underlying_asset_class"),
                    "sw_l1": holding.get("sw_l1"),
                    "raw_parent": parent,
                }
            )

        residual = Decimal("1") - weight_sum
        if holdings and lookthrough_type in REAL_LOOKTHROUGH_TYPES and residual > Decimal("0.001"):
            units.append(
                {
                    "holding_name": parent_name,
                    "holding_symbol": parent_symbol,
                    "holding_asset_class": parent_asset_class,
                    "holding_currency": parent.get("parent_currency"),
                    "as_of": parent_as_of,
                    "lookthrough_type": "PRODUCT_TAG",
                    "source_type": parent.get("source_type"),
                    "confidence": "low",
                    "underlying_name": "未穿透残差",
                    "underlying_symbol": "",
                    "underlying_weight": residual,
                    "underlying_region": "",
                    "underlying_currency": "",
                    "underlying_industry": "",
                    "underlying_theme": "",
                    "underlying_asset_class": "fund",
                    "raw_parent": parent,
                }
            )
    return units


def _lookthrough_source(value: Any) -> str:
    key = _text(value).upper()
    if key in {"FULL_HOLDINGS", "OFFICIAL"}:
        return "official"
    if key == "INDEX_PROXY":
        return "index_proxy"
    if key == "TOP10":
        return "top10"
    if key == "PRODUCT_TAG":
        return "product_tag"
    return "manual"


def _coverage_type(value: Any) -> str:
    key = _text(value).upper()
    return {
        "FULL_HOLDINGS": "full",
        "INDEX_PROXY": "index_proxy",
        "TOP10": "top10",
        "PRODUCT_TAG": "residual",
        "MISSING": "missing",
    }.get(key, "manual")


def _sync_asset_master_seed(
    cur,
    *,
    positions: list[dict[str, Any]],
    units: list[dict[str, Any]],
    config: dict[str, Any] | None,
    load_lookup: bool,
) -> dict[str, Any]:
    ensure_asset_master_schema(cur)
    cfg = config or {}
    seeded_assets: set[str] = set()
    for mapping in cfg.get("asset_mapping") or []:
        seeded_assets.add(_upsert_asset(cur, _raw_from_mapping(mapping), source="config"))
    for position in positions:
        seeded_assets.add(_upsert_asset(cur, _raw_from_position(position), source="system"))

    parents_with_real_lookthrough = {
        key
        for unit in units
        if _text(unit.get("lookthrough_type")).upper() not in {"", "DIRECT", "PRODUCT_TAG", "MISSING"}
        for key in _identity_keys(unit.get("holding_symbol"), unit.get("holding_name"))
    }
    cleaned_funds: set[str] = set()
    fund_asset_ids: set[str] = set()
    underlying_rows = 0
    for unit in units:
        parent_asset_id = _upsert_asset(cur, _raw_from_unit_parent(unit), source="lookthrough_config")
        seeded_assets.add(parent_asset_id)
        fund_asset_ids.add(parent_asset_id)
        lookthrough_type = _text(unit.get("lookthrough_type")).upper()
        if lookthrough_type in {"", "DIRECT", "MISSING"}:
            continue
        parent_keys = _identity_keys(unit.get("holding_symbol"), unit.get("holding_name"))
        if lookthrough_type == "PRODUCT_TAG" and not (parent_keys & parents_with_real_lookthrough):
            continue
        if parent_asset_id not in cleaned_funds:
            cur.execute(
                "DELETE FROM pams_fund_underlying_position WHERE fund_asset_id = %s AND source = 'lookthrough_config'",
                (parent_asset_id,),
            )
            cleaned_funds.add(parent_asset_id)
        is_residual = lookthrough_type == "PRODUCT_TAG"
        underlying_asset_id = None
        underlying_asset_type = "未穿透残差"
        if not is_residual:
            underlying_raw = _raw_from_unit_underlying(unit)
            underlying_asset_id = _upsert_asset(cur, underlying_raw, source="lookthrough_config")
            seeded_assets.add(underlying_asset_id)
            underlying_asset_type = resolveAssetType(underlying_raw)
        cur.execute(
            """
            INSERT INTO pams_fund_underlying_position (
                fund_asset_id, underlying_asset_id, underlying_name, underlying_symbol,
                underlying_weight, underlying_asset_type, as_of, effective_source,
                coverage_type, confidence, source, raw_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'lookthrough_config', %s)
            """,
            (
                parent_asset_id,
                underlying_asset_id,
                "未穿透残差" if is_residual else _text(unit.get("underlying_name")),
                "" if is_residual else _text(unit.get("underlying_symbol")),
                _num(unit.get("underlying_weight")),
                underlying_asset_type,
                _date_or_none(unit.get("as_of")),
                _lookthrough_source(lookthrough_type),
                _coverage_type(lookthrough_type),
                _text(unit.get("confidence") or "medium").lower(),
                psycopg2.extras.Json(_json_ready(dict(unit))),
            ),
        )
        underlying_rows += 1
    result = {
        "status": "ok",
        "seeded_assets": len(seeded_assets),
        "asset_mapping_rows": len(cfg.get("asset_mapping") or []),
        "lookthrough_units": len(units),
        "funds_seen": len(fund_asset_ids),
        "fund_underlying_rows": underlying_rows,
    }
    if load_lookup:
        result["lookup"] = load_asset_master_lookup(cur)
    return result


def sync_asset_master_from_analysis(
    cur,
    *,
    positions: list[dict[str, Any]],
    units: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _sync_asset_master_seed(
        cur,
        positions=positions,
        units=units,
        config=config,
        load_lookup=True,
    )
    return result["lookup"]


def sync_asset_master_from_lookthrough_config(
    cur,
    *,
    config: dict[str, Any],
    load_lookup: bool = True,
) -> dict[str, Any]:
    """Actively seed asset master and fund underlyings from lookthrough.yaml."""

    units = build_lookthrough_seed_units(config)
    return _sync_asset_master_seed(
        cur,
        positions=[],
        units=units,
        config=config,
        load_lookup=load_lookup,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def load_asset_master_lookup(cur, *, ensure_schema: bool = True) -> dict[str, Any]:
    if ensure_schema:
        ensure_asset_master_schema(cur)
    cur.execute("SELECT * FROM pams_asset_master")
    assets = [{k: _jsonable(v) for k, v in row.items()} for row in cur.fetchall()]
    by_id = {row["asset_id"]: {**row, "aliases": [], "industry": None, "themes": []} for row in assets}

    cur.execute("SELECT asset_id, alias_type, alias_value, source, priority FROM pams_asset_alias ORDER BY priority ASC")
    alias_to_id: dict[str, str] = {}
    for row in cur.fetchall():
        asset = by_id.get(row["asset_id"])
        if not asset:
            continue
        alias = {k: _jsonable(v) for k, v in row.items()}
        asset["aliases"].append(alias)
        alias_to_id.setdefault(_norm_key(row["alias_value"]), row["asset_id"])

    cur.execute(
        """
        SELECT DISTINCT ON (asset_id) *
        FROM pams_asset_industry_map
        WHERE is_primary = TRUE
        ORDER BY asset_id, (source = 'manual') DESC NULLS LAST, updated_at DESC
        """
    )
    for row in cur.fetchall():
        asset = by_id.get(row["asset_id"])
        if asset:
            asset["industry"] = {k: _jsonable(v) for k, v in row.items()}

    cur.execute("SELECT * FROM pams_asset_theme_map ORDER BY theme_level, score DESC")
    for row in cur.fetchall():
        asset = by_id.get(row["asset_id"])
        if asset:
            asset["themes"].append({k: _jsonable(v) for k, v in row.items()})

    return {"assets": by_id, "alias_to_id": alias_to_id}


def _find_asset(raw: dict[str, Any], lookup: dict[str, Any]) -> dict[str, Any] | None:
    assets = lookup.get("assets", {}) or {}
    symbol = raw.get("symbol") or raw.get("underlying_symbol") or raw.get("holding_symbol") or raw.get("asset_id")
    name = raw.get("asset_name") or raw.get("name") or raw.get("underlying_name") or raw.get("holding_name")
    asset_type_hint = (
        raw.get("mapped_asset_class")
        or raw.get("asset_class_override")
        or raw.get("asset_class")
        or raw.get("asset_type")
        or raw.get("underlying_asset_class")
        or raw.get("holding_asset_class")
    )
    if asset_type_hint:
        expected_asset_id = asset_id_for(symbol, name, resolveAssetType({"asset_type": asset_type_hint, "symbol": symbol, "asset_name": name}))
        if expected_asset_id in assets:
            return assets[expected_asset_id]

    alias_to_id = lookup.get("alias_to_id") or {}
    for key in _identity_keys(
        raw.get("asset_master_id"),
        raw.get("asset_id"),
        raw.get("symbol"),
        raw.get("underlying_symbol"),
        raw.get("asset_name"),
        raw.get("name"),
        raw.get("underlying_name"),
    ):
        asset_id = alias_to_id.get(_norm_key(key)) or (key if key in assets else None)
        if asset_id and asset_id in assets:
            return assets[asset_id]
    return None


def _theme_exposures_from_asset(asset: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in asset.get("themes") or []:
        if row.get("theme_level") != "parent":
            continue
        theme_id = row.get("theme_id")
        name = row.get("theme_name")
        family = row.get("family") or THEME_DEFINITIONS.get(theme_id, ("", "未映射主题"))[1]
        out.append(
            {
                "themeId": theme_id,
                "themeName": name,
                "family": family,
                "score": float(row.get("score") or 0),
                "role": row.get("role") or _role(float(row.get("score") or 0)),
                "source": "manual" if row.get("source") == "manual" else "rule",
                "confidence": row.get("confidence") or "medium",
            }
        )
    return out


def _ai_subthemes_from_asset(asset: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in asset.get("themes") or []:
        if row.get("theme_level") != "child" or row.get("parent_theme_id") != "ai_compute":
            continue
        out.append(
            {
                "subThemeId": row.get("theme_id"),
                "subThemeName": row.get("theme_name"),
                "score": float(row.get("score") or 0),
                "role": row.get("role") or _role(float(row.get("score") or 0)),
                "source": row.get("source") or "system",
                "confidence": row.get("confidence") or "medium",
            }
        )
    return out


def _apply_asset_to_raw(raw: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    item = dict(raw)
    asset_currency = _valid_currency(asset.get("currency"))
    item["asset_master_id"] = asset.get("asset_id")
    item["asset_type"] = asset.get("asset_type") or item.get("asset_type")
    item["asset_subtype"] = asset.get("asset_subtype") or item.get("asset_subtype")
    item["market"] = asset.get("market") or item.get("market")
    item["region"] = asset.get("economic_region") or item.get("region")
    item["currency"] = asset_currency or item.get("currency")
    industry = asset.get("industry") or {}
    if industry:
        item["industry_bucket"] = industry.get("industry_bucket")
        item["industry_sub_bucket"] = industry.get("industry_sub_bucket")
        item["industry"] = industry.get("industry_leaf") or industry.get("industry_sub_bucket") or item.get("industry")
        item["industry_bucket_source"] = "asset_master"
    themes = _theme_exposures_from_asset(asset)
    if themes:
        item["theme_exposures"] = themes
    ai_subthemes = _ai_subthemes_from_asset(asset)
    if ai_subthemes:
        item["ai_sub_themes"] = ai_subthemes
    return item


def enrich_positions_with_asset_master(positions: list[dict[str, Any]], lookup: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for position in positions:
        asset = _find_asset(position, lookup)
        out.append(_apply_asset_to_raw(position, asset) if asset else dict(position))
    return out


def enrich_units_with_asset_master(units: list[dict[str, Any]], lookup: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for unit in units:
        item = dict(unit)
        parent_asset = _find_asset(
            {
                "symbol": item.get("holding_symbol"),
                "asset_name": item.get("holding_name"),
                "asset_type": item.get("holding_asset_class"),
            },
            lookup,
        )
        if parent_asset:
            item["holding_asset_id"] = parent_asset.get("asset_id")
            item["holding_asset_class"] = parent_asset.get("asset_type") or item.get("holding_asset_class")
        underlying_asset = _find_asset(
            {
                "symbol": item.get("underlying_symbol"),
                "asset_name": item.get("underlying_name"),
                "asset_type": item.get("underlying_asset_class"),
            },
            lookup,
        )
        if underlying_asset and _text(item.get("lookthrough_type")).upper() != "PRODUCT_TAG":
            underlying_currency = _valid_currency(underlying_asset.get("currency"))
            item["underlying_asset_id"] = underlying_asset.get("asset_id")
            item["underlying_asset_class"] = underlying_asset.get("asset_type") or item.get("underlying_asset_class")
            item["underlying_region"] = underlying_asset.get("economic_region") or item.get("underlying_region")
            item["underlying_currency"] = underlying_currency or item.get("underlying_currency")
            industry = underlying_asset.get("industry") or {}
            if industry:
                item["industry_bucket"] = industry.get("industry_bucket")
                item["industry_sub_bucket"] = industry.get("industry_sub_bucket")
                item["underlying_industry"] = industry.get("industry_leaf") or industry.get("industry_sub_bucket") or item.get("underlying_industry")
                item["industry_bucket_source"] = "asset_master"
            themes = _theme_exposures_from_asset(underlying_asset)
            if themes:
                item["underlying_theme_exposures"] = themes
            ai_subthemes = _ai_subthemes_from_asset(underlying_asset)
            if ai_subthemes:
                item["underlying_ai_sub_themes"] = ai_subthemes
        out.append(item)
    return out


def build_asset_master_payload(
    *,
    positions: list[dict[str, Any]],
    units: list[dict[str, Any]],
    lookup: dict[str, Any],
) -> dict[str, Any]:
    assets = lookup.get("assets") or {}
    current_assets: dict[str, dict[str, Any]] = {}
    for row in positions:
        asset = _find_asset(row, lookup)
        if asset:
            current_assets[asset["asset_id"]] = asset
    for unit in units:
        for raw in (
            {
                "symbol": unit.get("holding_symbol"),
                "asset_name": unit.get("holding_name"),
                "asset_type": unit.get("holding_asset_class"),
            },
            {
                "symbol": unit.get("underlying_symbol"),
                "asset_name": unit.get("underlying_name"),
                "asset_type": unit.get("underlying_asset_class"),
            },
        ):
            asset = _find_asset(raw, lookup)
            if asset:
                current_assets[asset["asset_id"]] = asset

    current = list(current_assets.values())
    industry_mapped = [a for a in current if (a.get("industry") or {}).get("industry_bucket") not in {None, "", "未穿透 / 未映射"}]
    theme_mapped = [a for a in current if any(t.get("theme_level") == "parent" for t in a.get("themes") or [])]
    low_confidence = [
        {
            "asset_id": a["asset_id"],
            "name": a.get("display_name") or a.get("canonical_name"),
            "symbol": a.get("primary_symbol"),
            "asset_type": a.get("asset_type"),
            "confidence": a.get("confidence"),
            "industry_bucket": (a.get("industry") or {}).get("industry_bucket"),
            "theme_count": len([t for t in a.get("themes") or [] if t.get("theme_level") == "parent"]),
        }
        for a in current
        if a.get("confidence") == "low"
        or (a.get("asset_type") in EQUITY_TYPES and not (a.get("industry") or {}).get("industry_bucket"))
    ]
    fund_rows = [
        unit for unit in units
        if _text(unit.get("lookthrough_type")).upper() not in {"", "DIRECT", "MISSING"}
    ]
    fund_count = len({asset_id_for(unit.get("holding_symbol"), unit.get("holding_name"), "ETF / 基金") for unit in fund_rows})
    fund_groups: dict[str, dict[str, Any]] = {}
    for unit in fund_rows:
        lookthrough_type = _text(unit.get("lookthrough_type")).upper()
        fund_key = asset_id_for(unit.get("holding_symbol"), unit.get("holding_name"), unit.get("holding_asset_class") or "ETF / 基金")
        group = fund_groups.setdefault(
            fund_key,
            {
                "fund_asset_id": fund_key,
                "fund_name": _text(unit.get("holding_name")) or fund_key,
                "fund_symbol": _text(unit.get("holding_symbol")),
                "fund_market_value_cny": _num(unit.get("holding_market_value_cny")),
                "lookthrough_type": lookthrough_type,
                "coverage_ratio": 0.0,
                "underlyings": [],
            },
        )
        group["fund_market_value_cny"] = max(group.get("fund_market_value_cny") or 0.0, _num(unit.get("holding_market_value_cny")))
        if lookthrough_type == "PRODUCT_TAG":
            continue
        weight = max(_num(unit.get("underlying_weight")), 0.0)
        group["coverage_ratio"] += weight
        group["underlyings"].append(
            {
                "name": _text(unit.get("underlying_name")) or _text(unit.get("underlying_symbol")) or "未命名底层",
                "symbol": _text(unit.get("underlying_symbol")),
                "weight_in_fund": weight,
                "value_cny": _num(unit.get("value_cny")),
                "asset_type": _text(unit.get("underlying_asset_class")),
                "industry_bucket": _text(unit.get("industry_bucket")) or "未穿透 / 未映射",
                "industry_leaf": _text(unit.get("underlying_industry")),
                "currency": _text(unit.get("underlying_currency")),
                "lookthrough_type": lookthrough_type,
            }
        )
    fund_underlying_top10 = []
    for group in fund_groups.values():
        rows = sorted(group["underlyings"], key=lambda row: -abs(_num(row.get("value_cny"))))[:10]
        if not rows:
            continue
        fund_underlying_top10.append(
            {
                **group,
                "coverage_ratio": min(group.get("coverage_ratio") or 0.0, 1.0),
                "underlyings": rows,
            }
        )
    fund_underlying_top10.sort(key=lambda row: -abs(_num(row.get("fund_market_value_cny"))))
    return {
        "status": "ok",
        "model_version": "asset_master_v1",
        "tables": [
            "pams_asset_master",
            "pams_asset_alias",
            "pams_asset_industry_map",
            "pams_asset_theme_map",
            "pams_fund_underlying_position",
            "pams_asset_fixed_income_profile",
            "pams_asset_real_asset_profile",
        ],
        "total_assets": len(assets),
        "current_assets": len(current),
        "current_industry_mapped": len(industry_mapped),
        "current_theme_mapped": len(theme_mapped),
        "funds_with_underlyings": fund_count,
        "fund_underlying_rows": len(fund_rows),
        "fund_underlying_top10": fund_underlying_top10[:12],
        "coverage": {
            "industry": len(industry_mapped) / len(current) if current else 0.0,
            "theme": len(theme_mapped) / len(current) if current else 0.0,
        },
        "low_confidence_assets": sorted(low_confidence, key=lambda r: (r["confidence"] != "low", r["name"]))[:30],
        "field_contract": [
            {"table": "asset_master", "meaning": "标的身份、资产类型、市种、地区、币种、估值和流动性口径"},
            {"table": "asset_alias", "meaning": "同花顺、IBKR、腾讯理财通、基金代码、展示名等别名统一到 asset_id"},
            {"table": "asset_industry_map", "meaning": "一级行业、二级行业和原始行业留痕；只服务行业维度"},
            {"table": "asset_theme_map", "meaning": "父主题/子主题、score、role、confidence；主题允许重叠"},
            {"table": "fund_underlying_position", "meaning": "基金到底层股票、债券、现金、商品或残差的权重连接"},
        ],
    }


def _asset_ref(raw: dict[str, Any], *, reason: str, asset: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "asset_id": (asset or {}).get("asset_id"),
        "symbol": _text(
            raw.get("symbol")
            or raw.get("primary_symbol")
            or raw.get("underlying_symbol")
            or raw.get("holding_symbol")
            or raw.get("asset_id")
        ),
        "name": _text(
            raw.get("asset_name")
            or raw.get("name")
            or raw.get("underlying_name")
            or raw.get("holding_name")
            or raw.get("symbol")
            or raw.get("underlying_symbol")
            or raw.get("holding_symbol")
        ),
        "asset_type": (asset or {}).get("asset_type") or _text(raw.get("asset_type") or raw.get("underlying_asset_class") or raw.get("holding_asset_class")),
        "reason": reason,
    }


def _dedupe_refs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = (
            _text(row.get("asset_id")),
            _text(row.get("symbol")),
            _text(row.get("name")),
            _text(row.get("reason")),
            _text(row.get("value")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _asset_parent_theme_rows(asset: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in asset.get("themes") or [] if row.get("theme_level") == "parent"]


def validate_asset_master_whitelists(lookup: dict[str, Any], *, strict: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Validate P1 whitelist boundaries for asset master data."""

    problems: dict[str, list[dict[str, Any]]] = {
        "invalid_asset_type": [],
        "invalid_industry": [],
        "invalid_theme": [],
        "invalid_market": [],
        "invalid_region": [],
        "invalid_currency": [],
    }
    for asset in (lookup.get("assets") or {}).values():
        ref = _asset_ref(asset, reason="whitelist", asset=asset)
        asset_type = _text(asset.get("asset_type"))
        if asset_type and asset_type not in ASSET_TYPES:
            problems["invalid_asset_type"].append({**ref, "value": asset_type})

        market = _text(asset.get("market"))
        if market and market not in VALID_MARKETS:
            problems["invalid_market"].append({**ref, "value": market})

        region = _text(asset.get("economic_region"))
        if region and region not in VALID_REGIONS:
            problems["invalid_region"].append({**ref, "value": region})

        currency = _text(asset.get("currency"))
        if currency and currency not in VALID_CURRENCIES:
            problems["invalid_currency"].append({**ref, "value": currency})

        industry = asset.get("industry") or {}
        bucket = _text(industry.get("industry_bucket"))
        if bucket and (bucket not in INDUSTRY_BUCKETS or bucket in FORBIDDEN_INDUSTRY_LABELS):
            problems["invalid_industry"].append({**ref, "value": bucket})

        for theme in _asset_parent_theme_rows(asset):
            name = _text(theme.get("theme_name"))
            if name and (name not in VALID_THEME_NAMES or name in FORBIDDEN_THEME_LABELS):
                problems["invalid_theme"].append({**ref, "value": name})

    if strict:
        non_empty = {key: value for key, value in problems.items() if value}
        if non_empty:
            details = ", ".join(f"{key}={len(value)}" for key, value in non_empty.items())
            raise ValueError(f"asset master whitelist validation failed: {details}")
    return {key: _dedupe_refs(value) for key, value in problems.items()}


def build_asset_master_missing_mapping_report(
    *,
    positions: list[dict[str, Any]],
    units: list[dict[str, Any]],
    lookup: dict[str, Any],
) -> dict[str, Any]:
    """Return P1 missing mapping lists for current holdings and fund seeds."""

    current_assets: dict[str, dict[str, Any]] = {}
    missing_asset_master: list[dict[str, Any]] = []
    missing_industry: list[dict[str, Any]] = []
    missing_theme: list[dict[str, Any]] = []
    missing_fund_underlying: list[dict[str, Any]] = []

    real_parent_keys = {
        key
        for unit in units
        if _text(unit.get("lookthrough_type")).upper() in REAL_LOOKTHROUGH_TYPES
        for key in _identity_keys(unit.get("holding_symbol"), unit.get("holding_name"))
    }

    for position in positions:
        asset = _find_asset(position, lookup)
        raw_asset_type = resolveAssetType(_raw_from_position(position))
        if not asset:
            missing_asset_master.append(_asset_ref(position, reason="position_missing_asset_master"))
        else:
            current_assets[asset["asset_id"]] = asset
            raw_asset_type = asset.get("asset_type") or raw_asset_type

        position_keys = _identity_keys(position.get("symbol"), position.get("asset_id"), position.get("asset_name"), position.get("name"))
        if raw_asset_type == "ETF / 基金" and not (position_keys & real_parent_keys):
            missing_fund_underlying.append(_asset_ref(position, reason="fund_without_real_lookthrough", asset=asset))

    for unit in units:
        if _text(unit.get("lookthrough_type")).upper() == "PRODUCT_TAG":
            continue
        raw = {
            "symbol": unit.get("underlying_symbol"),
            "asset_name": unit.get("underlying_name"),
            "asset_type": unit.get("underlying_asset_class"),
        }
        if not raw.get("symbol") and not raw.get("asset_name"):
            continue
        asset = _find_asset(raw, lookup)
        if not asset:
            missing_asset_master.append(_asset_ref(raw, reason="underlying_missing_asset_master"))
            continue
        current_assets[asset["asset_id"]] = asset

    for asset in current_assets.values():
        asset_type = _text(asset.get("asset_type"))
        industry = asset.get("industry") or {}
        bucket = _text(industry.get("industry_bucket"))
        if asset_type in EQUITY_TYPES and bucket in {"", UNMAPPED_INDUSTRY}:
            missing_industry.append(_asset_ref(asset, reason="equity_missing_industry", asset=asset))

        parent_themes = _asset_parent_theme_rows(asset)
        mapped_themes = [row for row in parent_themes if _text(row.get("theme_name")) not in {"", UNMAPPED_THEME_NAME}]
        if asset_type not in NON_THEME_TYPES and not mapped_themes:
            missing_theme.append(_asset_ref(asset, reason="theme_missing_or_unmapped", asset=asset))

    whitelist = validate_asset_master_whitelists(lookup, strict=False)
    return {
        "status": "ok",
        "missing_asset_master": _dedupe_refs(missing_asset_master),
        "missing_industry": _dedupe_refs(missing_industry),
        "missing_theme": _dedupe_refs(missing_theme),
        "missing_fund_underlying": _dedupe_refs(missing_fund_underlying),
        "whitelist": whitelist,
        "summary": {
            "missing_asset_master": len(_dedupe_refs(missing_asset_master)),
            "missing_industry": len(_dedupe_refs(missing_industry)),
            "missing_theme": len(_dedupe_refs(missing_theme)),
            "missing_fund_underlying": len(_dedupe_refs(missing_fund_underlying)),
            "invalid_whitelist": sum(len(rows) for rows in whitelist.values()),
        },
    }
