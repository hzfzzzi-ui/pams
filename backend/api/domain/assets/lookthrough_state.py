"""Lookthrough exposure assembly for the PAMS analysis module.

This MVP is intentionally config-backed.  It separates product-level mapping
from partial ETF/fund lookthrough data, so risk pages can distinguish real
lookthrough coverage from product-tag estimates.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date as Date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from domain.assets.asset_master import (
    build_asset_master_payload,
    enrich_positions_with_asset_master,
    enrich_units_with_asset_master,
    load_asset_master_lookup,
    sync_asset_master_from_analysis,
)
from domain.assets.exposure_engine import build_exposure_analysis_payload
from domain.investments.portfolio_state import _load_accounts_config, load_account_balances, load_investment_positions


ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = Path(os.environ.get("PAMS_LOOKTHROUGH_CONFIG", "/app/config/lookthrough.yaml"))
LOCAL_CONFIG_PATH = ROOT / "config" / "lookthrough.yaml"

LOOKTHROUGH_TYPES = {
    "DIRECT",
    "FULL_HOLDINGS",
    "TOP10",
    "INDEX_PROXY",
    "PRODUCT_TAG",
    "MANUAL",
    "MISSING",
}

SOURCE_TYPES = {"OFFICIAL", "OFFICIAL_INDEX", "FUND_REPORT", "MANUAL", "ESTIMATED"}
CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
QUALITY_TYPES = {
    "DIRECT": "FULL",
    "FULL_HOLDINGS": "FULL",
    "TOP10": "TOP10_ONLY",
    "INDEX_PROXY": "INDEX_PROXY",
    "PRODUCT_TAG": "PRODUCT_TAG_ONLY",
    "MANUAL": "PRODUCT_TAG_ONLY",
    "MISSING": "MISSING",
}

EXPOSURE_DIMENSIONS = {
    "risk_bucket": "underlying_risk_bucket",
    "industry": "industry_bucket",
    "theme": "underlying_theme",
    "currency": "underlying_currency",
    "region": "underlying_region",
    "underlying_asset": "underlying_name",
}

RISK_BUCKET_DEFAULT = "OTHER"

DEFAULT_RISK_BUCKET_LABELS = {
    "HUAWEI_ESOP": "华为ESOP / 内部股份",
    "US_GLOBAL_TECH": "美股/全球科技",
    "CN_EQUITY": "A股/中国权益",
    "GOLD_RESOURCES": "黄金/资源",
    "CASH_SHORT_DURATION": "现金/短久期",
    "FX": "汇率",
    "OTHER": "其他",
}

CASH_LIKE_RISK_BUCKETS = {"CASH_SHORT_DURATION", "FX"}
CASH_LIKE_ASSET_CLASSES = {
    "cash",
    "deposit",
    "money_market",
    "short_bond",
    "short_duration_usd",
    "t_bill",
    "treasury_bill",
}
CASH_LIKE_INDUSTRY_LABELS = {"现金/短久期", "货币基金", "美国短债", "住房公积金"}
CASH_LIKE_THEME_LABELS = {"现金管理", "福利账户/住房公积金"}
EXCLUDE_CASH_DIMENSIONS = {"industry", "theme"}

INDUSTRY_UNMAPPED = "未穿透 / 未映射"

SW_L1_INDUSTRY_BUCKETS = {
    "银行": "金融地产",
    "非银金融": "金融地产",
    "房地产": "金融地产",
    "食品饮料": "大消费",
    "家用电器": "大消费",
    "商贸零售": "大消费",
    "社会服务": "大消费",
    "纺织服饰": "大消费",
    "农林牧渔": "大消费",
    "美容护理": "大消费",
    "电子": "科技TMT",
    "计算机": "科技TMT",
    "通信": "科技TMT",
    "传媒": "科技TMT",
    "电力设备": "高端制造",
    "汽车": "高端制造",
    "机械设备": "高端制造",
    "国防军工": "高端制造",
    "有色金属": "周期资源",
    "煤炭": "周期资源",
    "石油石化": "周期资源",
    "基础化工": "周期资源",
    "钢铁": "周期资源",
    "建筑材料": "周期资源",
    "医药生物": "医药医疗",
    "公用事业": "稳定公用",
    "交通运输": "稳定公用",
    "环保": "稳定公用",
    "建筑装饰": "稳定公用",
}

INDUSTRY_LABEL_BUCKETS = {
    **SW_L1_INDUSTRY_BUCKETS,
    # Common A-share / HK-share / product labels that are not SW L1 names.
    "证券": "金融地产",
    "保险": "金融地产",
    "多元金融": "金融地产",
    "全国地产": "金融地产",
    "房地产开发": "金融地产",
    "白酒": "大消费",
    "啤酒": "大消费",
    "食品": "大消费",
    "食品饮料": "大消费",
    "软饮料": "大消费",
    "乳制品": "大消费",
    "饲料": "大消费",
    "农业综合": "大消费",
    "农牧养殖": "大消费",
    "服饰": "大消费",
    "家居用品": "大消费",
    "商品城": "大消费",
    "旅游服务": "大消费",
    "文教休闲": "大消费",
    "半导体": "科技TMT",
    "元器件": "科技TMT",
    "电子制造": "科技TMT",
    "通信设备": "科技TMT",
    "电信运营": "科技TMT",
    "软件": "科技TMT",
    "软件服务": "科技TMT",
    "互联网": "科技TMT",
    "互联网软件": "科技TMT",
    "互联网平台": "科技TMT",
    "港股科技": "科技TMT",
    "科技": "科技TMT",
    "科技综合": "科技TMT",
    "科创板": "科技TMT",
    "人工智能": "科技TMT",
    "IT设备": "科技TMT",
    "影视音像": "科技TMT",
    "工程机械": "高端制造",
    "专用机械": "高端制造",
    "机械基件": "高端制造",
    "电气设备": "高端制造",
    "电器仪表": "高端制造",
    "汽车整车": "高端制造",
    "汽车配件": "高端制造",
    "摩托车": "高端制造",
    "航空": "高端制造",
    "船舶": "高端制造",
    "运输设备": "高端制造",
    "贵金属": "周期资源",
    "贵金属与铜": "周期资源",
    "黄金": "周期资源",
    "铜": "周期资源",
    "铝": "周期资源",
    "小金属": "周期资源",
    "煤炭开采": "周期资源",
    "石油加工": "周期资源",
    "石油开采": "周期资源",
    "化工原料": "周期资源",
    "化纤": "周期资源",
    "农药化肥": "周期资源",
    "普钢": "周期资源",
    "特种钢": "周期资源",
    "水泥": "周期资源",
    "玻璃": "周期资源",
    "其他建材": "周期资源",
    "矿物制品": "周期资源",
    "造纸": "周期资源",
    "化学制药": "医药医疗",
    "生物制药": "医药医疗",
    "中成药": "医药医疗",
    "医疗保健": "医药医疗",
    "医药商业": "医药医疗",
    "供气供热": "稳定公用",
    "水力发电": "稳定公用",
    "火力发电": "稳定公用",
    "新型电力": "稳定公用",
    "机场": "稳定公用",
    "空运": "稳定公用",
    "水运": "稳定公用",
    "港口": "稳定公用",
    "铁路": "稳定公用",
    "路桥": "稳定公用",
    "仓储物流": "稳定公用",
    "建筑工程": "稳定公用",
    # Overseas labels from index proxies and direct foreign stocks.
    "software": "科技TMT",
    "software/services": "科技TMT",
    "semiconductors": "科技TMT",
    "semiconductor equipment": "科技TMT",
    "communications equipment": "科技TMT",
    "consumer electronics": "科技TMT",
    "internet software": "科技TMT",
    "internet platforms": "科技TMT",
    "internet retail/cloud": "科技TMT",
    "streaming media": "科技TMT",
    "nasdaq-100 constituent": "科技TMT",
    "biotechnology": "医药医疗",
    "medical equipment": "医药医疗",
    "automobiles": "高端制造",
    "food and beverage": "大消费",
    "restaurants": "大消费",
    "retail": "大消费",
    "online travel": "大消费",
}

INDUSTRY_LABEL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("金融地产", ("bank", "financial", "insurance", "real estate", "broker", "securities")),
    ("大消费", ("consumer", "retail", "restaurant", "food", "beverage", "travel", "apparel")),
    ("科技TMT", ("software", "semiconductor", "internet", "cloud", "media", "streaming", "communications", "electronics", "technology", "nasdaq")),
    ("高端制造", ("automobile", "automotive", "machinery", "equipment", "aerospace", "defense", "manufacturing")),
    ("周期资源", ("metal", "mining", "coal", "oil", "chemical", "steel", "material", "gold", "copper", "energy")),
    ("医药医疗", ("biotech", "pharma", "medical", "health", "drug", "life science")),
    ("稳定公用", ("utility", "utilities", "transport", "infrastructure", "rail", "airport", "shipping", "logistics")),
]

RISK_BUCKET_INDUSTRY_BUCKETS = {
    "HUAWEI_ESOP": "科技TMT",
    "US_GLOBAL_TECH": "科技TMT",
    "GOLD_RESOURCES": "周期资源",
}


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _date_str(value: Any) -> str | None:
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse_date(value: Any) -> Date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    try:
        return Date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return _norm_text(value).lower()


def _lookup_keys(*values: Any) -> list[str]:
    keys: list[str] = []
    for value in values:
        raw = _norm_text(value)
        if not raw:
            continue
        # The original identifier must win over derived aliases.  A set here
        # makes first-match lookups depend on PYTHONHASHSEED.
        variants = [raw, raw.upper(), raw.lower()]
        match = re.search(r"(\d{6})", raw)
        if match:
            digits = match.group(1)
            variants.extend([digits, f"sh{digits}", f"sz{digits}", f"{digits}.SH", f"{digits}.SZ"])
        if raw.endswith(".KS"):
            variants.append(raw.replace(".KS", ""))
        for variant in variants:
            normalized = _key(variant)
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys


def _symbol_lookup_keys(symbol: Any) -> list[str]:
    raw = _norm_text(symbol)
    if not raw:
        return []
    keys = [raw, raw.lower()]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        exchange_prefix = "sh" if digits[0] in {"5", "6", "9"} else "sz"
        exchange_suffix = "SH" if exchange_prefix == "sh" else "SZ"
        keys.extend([digits, f"{exchange_prefix}{digits}", f"{digits}.{exchange_suffix}", f"{digits}.{exchange_suffix.lower()}"])
    out: list[str] = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    return out


def _industry_bucket_from_sw_l1(value: Any) -> str:
    sw_l1 = _norm_text(value)
    if not sw_l1:
        return INDUSTRY_UNMAPPED
    return SW_L1_INDUSTRY_BUCKETS.get(sw_l1, INDUSTRY_UNMAPPED)


def _industry_label_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _norm_text(value)).casefold()


def _industry_bucket_from_label(value: Any) -> str:
    key = _industry_label_key(value)
    if not key:
        return INDUSTRY_UNMAPPED
    exact = INDUSTRY_LABEL_BUCKETS.get(key) or INDUSTRY_LABEL_BUCKETS.get(_norm_text(value))
    if exact:
        return exact
    for bucket, keywords in INDUSTRY_LABEL_KEYWORDS:
        if any(keyword in key for keyword in keywords):
            return bucket
    return INDUSTRY_UNMAPPED


def _industry_bucket_from_risk_bucket(value: Any) -> str:
    risk_bucket = _norm_text(value).upper()
    if not risk_bucket:
        return INDUSTRY_UNMAPPED
    return RISK_BUCKET_INDUSTRY_BUCKETS.get(risk_bucket, INDUSTRY_UNMAPPED)


def _lookup_sw_l1(symbol: Any, industry_l1_by_symbol: dict[str, str] | None) -> str:
    if not industry_l1_by_symbol:
        return ""
    for key in _symbol_lookup_keys(symbol):
        found = industry_l1_by_symbol.get(key) or industry_l1_by_symbol.get(key.lower())
        if found:
            return _norm_text(found)
    return ""


def _load_sw_l1_lookup(cur) -> dict[str, str]:
    cur.execute(
        """
        SELECT DISTINCT ON (ts_code) ts_code, industry_l1
        FROM pit_industry_history
        WHERE standard = 'SW2021'
          AND COALESCE(industry_l1, '') <> ''
          AND (end_date IS NULL OR end_date >= CURRENT_DATE)
        ORDER BY ts_code, start_date DESC
        """
    )
    lookup: dict[str, str] = {}
    for row in cur.fetchall():
        ts_code = _norm_text(row.get("ts_code"))
        industry_l1 = _norm_text(row.get("industry_l1"))
        if not ts_code or not industry_l1:
            continue
        for key in _symbol_lookup_keys(ts_code):
            lookup[key] = industry_l1
            lookup[key.lower()] = industry_l1
    return lookup


def _load_yaml_config() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else LOCAL_CONFIG_PATH
    if not path.exists():
        return {"asset_mapping": [], "asset_lookthrough": []}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"asset_mapping": [], "asset_lookthrough": []}


def _normalize_mapping(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["symbol"] = _norm_text(out.get("symbol"))
    out["asset_name"] = _norm_text(out.get("asset_name") or out.get("name") or out.get("symbol"))
    out["risk_bucket"] = _norm_text(out.get("risk_bucket") or RISK_BUCKET_DEFAULT)
    out["region"] = _norm_text(out.get("region") or "Unknown")
    out["currency"] = _norm_text(out.get("currency") or "CNY").upper()
    out["industry"] = _norm_text(out.get("industry") or "未分类")
    out["theme"] = _norm_text(out.get("theme") or "未分类")
    out["style"] = _norm_text(out.get("style") or "未分类")
    out["asset_class"] = _norm_text(out.get("asset_class") or "other")
    out["mapping_confidence"] = _norm_text(out.get("mapping_confidence") or "LOW").upper()
    out["mapping_confidence"] = out["mapping_confidence"] if out["mapping_confidence"] in CONFIDENCE_RANK else "LOW"
    return out


def _normalize_lookthrough(parent: dict[str, Any], holding: dict[str, Any]) -> dict[str, Any]:
    item = {**parent, **holding}
    item.pop("holdings", None)
    item["parent_symbol"] = _norm_text(parent.get("parent_symbol"))
    item["parent_asset_name"] = _norm_text(parent.get("parent_asset_name"))
    item["parent_asset_type"] = _norm_text(parent.get("parent_asset_type") or "fund")
    item["lookthrough_type"] = _norm_text(parent.get("lookthrough_type") or "PRODUCT_TAG").upper()
    if item["lookthrough_type"] not in LOOKTHROUGH_TYPES:
        item["lookthrough_type"] = "PRODUCT_TAG"
    item["source_type"] = _norm_text(parent.get("source_type") or "ESTIMATED").upper()
    if item["source_type"] not in SOURCE_TYPES:
        item["source_type"] = "ESTIMATED"
    item["confidence"] = _norm_text(parent.get("confidence") or "LOW").upper()
    if item["confidence"] not in CONFIDENCE_RANK:
        item["confidence"] = "LOW"
    item["underlying_weight"] = _float(item.get("underlying_weight"))
    item["underlying_name"] = _norm_text(item.get("underlying_name") or item.get("underlying_symbol") or "未分类")
    item["underlying_symbol"] = _norm_text(item.get("underlying_symbol") or item["underlying_name"])
    item["underlying_region"] = _norm_text(item.get("underlying_region") or "Unknown")
    item["underlying_currency"] = _norm_text(item.get("underlying_currency") or "CNY").upper()
    item["underlying_industry"] = _norm_text(item.get("underlying_industry") or "未分类")
    item["underlying_theme"] = _norm_text(item.get("underlying_theme") or "未分类")
    item["underlying_style"] = _norm_text(item.get("underlying_style") or "未分类")
    item["underlying_risk_bucket"] = _norm_text(item.get("underlying_risk_bucket") or RISK_BUCKET_DEFAULT)
    return item


def load_lookthrough_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load and index the config-backed asset mapping and lookthrough rows."""
    data = raw or _load_yaml_config()
    risk_buckets = {**DEFAULT_RISK_BUCKET_LABELS, **(data.get("risk_buckets") or {})}

    mappings = [_normalize_mapping(r or {}) for r in (data.get("asset_mapping") or [])]
    mapping_by_key: dict[str, dict[str, Any]] = {}
    mapping_candidates_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        aliases = mapping.get("aliases") or []
        lookthrough_aliases = mapping.get("lookthrough_aliases") or []
        keys = _lookup_keys(mapping.get("symbol"), mapping.get("asset_name"), *aliases, *lookthrough_aliases)
        for key in keys:
            mapping_by_key[key] = mapping
            if mapping not in mapping_candidates_by_key[key]:
                mapping_candidates_by_key[key].append(mapping)

    lookthrough_rows: list[dict[str, Any]] = []
    for parent in data.get("asset_lookthrough") or []:
        holdings = parent.get("holdings")
        if holdings:
            lookthrough_rows.extend(_normalize_lookthrough(parent, h or {}) for h in holdings)
        elif parent.get("underlying_name") or parent.get("underlying_symbol"):
            lookthrough_rows.append(_normalize_lookthrough(parent, parent))

    lookthrough_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lookthrough_rows:
        for key in _lookup_keys(row.get("parent_symbol"), row.get("parent_asset_name")):
            lookthrough_by_key[key].append(row)

    return {
        "version": data.get("version"),
        "as_of": _date_str(data.get("as_of")),
        "asset_mapping": mappings,
        "asset_lookthrough": lookthrough_rows,
        "risk_buckets": risk_buckets,
        "mapping_by_key": mapping_by_key,
        "mapping_candidates_by_key": mapping_candidates_by_key,
        "lookthrough_by_key": lookthrough_by_key,
    }


def _find_mapping(position: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    # A literal symbol match is stronger evidence than a generated alias.
    symbol = _key(position.get("symbol"))
    exact = [m for m in cfg.get("asset_mapping", []) if symbol and _key(m.get("symbol")) == symbol]
    distinct_exact = [m for i, m in enumerate(exact) if m not in exact[:i]]
    if len(distinct_exact) == 1:
        return distinct_exact[0]
    if len(distinct_exact) > 1:
        return None
    candidates: list[dict[str, Any]] = []
    index = cfg.get("mapping_candidates_by_key", {})
    for key in _lookup_keys(position.get("symbol"), position.get("asset_id"), position.get("asset_name")):
        for mapping in index.get(key, []):
            if mapping not in candidates:
                candidates.append(mapping)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return None
    return _fallback_mapping(position)


def _find_lookthrough(position: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = _key(position.get("symbol"))
    exact = [row for row in cfg.get("asset_lookthrough", []) if symbol and _key(row.get("parent_symbol")) == symbol]
    if exact:
        return exact
    candidates: list[dict[str, Any]] = []
    seen_rows: set[int] = set()
    for key in _lookup_keys(position.get("symbol"), position.get("asset_id"), position.get("asset_name")):
        for row in cfg["lookthrough_by_key"].get(key, []):
            if id(row) not in seen_rows:
                seen_rows.add(id(row))
                candidates.append(row)
    parents = {_key(row.get("parent_symbol") or row.get("parent_asset_name")) for row in candidates}
    return candidates if len(parents) <= 1 else []


def _fallback_mapping(position: dict[str, Any]) -> dict[str, Any] | None:
    asset_type = _key(position.get("asset_type"))
    name = _norm_text(position.get("asset_name") or position.get("symbol") or "未命名资产")
    currency = _norm_text(position.get("currency") or "CNY").upper()
    if asset_type in {"cash", "money_market", "short_duration_usd", "short_bond"}:
        bucket = "CASH_SHORT_DURATION"
        industry = "现金/短久期"
        theme = "现金管理"
        style = "现金/短久期"
    elif asset_type in {"esop"} or "esop" in _key(name) or "内部" in name:
        bucket = "HUAWEI_ESOP"
        industry = "非上市股权"
        theme = "内部股份"
        style = "非流动/单一公司"
    elif asset_type in {"precious_metal", "gold", "gold_fund"} or "黄金" in name:
        bucket = "GOLD_RESOURCES"
        industry = "贵金属"
        theme = "黄金"
        style = "商品/避险"
    elif asset_type in {"us_stock", "stock_us", "kr_stock", "stock_kr", "eu_stock", "stock_eu", "se_stock", "stock_se", "etf_us"}:
        bucket = "US_GLOBAL_TECH"
        industry = "美股/海外"
        theme = "海外权益"
        style = "海外权益"
    elif asset_type in {"a_share_stock", "a_share_fund", "stock_cn", "fund_cn", "active_fund_cn"}:
        bucket = "CN_EQUITY"
        industry = "中国权益"
        theme = "中国权益"
        style = "中国权益"
    else:
        return None
    fallback_region = {
        "CNY": "China",
        "HKD": "Hong Kong",
        "USD": "United States",
        "KRW": "Korea",
        "SEK": "Sweden",
    }.get(currency, "Unknown")
    return {
        "asset_name": name,
        "symbol": _norm_text(position.get("symbol") or position.get("asset_id") or name),
        "asset_class": asset_type or "other",
        "risk_bucket": bucket,
        "region": fallback_region,
        "currency": currency,
        "industry": industry,
        "theme": theme,
        "style": style,
        "is_tradable": True,
        "is_core": False,
        "mapping_confidence": "LOW",
        "note": "自动兜底分类，需补充 asset_mapping。",
    }


def _is_direct_asset(position: dict[str, Any], mapping: dict[str, Any] | None) -> bool:
    if not mapping:
        return False
    asset_class = _key(mapping.get("asset_class") or position.get("asset_type"))
    return asset_class in {
        "a_share_stock",
        "a_stock",
        "cn_stock",
        "listed_equity_cn",
        "stock_cn",
        "stock_us",
        "stock_kr",
        "kr_stock",
        "stock_eu",
        "eu_stock",
        "stock_se",
        "se_stock",
        "stock_hk",
        "esop",
        "cash",
        "money_market",
        "short_duration_usd",
        "gold",
    }


def _mapped_unit(
    *,
    position: dict[str, Any],
    mapping: dict[str, Any] | None,
    value_cny: float,
    ratio: float,
    lookthrough_type: str,
    confidence: str,
    source_type: str,
    coverage_ratio: float,
    quality_status: str = "",
    note: str = "",
) -> dict[str, Any]:
    asset_name = _norm_text(position.get("asset_name") or position.get("symbol") or "未命名资产")
    symbol = _norm_text(position.get("symbol") or position.get("asset_id") or asset_name)
    common = {
        "holding_name": asset_name,
        "holding_symbol": symbol,
        "holding_asset_class": _norm_text(mapping.get("asset_class") if mapping else position.get("asset_type")),
        "account": _norm_text(position.get("account_name") or position.get("account_code") or "未标注账户"),
        "holding_market_value_cny": value_cny,
        "as_of": _date_str(position.get("as_of")),
        "quality_status": quality_status,
    }
    if mapping:
        risk_bucket = mapping.get("risk_bucket") or RISK_BUCKET_DEFAULT
        return {
            **common,
            "underlying_name": mapping.get("asset_name") or asset_name,
            "underlying_symbol": mapping.get("symbol") or symbol,
            "underlying_weight": ratio,
            "value_cny": value_cny * ratio,
            "underlying_region": mapping.get("region") or "Unknown",
            "underlying_currency": mapping.get("currency") or position.get("currency") or "CNY",
            "underlying_industry": mapping.get("industry") or "未分类",
            "underlying_theme": mapping.get("theme") or "未分类",
            "underlying_style": mapping.get("style") or "未分类",
            "underlying_risk_bucket": risk_bucket,
            "underlying_asset_class": mapping.get("asset_class") or position.get("asset_type") or "",
            "lookthrough_type": lookthrough_type,
            "confidence": confidence,
            "source_type": source_type,
            "coverage_ratio": coverage_ratio,
            "note": note or mapping.get("note") or "",
        }
    return {
        **common,
        "underlying_name": asset_name,
        "underlying_symbol": symbol,
        "underlying_weight": ratio,
        "value_cny": value_cny * ratio,
        "underlying_region": "Unknown",
        "underlying_currency": position.get("currency") or "CNY",
        "underlying_industry": "未映射",
        "underlying_theme": "未映射",
        "underlying_style": "未映射",
        "underlying_risk_bucket": RISK_BUCKET_DEFAULT,
        "underlying_asset_class": position.get("asset_type") or "",
        "lookthrough_type": "MISSING",
        "confidence": "LOW",
        "source_type": "ESTIMATED",
        "coverage_ratio": 0.0,
        "note": note or "缺少 asset_mapping。",
    }


def _staleness_days(asset_as_of: Any, portfolio_as_of: Any) -> int | None:
    left = _parse_date(asset_as_of)
    right = _parse_date(portfolio_as_of)
    if not left or not right:
        return None
    return max((right - left).days, 0)


def _quality_for_position(
    position: dict[str, Any],
    mapping: dict[str, Any] | None,
    lookthrough_rows: list[dict[str, Any]],
    portfolio_as_of: Any,
) -> dict[str, Any]:
    value_cny = _float(position.get("market_value_cny"))
    asset_name = _norm_text(position.get("asset_name") or position.get("symbol") or "未命名资产")
    symbol = _norm_text(position.get("symbol") or position.get("asset_id") or asset_name)
    if lookthrough_rows:
        weight_sum = min(sum(max(_float(r.get("underlying_weight")), 0.0) for r in lookthrough_rows), 1.0)
        ltype = _norm_text(lookthrough_rows[0].get("lookthrough_type") or "TOP10").upper()
        confidence = _aggregate_confidence(r.get("confidence") for r in lookthrough_rows)
        as_of = lookthrough_rows[0].get("as_of")
        stale_days = _staleness_days(as_of, portfolio_as_of)
        is_stale = bool(lookthrough_rows[0].get("is_stale")) or (stale_days is not None and stale_days > 180)
        status = "STALE" if is_stale else QUALITY_TYPES.get(ltype, "PARTIAL")
        note = lookthrough_rows[0].get("note") or "仅覆盖主要成分，剩余权重回落到产品标签。"
        return {
            "asset_name": asset_name,
            "symbol": symbol,
            "market_value_cny": value_cny,
            "lookthrough_type": ltype,
            "coverage_ratio": weight_sum,
            "top10_coverage_ratio": weight_sum if ltype in {"TOP10", "INDEX_PROXY"} else 0.0,
            "as_of": _date_str(as_of),
            "staleness_days": stale_days,
            "confidence": confidence,
            "quality_status": status,
            "current_mapping": f"{ltype} / {(mapping or {}).get('risk_bucket', RISK_BUCKET_DEFAULT)}",
            "reason": _confidence_reason(status, ltype, weight_sum, note),
            "suggested_action": _suggested_action(status, ltype, weight_sum),
            "note": note,
        }
    if mapping and _is_direct_asset(position, mapping):
        return {
            "asset_name": asset_name,
            "symbol": symbol,
            "market_value_cny": value_cny,
            "lookthrough_type": "DIRECT",
            "coverage_ratio": 1.0,
            "top10_coverage_ratio": 0.0,
            "as_of": _date_str(position.get("as_of")),
            "staleness_days": _staleness_days(position.get("as_of"), portfolio_as_of),
            "confidence": mapping.get("mapping_confidence") or "MEDIUM",
            "quality_status": "FULL",
            "current_mapping": f"DIRECT / {mapping.get('risk_bucket', RISK_BUCKET_DEFAULT)}",
            "reason": _confidence_reason("FULL", "DIRECT", 1.0),
            "suggested_action": _suggested_action("FULL", "DIRECT", 1.0),
            "note": "直接持仓按资产映射进入穿透口径。",
        }
    if mapping:
        note = mapping.get("note") or "无底层持仓，按产品标签估算。"
        return {
            "asset_name": asset_name,
            "symbol": symbol,
            "market_value_cny": value_cny,
            "lookthrough_type": "PRODUCT_TAG",
            "coverage_ratio": 0.0,
            "top10_coverage_ratio": 0.0,
            "as_of": _date_str(position.get("as_of")),
            "staleness_days": _staleness_days(position.get("as_of"), portfolio_as_of),
            "confidence": mapping.get("mapping_confidence") or "LOW",
            "quality_status": "PRODUCT_TAG_ONLY",
            "current_mapping": f"PRODUCT_TAG / {mapping.get('risk_bucket', RISK_BUCKET_DEFAULT)}",
            "reason": _confidence_reason("PRODUCT_TAG_ONLY", "PRODUCT_TAG", 0.0, note),
            "suggested_action": _suggested_action("PRODUCT_TAG_ONLY", "PRODUCT_TAG", 0.0),
            "note": note,
        }
    return {
        "asset_name": asset_name,
        "symbol": symbol,
        "market_value_cny": value_cny,
        "lookthrough_type": "MISSING",
        "coverage_ratio": 0.0,
        "top10_coverage_ratio": 0.0,
        "as_of": _date_str(position.get("as_of")),
        "staleness_days": _staleness_days(position.get("as_of"), portfolio_as_of),
        "confidence": "LOW",
        "quality_status": "MISSING",
        "current_mapping": f"MISSING / {RISK_BUCKET_DEFAULT}",
        "reason": _confidence_reason("MISSING", "MISSING", 0.0),
        "suggested_action": _suggested_action("MISSING", "MISSING", 0.0),
        "note": "缺少资产映射，已归入 OTHER。",
    }


def _aggregate_confidence(values: Any) -> str:
    lowest = "HIGH"
    for value in values:
        conf = _norm_text(value or "LOW").upper()
        if CONFIDENCE_RANK.get(conf, 1) < CONFIDENCE_RANK[lowest]:
            lowest = conf if conf in CONFIDENCE_RANK else "LOW"
    return lowest


def _confidence_reason(status: str, lookthrough_type: str, coverage_ratio: float, note: str = "") -> str:
    status = _norm_text(status).upper()
    lookthrough_type = _norm_text(lookthrough_type).upper()
    if status == "FULL":
        return "直接持仓或完整映射，按资产本身进入穿透口径。"
    if status == "STALE":
        return "穿透日期过旧，需要更新后再作为风控判断依据。"
    if status == "MISSING":
        return "缺少资产映射，当前只能归入 OTHER。"
    if status == "PRODUCT_TAG_ONLY":
        return note or "缺少稳定底层持仓，当前仅按产品级标签估算。"
    if status in {"TOP10_ONLY", "INDEX_PROXY"}:
        if coverage_ratio < 0.7:
            return "仅覆盖主要成分，未覆盖部分仍回落到产品标签。"
        return "使用指数代理或前十大持仓，能反映主要暴露但不是完整底层。"
    return note or "穿透口径需要核准。"


def _suggested_action(status: str, lookthrough_type: str, coverage_ratio: float) -> str:
    status = _norm_text(status).upper()
    lookthrough_type = _norm_text(lookthrough_type).upper()
    if status == "MISSING":
        return "补充资产映射"
    if status == "STALE":
        return "更新过期持仓日期"
    if status == "PRODUCT_TAG_ONLY":
        return "补充基金前十大持仓"
    if status in {"TOP10_ONLY", "INDEX_PROXY"}:
        return "补充指数代理行业权重" if coverage_ratio < 0.7 else "定期核准指数代理权重"
    if lookthrough_type == "MANUAL":
        return "核准产品分类"
    return "无需处理"


def _weighted_exposure_confidence(
    *,
    value_cny: float,
    coverage_ratio: float,
    confidence_score: float,
    low_confidence_value_cny: float,
    source_breakdown: dict[str, float],
) -> tuple[str, str]:
    if value_cny <= 0:
        return "LOW", "无有效暴露金额。"
    avg_score = confidence_score / value_cny
    low_ratio = low_confidence_value_cny / value_cny
    product_tag_ratio = _float(source_breakdown.get("PRODUCT_TAG")) / value_cny
    missing_ratio = _float(source_breakdown.get("MISSING")) / value_cny
    weak_ratio = product_tag_ratio + missing_ratio

    if missing_ratio > 0 or coverage_ratio < 0.7 or weak_ratio >= 0.35 or avg_score < 1.7:
        label = "LOW"
    elif avg_score >= 2.65 and low_ratio <= 0.05 and coverage_ratio >= 0.85:
        label = "HIGH"
    else:
        label = "MEDIUM"

    if label == "LOW":
        reason = (
            f"低可信或产品标签来源占比约 {weak_ratio * 100:.1f}%，"
            f"穿透覆盖约 {coverage_ratio * 100:.1f}%。"
        )
    elif label == "HIGH":
        reason = "主要由直接持仓或高可信映射构成，穿透覆盖充足。"
    else:
        reason = (
            f"主要暴露可识别，但仍含约 {low_ratio * 100:.1f}% 低可信或估算来源；"
            f"覆盖约 {coverage_ratio * 100:.1f}%。"
        )
    return label, reason


def _quality_counts(items: list[dict[str, Any]], total_value: float) -> dict[str, Any]:
    buckets = {
        "full_coverage_value_cny": 0.0,
        "top10_only_value_cny": 0.0,
        "top10_total_value_cny": 0.0,
        "proxy_residual_value_cny": 0.0,
        "product_tag_only_value_cny": 0.0,
        "missing_value_cny": 0.0,
        "stale_value_cny": 0.0,
    }
    covered_value = 0.0
    for item in items:
        value = max(_float(item.get("market_value_cny")), 0.0)
        coverage = max(min(_float(item.get("coverage_ratio")), 1.0), 0.0)
        status = item.get("quality_status")
        if status == "FULL":
            covered_part = value * coverage
            buckets["full_coverage_value_cny"] += covered_part
            covered_value += covered_part
        elif status in {"TOP10_ONLY", "INDEX_PROXY"}:
            covered_part = value * coverage
            buckets["top10_total_value_cny"] += value
            buckets["top10_only_value_cny"] += covered_part
            buckets["proxy_residual_value_cny"] += max(value - covered_part, 0.0)
            covered_value += covered_part
        elif status == "STALE":
            buckets["stale_value_cny"] += value
        elif status == "MISSING":
            buckets["missing_value_cny"] += value
        else:
            buckets["product_tag_only_value_cny"] += value
    return {
        "coverage_ratio": covered_value / total_value if total_value > 0 else 0.0,
        "covered_value_cny": covered_value,
        "uncovered_value_cny": max(total_value - covered_value, 0.0),
        **buckets,
    }


def _quality_segments(counts: dict[str, Any], total_value: float) -> list[dict[str, Any]]:
    specs = [
        ("full_coverage", "已穿透/直接持仓", counts.get("full_coverage_value_cny"), "FULL"),
        ("top10_only", "指数/Top10 代理", counts.get("top10_only_value_cny"), "INDEX_PROXY"),
        ("proxy_residual", "代理未覆盖残差", counts.get("proxy_residual_value_cny"), "PRODUCT_TAG_ONLY"),
        ("product_tag_only", "产品标签估算", counts.get("product_tag_only_value_cny"), "PRODUCT_TAG_ONLY"),
        ("missing", "未穿透/缺映射", counts.get("missing_value_cny"), "MISSING"),
        ("stale", "过期穿透数据", counts.get("stale_value_cny"), "STALE"),
    ]
    return [
        {
            "key": key,
            "label": label,
            "value_cny": _float(value),
            "weight": _float(value) / total_value if total_value > 0 else 0.0,
            "quality_status": status,
        }
        for key, label, value, status in specs
    ]


def _low_confidence_assets(items: list[dict[str, Any]], total_value: float) -> list[dict[str, Any]]:
    statuses = {"PRODUCT_TAG_ONLY", "MISSING", "STALE", "TOP10_ONLY", "INDEX_PROXY"}
    out: list[dict[str, Any]] = []
    for item in items:
        status = _norm_text(item.get("quality_status")).upper()
        confidence = _norm_text(item.get("confidence")).upper()
        coverage = _float(item.get("coverage_ratio"))
        if status not in statuses and confidence != "LOW" and coverage >= 0.7:
            continue
        value = _float(item.get("market_value_cny"))
        coverage = max(min(coverage, 1.0), 0.0)
        covered_value = value * coverage
        uncovered_value = max(value - covered_value, 0.0)
        out.append(
            {
                "asset_name": item.get("asset_name"),
                "symbol": item.get("symbol"),
                "market_value_cny": value,
                "covered_value_cny": covered_value,
                "uncovered_value_cny": uncovered_value,
                "weight": value / total_value if total_value > 0 else 0.0,
                "uncovered_weight": uncovered_value / total_value if total_value > 0 else 0.0,
                "current_mapping": item.get("current_mapping"),
                "quality_status": status,
                "lookthrough_type": item.get("lookthrough_type"),
                "coverage_ratio": coverage,
                "confidence": confidence or "LOW",
                "reason": (
                    "映射来自低可信配置或兜底分类，需要核准资产分类。"
                    if status == "FULL" and confidence == "LOW"
                    else item.get("reason") or item.get("note") or "数据待核准。"
                ),
                "suggested_action": (
                    "核准资产分类"
                    if status == "FULL" and confidence == "LOW"
                    else item.get("suggested_action") or _suggested_action(status, item.get("lookthrough_type"), coverage)
                ),
            }
        )
    return sorted(
        out,
        key=lambda r: (
            -abs(_float(r.get("uncovered_value_cny"))),
            -abs(_float(r.get("market_value_cny"))),
        ),
    )


def _units_for_position(
    position: dict[str, Any],
    mapping: dict[str, Any] | None,
    lookthrough_rows: list[dict[str, Any]],
    quality: dict[str, Any],
) -> list[dict[str, Any]]:
    value_cny = _float(position.get("market_value_cny"))
    if value_cny <= 0:
        return []
    if lookthrough_rows:
        units: list[dict[str, Any]] = []
        weight_sum = 0.0
        for row in lookthrough_rows:
            weight = max(_float(row.get("underlying_weight")), 0.0)
            if weight <= 0:
                continue
            weight_sum += weight
            units.append(
                {
                    "holding_name": _norm_text(position.get("asset_name") or position.get("symbol")),
                    "holding_symbol": _norm_text(position.get("symbol") or position.get("asset_id")),
                    "holding_asset_class": _norm_text((mapping or {}).get("asset_class") or position.get("asset_type")),
                    "account": _norm_text(position.get("account_name") or position.get("account_code") or "未标注账户"),
                    "holding_market_value_cny": value_cny,
                    "value_cny": value_cny * weight,
                    "underlying_weight": weight,
                    "coverage_ratio": quality.get("coverage_ratio") or 0.0,
                    "as_of": _date_str(position.get("as_of")),
                    "quality_status": quality.get("quality_status") or "",
                    **{k: row.get(k) for k in [
                        "underlying_name",
                        "underlying_symbol",
                        "underlying_region",
                        "underlying_currency",
                        "underlying_industry",
                        "underlying_theme",
                        "underlying_style",
                        "underlying_risk_bucket",
                        "underlying_asset_class",
                        "lookthrough_type",
                        "confidence",
                        "source_type",
                    ]},
                    "note": row.get("note") or "",
                }
            )
        residual = max(1.0 - weight_sum, 0.0)
        if residual > 0.001:
            units.append(
                _mapped_unit(
                    position=position,
                    mapping=mapping,
                    value_cny=value_cny,
                    ratio=residual,
                    lookthrough_type="PRODUCT_TAG",
                    confidence=mapping.get("mapping_confidence", "LOW") if mapping else "LOW",
                    source_type="ESTIMATED",
                    coverage_ratio=quality.get("coverage_ratio") or 0.0,
                    quality_status=quality.get("quality_status") or "",
                    note="穿透未覆盖的剩余权重，回落到产品标签。",
                )
            )
        return units
    if mapping and _is_direct_asset(position, mapping):
        return [
            _mapped_unit(
                position=position,
                mapping=mapping,
                value_cny=value_cny,
                ratio=1.0,
                lookthrough_type="DIRECT",
                confidence=mapping.get("mapping_confidence", "MEDIUM"),
                source_type="MANUAL",
                coverage_ratio=1.0,
                quality_status=quality.get("quality_status") or "",
            )
        ]
    return [
        _mapped_unit(
            position=position,
            mapping=mapping,
            value_cny=value_cny,
            ratio=1.0,
            lookthrough_type="PRODUCT_TAG" if mapping else "MISSING",
            confidence=mapping.get("mapping_confidence", "LOW") if mapping else "LOW",
            source_type="ESTIMATED",
            coverage_ratio=0.0,
            quality_status=quality.get("quality_status") or "",
        )
    ]


def _apply_industry_buckets(units: list[dict[str, Any]], industry_l1_by_symbol: dict[str, str] | None) -> None:
    for unit in units:
        existing_bucket = _norm_text(unit.get("industry_bucket"))
        if existing_bucket in set(SW_L1_INDUSTRY_BUCKETS.values()) and existing_bucket != INDUSTRY_UNMAPPED:
            unit["industry_bucket_source"] = unit.get("industry_bucket_source") or "asset_master"
            continue
        sw_l1 = _lookup_sw_l1(unit.get("underlying_symbol"), industry_l1_by_symbol)
        unit["sw_l1"] = sw_l1
        if sw_l1:
            unit["industry_bucket"] = _industry_bucket_from_sw_l1(sw_l1)
            unit["industry_bucket_source"] = "sw_l1"
            continue
        bucket = _industry_bucket_from_label(unit.get("underlying_industry"))
        source = "industry_label"
        if bucket == INDUSTRY_UNMAPPED:
            bucket = _industry_bucket_from_risk_bucket(unit.get("underlying_risk_bucket"))
            source = "risk_bucket" if bucket != INDUSTRY_UNMAPPED else "unmapped"
        unit["industry_bucket"] = bucket
        unit["industry_bucket_source"] = source


def _is_cash_like_unit(unit: dict[str, Any]) -> bool:
    risk_bucket = _norm_text(unit.get("underlying_risk_bucket")).upper()
    if risk_bucket in CASH_LIKE_RISK_BUCKETS:
        return True
    for field in ("underlying_asset_class", "holding_asset_class", "parent_asset_type"):
        if _key(unit.get(field)) in CASH_LIKE_ASSET_CLASSES:
            return True
    if _norm_text(unit.get("underlying_industry")) in CASH_LIKE_INDUSTRY_LABELS:
        return True
    if _norm_text(unit.get("underlying_theme")) in CASH_LIKE_THEME_LABELS:
        return True
    return False


def _units_for_dimension(units: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    if dimension not in EXCLUDE_CASH_DIMENSIONS:
        return units
    return [unit for unit in units if not _is_cash_like_unit(unit)]


def _dimension_total_value(units: list[dict[str, Any]], portfolio_total_value: float, dimension: str) -> float:
    if dimension not in EXCLUDE_CASH_DIMENSIONS:
        return portfolio_total_value
    return sum(_float(unit.get("value_cny")) for unit in units)


def _aggregate_dimension(
    units: list[dict[str, Any]],
    total_value: float,
    dimension: str,
    display_labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    field = EXPOSURE_DIMENSIONS[dimension]
    grouped: dict[str, dict[str, Any]] = {}
    for unit in units:
        name = _norm_text(unit.get(field) or "未分类")
        if dimension == "underlying_asset":
            symbol = _norm_text(unit.get("underlying_symbol"))
            name = f"{name} ({symbol})" if symbol and symbol != name else name
        display_name = (display_labels or {}).get(name, name)
        item = grouped.setdefault(
            name,
            {
                "name": name,
                "display_name": display_name,
                "value_cny": 0.0,
                "confidence_values": [],
                "confidence_score": 0.0,
                "low_confidence_value_cny": 0.0,
                "direct_value_cny": 0.0,
                "indirect_value_cny": 0.0,
                "source_breakdown": defaultdict(float),
                "coverage_weighted": 0.0,
                "sources": defaultdict(float),
                "source_details": [],
            },
        )
        value = _float(unit.get("value_cny"))
        lookthrough_type = _norm_text(unit.get("lookthrough_type") or "PRODUCT_TAG").upper()
        confidence = _norm_text(unit.get("confidence") or "LOW").upper()
        if confidence not in CONFIDENCE_RANK:
            confidence = "LOW"
        item["value_cny"] += value
        item["confidence_values"].append(confidence)
        item["confidence_score"] += value * CONFIDENCE_RANK[confidence]
        if confidence == "LOW":
            item["low_confidence_value_cny"] += value
        if lookthrough_type == "DIRECT":
            item["direct_value_cny"] += value
        else:
            item["indirect_value_cny"] += value
        item["source_breakdown"][lookthrough_type] += value
        item["coverage_weighted"] += value * _float(unit.get("coverage_ratio"))
        source_label = _norm_text(unit.get("holding_name") or unit.get("holding_symbol") or "unknown")
        item["sources"][source_label] += value
        item["source_details"].append(
            {
                "source_asset_name": source_label,
                "source_symbol": _norm_text(unit.get("holding_symbol")),
                "holding_asset_class": _norm_text(unit.get("holding_asset_class")),
                "underlying_name": _norm_text(unit.get("underlying_name")),
                "underlying_symbol": _norm_text(unit.get("underlying_symbol")),
                "underlying_asset_class": _norm_text(unit.get("underlying_asset_class")),
                "underlying_industry": _norm_text(unit.get("underlying_industry")),
                "sw_l1": _norm_text(unit.get("sw_l1")),
                "industry_bucket": _norm_text(unit.get("industry_bucket")),
                "industry_bucket_source": _norm_text(unit.get("industry_bucket_source")),
                "account": _norm_text(unit.get("account") or "未标注账户"),
                "market_value_cny": _float(unit.get("holding_market_value_cny")),
                "contribution_to_bucket_cny": value,
                "lookthrough_type": lookthrough_type,
                "coverage_ratio": _float(unit.get("coverage_ratio")),
                "confidence": confidence,
                "as_of": _date_str(unit.get("as_of")),
                "note": _norm_text(unit.get("note")),
            }
        )

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        value = item["value_cny"]
        source_breakdown = dict(sorted(item["source_breakdown"].items(), key=lambda kv: -kv[1]))
        coverage = item["coverage_weighted"] / value if value > 0 else 0.0
        confidence, confidence_reason = _weighted_exposure_confidence(
            value_cny=value,
            coverage_ratio=coverage,
            confidence_score=item["confidence_score"],
            low_confidence_value_cny=item["low_confidence_value_cny"],
            source_breakdown=source_breakdown,
        )
        source_names = [
            {"name": name, "value_cny": amount, "weight": amount / total_value if total_value > 0 else 0.0}
            for name, amount in sorted(item["sources"].items(), key=lambda kv: -kv[1])[:5]
        ]
        source_details = _merge_source_details(
            item["source_details"],
            aggregate_underlyings=dimension != "underlying_asset",
        )[:30]
        rows.append(
            {
                "name": item["name"],
                "display_name": item["display_name"],
                "value_cny": value,
                "total_exposure_cny": value,
                "direct_value_cny": item["direct_value_cny"],
                "indirect_value_cny": item["indirect_value_cny"],
                "weight": value / total_value if total_value > 0 else 0.0,
                "total_weight": value / total_value if total_value > 0 else 0.0,
                "confidence": confidence,
                "confidence_reason": confidence_reason,
                "low_confidence_weight": item["low_confidence_value_cny"] / value if value > 0 else 0.0,
                "source_breakdown": source_breakdown,
                "lookthrough_coverage": coverage,
                "main_sources": source_names,
                "top_sources": source_names,
                "source_count": len(item["sources"]),
                "source_details": source_details,
                "risk_eligible": confidence != "LOW" and coverage >= 0.7,
                "risk_gate_reason": "" if confidence != "LOW" and coverage >= 0.7 else "可信度低或穿透覆盖不足，仅进入观察/数据核准，不直接升级为 P1 风控。",
            }
        )
    return sorted(rows, key=lambda r: abs(r["value_cny"]), reverse=True)


def _merge_source_details(details: list[dict[str, Any]], *, aggregate_underlyings: bool) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for detail in details:
        source_name = _norm_text(detail.get("source_asset_name"))
        account = _norm_text(detail.get("account") or "未标注账户")
        lookthrough_type = _norm_text(detail.get("lookthrough_type") or "PRODUCT_TAG").upper()
        confidence = _norm_text(detail.get("confidence") or "LOW").upper()
        underlying_name = _norm_text(detail.get("underlying_name"))
        underlying_symbol = _norm_text(detail.get("underlying_symbol"))
        key = (source_name, account, lookthrough_type, confidence) if aggregate_underlyings else (
            source_name,
            account,
            lookthrough_type,
            confidence,
            underlying_name,
        )
        item = grouped.setdefault(
            key,
            {
                **detail,
                "contribution_to_bucket_cny": 0.0,
                "coverage_weighted": 0.0,
                "underlyings": {},
                "notes": [],
            },
        )
        contribution = _float(detail.get("contribution_to_bucket_cny"))
        item["contribution_to_bucket_cny"] += contribution
        item["coverage_weighted"] += contribution * _float(detail.get("coverage_ratio"))
        if underlying_name:
            label = f"{underlying_name} ({underlying_symbol})" if underlying_symbol and underlying_symbol != underlying_name else underlying_name
            item["underlyings"][label] = item["underlyings"].get(label, 0.0) + contribution
        note = _norm_text(detail.get("note"))
        if note and note not in item["notes"]:
            item["notes"].append(note)

    out: list[dict[str, Any]] = []
    for item in grouped.values():
        contribution = _float(item.get("contribution_to_bucket_cny"))
        underlyings = sorted(item.pop("underlyings").items(), key=lambda kv: -abs(kv[1]))
        notes = item.pop("notes")
        item["coverage_ratio"] = item.pop("coverage_weighted") / contribution if contribution > 0 else 0.0
        item["underlying_count"] = len(underlyings)
        item["underlying_names"] = [name for name, _ in underlyings[:6]]
        item["note"] = " / ".join(notes[:2])
        out.append(item)
    return sorted(out, key=lambda r: -abs(_float(r.get("contribution_to_bucket_cny"))))


def derive_lookthrough_payload(
    positions: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    *,
    as_of: Any | None = None,
    industry_l1_by_symbol: dict[str, str] | None = None,
    asset_master_cur: Any | None = None,
    sync_asset_master: bool = False,
    portfolio_nav_cny: float | Decimal | None = None,
) -> dict[str, Any]:
    """Derive lookthrough exposures from normalized investment positions."""
    cfg = load_lookthrough_config(config)
    portfolio_as_of = as_of or max((_date_str(p.get("as_of")) for p in positions if p.get("as_of")), default=None)
    positive_positions = [p for p in positions if _float(p.get("market_value_cny")) > 0]
    total_value = sum(_float(p.get("market_value_cny")) for p in positive_positions)
    nav_value = _float(portfolio_nav_cny) if portfolio_nav_cny is not None else total_value
    units: list[dict[str, Any]] = []
    quality_items: list[dict[str, Any]] = []
    warnings: list[str] = []

    for position in positive_positions:
        mapping = _find_mapping(position, cfg)
        if mapping and mapping.get("asset_class"):
            position["raw_asset_type"] = position.get("asset_type")
            position["asset_type"] = mapping.get("asset_class")
            position["mapped_asset_class"] = mapping.get("asset_class")
            position["asset_class"] = mapping.get("asset_class")
        lookthrough_rows = _find_lookthrough(position, cfg)
        quality = _quality_for_position(position, mapping, lookthrough_rows, portfolio_as_of)
        quality_items.append(quality)
        if quality["quality_status"] == "MISSING":
            warnings.append(f"{quality['asset_name']} 缺少资产映射，已归入 OTHER。")
        elif quality["quality_status"] == "PRODUCT_TAG_ONLY":
            warnings.append(f"{quality['asset_name']} 缺少底层持仓，当前仅按产品标签估算。")
        elif quality["quality_status"] == "STALE":
            warnings.append(f"{quality['asset_name']} 穿透数据过旧，需要核准。")
        units.extend(_units_for_position(position, mapping, lookthrough_rows, quality))

    master_payload: dict[str, Any] = {"status": "not_configured", "model_version": "asset_master_v1"}
    if asset_master_cur is not None:
        if sync_asset_master:
            master_lookup = sync_asset_master_from_analysis(
                asset_master_cur,
                positions=positive_positions,
                units=units,
                config=cfg,
            )
        else:
            master_lookup = load_asset_master_lookup(asset_master_cur, ensure_schema=False)
        positive_positions = enrich_positions_with_asset_master(positive_positions, master_lookup)
        units = enrich_units_with_asset_master(units, master_lookup)
        master_payload = build_asset_master_payload(
            positions=positive_positions,
            units=units,
            lookup=master_lookup,
        )

    _apply_industry_buckets(units, industry_l1_by_symbol)

    exposures = {}
    for dimension in EXPOSURE_DIMENSIONS:
        dimension_units = _units_for_dimension(units, dimension)
        dimension_total_value = _dimension_total_value(dimension_units, total_value, dimension)
        exposures[dimension] = _aggregate_dimension(
            dimension_units,
            dimension_total_value,
            dimension,
            cfg.get("risk_buckets") if dimension == "risk_bucket" else None,
        )
    quality_counts = _quality_counts(quality_items, total_value)
    for item in quality_items:
        value = _float(item.get("market_value_cny"))
        item["weight"] = value / total_value if total_value > 0 else 0.0
    sorted_quality_items = sorted(quality_items, key=lambda r: -abs(_float(r.get("market_value_cny"))))
    quality_payload = {
        **quality_counts,
        "segments": _quality_segments(quality_counts, total_value),
        "low_confidence_assets": _low_confidence_assets(sorted_quality_items, total_value),
        "items": sorted_quality_items,
    }
    engine_payload = build_exposure_analysis_payload(
        positions=positive_positions,
        units=units,
        quality=quality_payload,
        total_value=total_value,
    )
    exposures.update(engine_payload["exposures"])

    return {
        "status": "ok" if positions else "empty",
        "as_of": _date_str(portfolio_as_of),
        "portfolio_nav_cny": nav_value,
        "gross_exposure_cny": total_value,
        "total_market_value_cny": total_value,
        "exposures": exposures,
        "quality": quality_payload,
        "normalized_holdings": engine_payload["normalized_holdings"],
        "underlying_holdings": engine_payload["underlying_holdings"],
        "risk_summary": engine_payload["risk_summary"],
        "validation": engine_payload["validation"],
        "master_data": master_payload,
        "model_version": engine_payload["model_version"],
        "data_warnings": warnings,
    }


def build_lookthrough_payload(
    cur,
    *,
    base_currency: str = "CNY",
    sync_asset_master: bool = False,
) -> dict[str, Any]:
    """Build the /api/analysis/lookthrough payload from current PAMS holdings."""
    account_cfg = _load_accounts_config()
    account_balances = load_account_balances(cur, base_currency, account_cfg)
    portfolio_nav_cny = sum(
        (_dec(row.get("amount_cny")) for row in account_balances if row.get("include_in_nav")),
        Decimal("0"),
    )
    positions = load_investment_positions(cur, base_currency, account_cfg)
    industry_l1_by_symbol = _load_sw_l1_lookup(cur)
    return derive_lookthrough_payload(
        positions,
        industry_l1_by_symbol=industry_l1_by_symbol,
        asset_master_cur=cur,
        sync_asset_master=sync_asset_master,
        portfolio_nav_cny=portfolio_nav_cny,
    )
