"""Unified holding exposure model and calculators.

The engine deliberately keeps asset type, industry, theme, market, region,
currency, and underlying exposure separate.  UI modules should consume these
results instead of re-classifying holdings locally.
"""

from __future__ import annotations

import os
import re
import warnings
from collections import defaultdict
from decimal import Decimal
from typing import Any, Literal, TypedDict


AssetType = Literal[
    "A股个股",
    "港股个股",
    "美股个股",
    "欧股个股",
    "韩股个股",
    "ETF / 基金",
    "现金",
    "债券 / 货币基金",
    "黄金 / 商品",
    "非上市股权",
    "房地产 / 实物资产",
    "应收款 / 待到账",
    "其他资产",
]

IndustryBucket = Literal[
    "金融地产",
    "大消费",
    "科技TMT",
    "高端制造",
    "周期资源",
    "医药医疗",
    "稳定公用",
    "未穿透 / 未映射",
]

ThemeFamily = Literal["科技成长", "资源通胀", "制造升级", "中国资产", "医药健康", "未映射主题"]
ThemeScore = Literal[1.0, 0.8, 0.7, 0.5, 0.3, 0.2]


class ThemeExposure(TypedDict):
    themeId: str
    themeName: str
    family: ThemeFamily
    score: float
    role: Literal["core", "strong", "related", "weak"]
    source: Literal["manual", "rule", "fund_lookthrough", "fallback"]
    confidence: Literal["high", "medium", "low"]


class HoldingNormalized(TypedDict, total=False):
    id: str
    symbol: str
    name: str
    market: Literal["A股", "港股", "美股", "欧股", "韩股", "基金", "现金", "货币基金 / 债券", "黄金 / 商品", "非上市 / 内部", "其他"]
    region: Literal["中国大陆", "香港", "美国", "欧洲", "瑞典", "韩国", "全球", "其他"]
    currency: Literal["CNY", "HKD", "USD", "SEK", "KRW", "MXN", "其他"]
    assetType: AssetType
    assetSubtype: str
    marketValue: float
    portfolioWeight: float
    industryBucket: IndustryBucket | None
    industryLeaf: str | None
    industrySubBucket: str | None
    themeExposures: list[ThemeExposure]
    aiSubThemes: list[dict[str, Any]]
    indexMembershipTags: list[str]
    rawTags: list[str]
    lookthroughStatus: Literal["direct", "lookthrough_available", "lookthrough_missing", "not_applicable"]
    account: str
    sw_l1: str
    industryBucketSource: str


class UnderlyingHolding(TypedDict, total=False):
    parentHoldingId: str
    parentName: str
    underlyingSymbol: str
    underlyingName: str
    underlyingWeightInFund: float
    effectiveWeightInPortfolio: float
    effectiveMarketValue: float
    assetType: AssetType
    industryBucket: IndustryBucket
    industryLeaf: str | None
    industrySubBucket: str | None
    themeExposures: list[ThemeExposure]
    aiSubThemes: list[dict[str, Any]]
    sw_l1: str
    industryBucketSource: str
    market: str
    region: str
    currency: str
    account: str
    lookthroughType: str
    confidence: str


ASSET_TYPES: tuple[AssetType, ...] = (
    "A股个股",
    "港股个股",
    "美股个股",
    "欧股个股",
    "韩股个股",
    "ETF / 基金",
    "现金",
    "债券 / 货币基金",
    "黄金 / 商品",
    "非上市股权",
    "房地产 / 实物资产",
    "应收款 / 待到账",
    "其他资产",
)

INDUSTRY_BUCKETS: tuple[IndustryBucket, ...] = (
    "金融地产",
    "大消费",
    "科技TMT",
    "高端制造",
    "周期资源",
    "医药医疗",
    "稳定公用",
    "未穿透 / 未映射",
)

UNMAPPED_INDUSTRY: IndustryBucket = "未穿透 / 未映射"
UNMAPPED_THEME_NAME = "未映射主题"

SW_L1_TO_BUCKET: dict[str, IndustryBucket] = {
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
    "轻工制造": "大消费",
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

FORBIDDEN_INDUSTRY_LABELS = {
    "非上市股权",
    "ETF / 基金",
    "现金",
    "债券 / 货币基金",
    "Nasdaq-100 constituent",
    "S&P 500 constituent",
    "贵金属与铜",
    "黄金避险",
    "AI / 算力",
    "半导体",
    "互联网平台",
    "高股息 / 红利",
    "央国企 / 中特估",
    "美元资产",
    "中国资产",
    "美国资产",
    "中概股",
    "出海链",
    "A股个股",
    "美股个股",
    "欧股个股",
    "韩股个股",
}
FORBIDDEN_INDUSTRY_LABELS.discard("半导体")
FORBIDDEN_INDUSTRY_LABELS.discard("互联网平台")

FORBIDDEN_THEME_LABELS = {
    "现金",
    "ETF / 基金",
    "债券 / 货币基金",
    "非上市股权",
    "A股个股",
    "美股个股",
    "欧股个股",
    "韩股个股",
    "人民币资产",
    "美元资产",
    "中国资产",
    "美国资产",
    "Nasdaq-100 constituent",
    "S&P 500 constituent",
    "沪深300成分",
    "中证500成分",
}

THEME_DEFINITIONS: dict[str, tuple[str, ThemeFamily]] = {
    "ai_compute": ("AI / 算力", "科技成长"),
    "semiconductor": ("半导体", "科技成长"),
    "cloud_software": ("云计算 / 软件", "科技成长"),
    "internet_platform": ("互联网平台", "科技成长"),
    "telecom_infra": ("通信基础设施", "科技成长"),
    "precious_copper": ("贵金属与铜", "资源通胀"),
    "gold_hedge": ("黄金避险", "资源通胀"),
    "energy_oil_gas": ("能源 / 油气", "资源通胀"),
    "rare_earth_minor_metals": ("稀土 / 小金属", "资源通胀"),
    "new_energy_vehicle": ("新能源车", "制造升级"),
    "pv_storage": ("光伏 / 储能", "制造升级"),
    "high_end_equipment": ("高端装备 / 自动化", "制造升级"),
    "defense_aerospace": ("军工 / 航空航天", "制造升级"),
    "export_chain": ("出海链", "制造升级"),
    "high_dividend": ("高股息 / 红利", "中国资产"),
    "central_soe": ("央国企 / 中特估", "中国资产"),
    "financial_procycle": ("金融顺周期", "中国资产"),
    "consumer_core": ("消费核心资产", "中国资产"),
    "property_chain": ("地产链修复", "中国资产"),
    "innovative_drug": ("创新药", "医药健康"),
    "glp1": ("GLP-1 / 减重药", "医药健康"),
    "medical_device_service": ("医疗器械 / 服务", "医药健康"),
    "unmapped_theme": (UNMAPPED_THEME_NAME, "未映射主题"),
}

THEME_NAME_TO_ID = {name: theme_id for theme_id, (name, _family) in THEME_DEFINITIONS.items()}

AI_SUB_THEME_DEFINITIONS = {
    "gpu_accelerator": "GPU / AI 加速器",
    "cpu_server": "CPU / 服务器平台",
    "memory_hbm": "存储 / HBM",
    "optical_cpo": "光模块 / CPO",
    "network_switch": "网络 / 交换芯片",
    "pcb_connector": "PCB / 连接器",
    "power_cooling": "电力 / 散热 / 液冷",
    "edge_ai_pc": "端侧 AI / AI PC",
}

SYMBOL_ALIASES: dict[str, str] = {
    "GOOG": "GOOGL",
    "GOOGL": "GOOGL",
    "3690": "3690",
    "3690.HK": "3690",
    "005930.KS": "005930.KS",
    "005930": "005930.KS",
    "000660.KS": "000660.KS",
    "000660": "000660.KS",
}

CHINA_ECONOMIC_EXPOSURE_SYMBOLS = {
    "3690",
    "700",
    "9988",
    "9888",
    "9618",
    "1024",
    "1810",
    "9868",
    "1211",
    "2015",
    "2269",
    "992",
    "981",
}

INDUSTRY_SYMBOL_OVERRIDES: dict[str, IndustryBucket] = {
    "NVDA": "科技TMT",
    "AMD": "科技TMT",
    "MU": "科技TMT",
    "MRVL": "科技TMT",
    "AVGO": "科技TMT",
    "ANET": "科技TMT",
    "MSFT": "科技TMT",
    "GOOGL": "科技TMT",
    "GOOG": "科技TMT",
    "AAPL": "科技TMT",
    "PLTR": "科技TMT",
    "005930.KS": "科技TMT",
    "000660.KS": "科技TMT",
    "AMZN": "大消费",
    "3690": "大消费",
    "WMT": "大消费",
    "LIN": "周期资源",
    "TMUS": "科技TMT",
    "ADI": "科技TMT",
    "SNDK": "科技TMT",
    "STX": "科技TMT",
    "SHOP": "科技TMT",
    "PANW": "科技TMT",
    "WDC": "科技TMT",
    "APP": "科技TMT",
    "HON": "高端制造",
    "CRWD": "科技TMT",
    "CEG": "稳定公用",
    "CMCSA": "科技TMT",
    "981": "科技TMT",
    "CDNS": "科技TMT",
    "MAR": "大消费",
    "SNPS": "科技TMT",
    "CSX": "稳定公用",
    "ORLY": "大消费",
    "MDLZ": "大消费",
    "MPWR": "科技TMT",
    "AEP": "稳定公用",
    "MNST": "大消费",
    "NXPI": "科技TMT",
    "ROST": "大消费",
    "DASH": "大消费",
    "BKR": "周期资源",
    "CTAS": "稳定公用",
    "PDD": "大消费",
    "WBD": "科技TMT",
    "FTNT": "科技TMT",
    "ABNB": "大消费",
    "PCAR": "高端制造",
    "FANG": "周期资源",
    "MSTR": "科技TMT",
    "ADSK": "科技TMT",
    "FAST": "高端制造",
    "MCHP": "科技TMT",
    "XEL": "稳定公用",
    "EA": "科技TMT",
    "FER": "稳定公用",
    "EXC": "稳定公用",
    "DDOG": "科技TMT",
    "IDXX": "医药医疗",
    "PYPL": "金融地产",
    "CCEP": "大消费",
    "ODFL": "稳定公用",
    "TRI": "科技TMT",
    "ALNY": "医药医疗",
    "KDP": "大消费",
    "TTWO": "科技TMT",
    "ROP": "高端制造",
    "AXON": "高端制造",
    "CPRT": "大消费",
    "PAYX": "科技TMT",
    "ZS": "科技TMT",
    "ARM": "科技TMT",
    "INSM": "医药医疗",
    "GEHC": "医药医疗",
    "KHC": "大消费",
    "WDAY": "科技TMT",
    "CTSH": "科技TMT",
    "VRSK": "科技TMT",
    "DXCM": "医药医疗",
    "CHTR": "科技TMT",
    "CSGP": "金融地产",
    "BYD": "高端制造",
    "002594": "高端制造",
    "1211": "高端制造",
    "601899": "周期资源",
    "000630": "周期资源",
    "600036": "金融地产",
    "招商银行": "金融地产",
    "0941": "科技TMT",
    "600941": "科技TMT",
    "中国移动": "科技TMT",
}

GICS_SECTOR_TO_BUCKET: dict[str, IndustryBucket] = {
    "energy": "周期资源",
    "materials": "周期资源",
    "consumer staples": "大消费",
    "health care": "医药医疗",
    "financials": "金融地产",
    "information technology": "科技TMT",
    "communication services": "科技TMT",
    "utilities": "稳定公用",
    "real estate": "金融地产",
}

INDUSTRY_KEYWORD_BUCKETS: list[tuple[IndustryBucket, tuple[str, ...]]] = [
    ("金融地产", ("bank", "financial", "insurance", "real estate", "broker", "securities", "银行", "证券", "保险", "房地产", "地产", "多元金融", "券商", "信托", "金融")),
    ("大消费", ("consumer", "retail", "restaurant", "food", "beverage", "travel", "apparel", "luxury", "household", "食品", "白酒", "啤酒", "软饮料", "乳制品", "零售", "消费", "旅游", "服饰", "家居", "家电", "饲料", "农业", "养殖", "农牧", "商品城", "文教", "包装")),
    ("科技TMT", ("software", "semiconductor", "internet", "cloud", "media", "streaming", "communications", "electronics", "technology", "软件", "半导体", "互联网", "云", "通信", "电子", "传媒", "光通信", "科技", "元器件", "电信运营", "影视", "音像", "it设备", "IT设备", "计算机")),
    ("高端制造", ("automobile", "automotive", "machinery", "equipment", "aerospace", "defense", "manufacturing", "capital goods", "electrical equipment", "汽车", "机械", "设备", "军工", "航空", "电力设备", "电气设备", "电器仪表", "工程机械", "专用机械", "机械基件", "汽车整车", "汽车配件", "船舶", "运输设备")),
    ("周期资源", ("metal", "mining", "coal", "oil", "chemical", "steel", "material", "gold", "copper", "energy", "有色", "金属", "煤炭", "石油", "化工", "钢铁", "黄金", "铜", "稀土", "化纤", "农药化肥", "普钢", "特种钢", "水泥", "玻璃", "建材", "矿物制品", "铝", "造纸")),
    ("医药医疗", ("biotech", "pharma", "medical", "health", "drug", "life science", "医药", "医疗", "生物", "器械", "化学制药", "中成药", "生物制药", "医疗保健", "医药商业", "创新药")),
    ("稳定公用", ("utility", "utilities", "transport", "infrastructure", "rail", "airport", "shipping", "logistics", "公用", "交通", "环保", "建筑装饰", "航运", "铁路", "机场", "物流", "火力发电", "水力发电", "新型电力", "供气供热", "电力", "港口", "水运", "空运", "路桥", "仓储", "建筑工程")),
]

THEME_SYMBOL_RULES: dict[str, list[tuple[str, float]]] = {
    "NVDA": [("ai_compute", 1.0), ("semiconductor", 1.0)],
    "AMD": [("ai_compute", 0.8), ("semiconductor", 1.0)],
    "MU": [("ai_compute", 1.0), ("semiconductor", 1.0)],
    "MRVL": [("ai_compute", 0.8), ("semiconductor", 1.0)],
    "AVGO": [("ai_compute", 1.0), ("semiconductor", 1.0)],
    "ANET": [("ai_compute", 1.0), ("telecom_infra", 0.7)],
    "MSFT": [("ai_compute", 0.8), ("cloud_software", 1.0)],
    "GOOGL": [("ai_compute", 0.8), ("internet_platform", 1.0), ("cloud_software", 0.7)],
    "AAPL": [("ai_compute", 0.5)],
    "AMZN": [("internet_platform", 1.0), ("cloud_software", 1.0), ("ai_compute", 0.7)],
    "PLTR": [("ai_compute", 0.8), ("cloud_software", 0.8)],
    "005930.KS": [("ai_compute", 0.7), ("semiconductor", 1.0)],
    "000660.KS": [("ai_compute", 0.8), ("semiconductor", 1.0)],
    "601899": [("precious_copper", 1.0), ("gold_hedge", 0.8)],
    "000630": [("precious_copper", 1.0)],
    "002594": [("new_energy_vehicle", 1.0), ("export_chain", 0.8)],
    "1211": [("new_energy_vehicle", 1.0), ("export_chain", 0.8)],
    "600036": [("financial_procycle", 1.0), ("high_dividend", 0.8)],
    "0941": [("high_dividend", 1.0), ("central_soe", 1.0), ("telecom_infra", 1.0)],
    "600941": [("high_dividend", 1.0), ("central_soe", 1.0), ("telecom_infra", 1.0)],
    "3690": [("internet_platform", 1.0), ("consumer_core", 0.5)],
}

THEME_NAME_KEYWORD_RULES: list[tuple[tuple[str, ...], list[tuple[str, float]]]] = [
    (("紫金", "gold", "黄金"), [("precious_copper", 1.0), ("gold_hedge", 0.8)]),
    (("铜陵", "铜", "copper"), [("precious_copper", 1.0)]),
    (("比亚迪", "byd"), [("new_energy_vehicle", 1.0), ("export_chain", 0.8)]),
    (("招商银行", "cmb"), [("financial_procycle", 1.0), ("high_dividend", 0.8)]),
    (("中国移动", "china mobile"), [("high_dividend", 1.0), ("central_soe", 1.0), ("telecom_infra", 1.0)]),
    (("半导体", "semiconductor"), [("semiconductor", 1.0)]),
    (("ai", "算力", "服务器"), [("ai_compute", 0.8)]),
    (("云", "software", "软件"), [("cloud_software", 0.8)]),
    (("互联网", "platform"), [("internet_platform", 0.8)]),
    (("稀土",), [("rare_earth_minor_metals", 1.0)]),
    (("红利", "高股息"), [("high_dividend", 1.0)]),
    (("央企", "国企", "中特估"), [("central_soe", 1.0)]),
]

AI_SUB_THEME_RULES: dict[str, list[tuple[str, float]]] = {
    "NVDA": [("gpu_accelerator", 1.0), ("network_switch", 0.5)],
    "AMD": [("gpu_accelerator", 0.8), ("cpu_server", 0.8), ("edge_ai_pc", 0.5)],
    "MU": [("memory_hbm", 1.0)],
    "MRVL": [("network_switch", 0.8), ("optical_cpo", 0.7), ("gpu_accelerator", 0.3)],
    "AVGO": [("network_switch", 1.0), ("gpu_accelerator", 0.5)],
    "ANET": [("network_switch", 1.0)],
    "SMCI": [("cpu_server", 1.0)],
    "DELL": [("cpu_server", 1.0)],
    "601138": [("cpu_server", 1.0)],
    "000977": [("cpu_server", 1.0)],
    "300308": [("optical_cpo", 1.0)],
    "300502": [("optical_cpo", 1.0)],
    "300394": [("optical_cpo", 1.0)],
    "COHR": [("optical_cpo", 1.0)],
    "LITE": [("optical_cpo", 1.0)],
    "002463": [("pcb_connector", 1.0)],
    "300476": [("pcb_connector", 1.0)],
    "002916": [("pcb_connector", 1.0)],
    "VRT": [("power_cooling", 1.0)],
    "002837": [("power_cooling", 1.0)],
    "002335": [("power_cooling", 1.0)],
    "QCOM": [("edge_ai_pc", 0.8)],
    "AAPL": [("edge_ai_pc", 0.7)],
    "INTC": [("edge_ai_pc", 0.5), ("cpu_server", 0.5)],
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return _text(value).casefold()


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _canonical_symbol(symbol: Any, name: Any = "") -> str:
    raw = _text(symbol).upper()
    if raw.endswith(".SH") or raw.endswith(".SZ"):
        raw = raw[:6]
    if raw.endswith(".OF") or raw.endswith(".SS"):
        raw = raw[:6]
    if raw.endswith(".HK"):
        raw = raw[:-3].lstrip("0") or raw[:-3]
    raw = SYMBOL_ALIASES.get(raw, raw)
    if raw:
        return raw
    name_text = _text(name)
    for alias, canonical in SYMBOL_ALIASES.items():
        if alias and alias.casefold() in name_text.casefold():
            return canonical
    return name_text


def _identity_keys(*values: Any) -> set[str]:
    keys: set[str] = set()
    for value in values:
        text = _text(value)
        if not text:
            continue
        keys.add(text.casefold())
        keys.add(text.upper())
        canonical = _canonical_symbol(text, text)
        if canonical:
            keys.add(canonical.casefold())
            keys.add(canonical.upper())
    return {key for key in keys if key}


def _position_identity_keys(position: dict[str, Any]) -> set[str]:
    return _identity_keys(
        position.get("id"),
        position.get("asset_id"),
        position.get("symbol"),
        position.get("asset_name"),
        position.get("name"),
    )


def _unit_parent_identity_keys(unit: dict[str, Any]) -> set[str]:
    return _identity_keys(unit.get("holding_symbol"), unit.get("holding_name"))


def _is_real_lookthrough_type(value: Any) -> bool:
    return _text(value).upper() not in {"", "DIRECT", "PRODUCT_TAG", "MISSING"}


def _lookthrough_source_label(value: Any) -> str:
    lookthrough_type = _text(value).upper()
    if lookthrough_type == "PRODUCT_TAG":
        return "产品标签/残余"
    if lookthrough_type == "MISSING":
        return "未穿透"
    if lookthrough_type == "DIRECT":
        return "直接持仓"
    return "基金穿透"


def _role(score: float) -> Literal["core", "strong", "related", "weak"]:
    if score >= 1.0:
        return "core"
    if score >= 0.8:
        return "strong"
    if score >= 0.5:
        return "related"
    return "weak"


def _currency(value: Any) -> Literal["CNY", "HKD", "USD", "SEK", "KRW", "MXN", "其他"]:
    cur = _text(value).upper()
    return cur if cur in {"CNY", "HKD", "USD", "SEK", "KRW", "MXN"} else "其他"


def _symbol_digits(value: Any) -> str:
    return re.sub(r"\D", "", _text(value))


def _is_cn_equity_symbol(value: Any) -> bool:
    raw = _text(value).upper()
    if raw.endswith(".SH") or raw.endswith(".SZ") or raw.startswith(("SH", "SZ")):
        digits = _symbol_digits(raw)
    else:
        digits = _symbol_digits(_canonical_symbol(raw))
    if len(digits) != 6:
        return False
    return digits.startswith((
        "000",
        "001",
        "002",
        "003",
        "300",
        "301",
        "600",
        "601",
        "603",
        "605",
        "688",
        "689",
        "430",
        "830",
        "831",
        "832",
        "833",
        "834",
        "835",
        "836",
        "837",
        "838",
        "839",
        "870",
        "871",
        "872",
        "873",
        "874",
        "875",
        "876",
        "877",
        "878",
        "879",
    ))


def _is_hk_symbol(value: Any) -> bool:
    raw = _text(value).upper()
    return raw.endswith(".HK") or raw.startswith("HK")


def _is_kr_symbol(value: Any) -> bool:
    raw = _text(value).upper()
    return raw.endswith(".KS") or raw.endswith(".KQ")


def _infer_stock_asset_class(symbol: Any, currency: Any = "", region: Any = "") -> str:
    cur = _text(currency).upper()
    region_key = _key(region)
    if _is_hk_symbol(symbol) or cur == "HKD" or region_key in {"hong kong", "hk", "香港"}:
        return "stock_hk"
    if _is_kr_symbol(symbol) or cur == "KRW" or region_key in {"korea", "south korea", "kr", "韩国"}:
        return "stock_kr"
    if cur == "SEK" or region_key in {"europe", "eu", "欧洲", "sweden", "se", "sverige", "瑞典"}:
        return "stock_eu"
    if _is_cn_equity_symbol(symbol) or (cur == "CNY" and len(_symbol_digits(symbol)) == 6):
        return "stock_cn"
    if cur == "USD" or region_key in {"united states", "us", "usa", "美国"}:
        return "stock_us"
    return ""


def resolveAssetType(holding: dict[str, Any]) -> AssetType:
    """Resolve the orthogonal asset type dimension."""
    asset_type_raw = _text(holding.get("asset_type") or holding.get("asset_class") or holding.get("underlying_asset_class"))
    if asset_type_raw in ASSET_TYPES:
        return asset_type_raw  # type: ignore[return-value]
    asset_type = _key(holding.get("asset_type") or holding.get("asset_class") or holding.get("underlying_asset_class"))
    name = _key(holding.get("asset_name") or holding.get("name") or holding.get("underlying_name"))
    symbol = _canonical_symbol(holding.get("symbol") or holding.get("underlying_symbol"), name)
    currency = _text(holding.get("currency") or holding.get("underlying_currency")).upper()
    raw_symbol = holding.get("symbol") or holding.get("underlying_symbol")
    stock_hint = (
        asset_type in {"stock_hk", "hk_stock", "stock_us", "us_stock", "stock_cn", "a_share_stock", "cn_stock", "stock_kr", "kr_stock", "stock_eu", "eu_stock", "stock_se", "se_stock", "europe_stock", "swedish_stock"}
        or _is_hk_symbol(raw_symbol)
        or _is_kr_symbol(raw_symbol)
        or _is_cn_equity_symbol(raw_symbol)
    )

    if asset_type == "liability":
        return "其他资产"
    if asset_type in {"real_estate", "property", "house", "real_asset"} or "房地产" in name or "房产" in name:
        return "房地产 / 实物资产"
    if asset_type in {"receivable", "dividend_receivable", "pending_cash"} or "应收" in name or "待到账" in name:
        return "应收款 / 待到账"
    if (
        "esop" in asset_type
        or asset_type in {"private_equity", "unlisted_equity", "private_stock", "private_share"}
        or "非上市" in asset_type
        or "huawei" in name
        or "华为" in name
        or "内部股份" in name
    ):
        return "非上市股权"
    if symbol == "SGOV" or asset_type in {"money_market", "short_bond", "short_duration_usd", "t_bill", "treasury_bill"}:
        return "债券 / 货币基金"
    if "货币" in name or "短债" in name or "现金管理" in name or "treasury" in name:
        return "债券 / 货币基金"
    if asset_type in {"cash", "deposit"} or "现金" in name or "公积金" in name:
        return "现金"
    if (
        symbol in {"USO", "GLD", "IAU", "SLV", "CPER"}
        or asset_type in {"precious_metal", "gold", "commodity", "commodity_fund", "oil_fund"}
        or (not stock_hint and ("黄金" in name or "gold" in name or "oil fund" in name))
    ):
        return "黄金 / 商品"
    if (
        holding.get("has_lookthrough")
        or "fund" in asset_type
        or "etf" in asset_type
        or asset_type in {"index_proxy", "index_fund", "mutual_fund", "fund_proxy", "lof"}
        or symbol in {"QQQ", "SPY", "IVV", "VOO"}
        or "基金" in name
        or re.search(r"(?<![a-z])etf(?![a-z])", name)
        or re.search(r"(?<![a-z])lof(?![a-z])", name)
        or "联接" in name
        or "混合" in name
        or "trust series" in name
    ):
        return "ETF / 基金"
    if _is_hk_symbol(raw_symbol):
        return "港股个股"
    if _is_kr_symbol(raw_symbol) or asset_type in {"stock_kr", "kr_stock"} or currency == "KRW":
        return "韩股个股"
    if asset_type in {"stock_eu", "eu_stock", "stock_se", "se_stock", "europe_stock", "swedish_stock"} or currency == "SEK":
        return "欧股个股"
    if _is_cn_equity_symbol(raw_symbol):
        return "A股个股"
    if asset_type in {"stock_hk", "hk_stock"} or currency == "HKD":
        return "港股个股"
    if asset_type in {"stock_us", "us_stock"} or currency == "USD":
        return "美股个股"
    if asset_type in {"stock_cn", "a_share_stock", "cn_stock"} or currency == "CNY":
        return "A股个股"
    return "其他资产"


def resolve_market(holding: dict[str, Any], asset_type: AssetType | None = None) -> str:
    asset_type = asset_type or resolveAssetType(holding)
    explicit = _text(holding.get("market"))
    if explicit and explicit != "其他":
        return explicit
    raw_asset_type = _key(holding.get("asset_type") or holding.get("asset_class") or holding.get("underlying_asset_class"))
    currency = _currency(holding.get("currency") or holding.get("underlying_currency"))
    raw_symbol = holding.get("symbol") or holding.get("underlying_symbol")
    if asset_type == "现金":
        return "现金"
    if asset_type == "债券 / 货币基金":
        return "货币基金 / 债券"
    if asset_type == "黄金 / 商品":
        return "黄金 / 商品"
    if asset_type == "非上市股权":
        return "非上市 / 内部"
    if asset_type == "ETF / 基金":
        return "基金"
    if raw_asset_type in {"stock_kr", "kr_stock"} or _is_kr_symbol(raw_symbol):
        return "韩股"
    if raw_asset_type in {"stock_eu", "eu_stock", "stock_se", "se_stock", "europe_stock", "swedish_stock"} or currency == "SEK":
        return "欧股"
    if asset_type == "A股个股" or _is_cn_equity_symbol(raw_symbol):
        return "A股"
    if asset_type == "港股个股" or currency == "HKD" or _is_hk_symbol(raw_symbol):
        return "港股"
    if asset_type == "韩股个股" or currency == "KRW":
        return "韩股"
    if asset_type == "美股个股" or currency == "USD":
        return "美股"
    if asset_type == "欧股个股" or currency == "SEK":
        return "欧股"
    return "其他"


def resolve_region(holding: dict[str, Any], market: str | None = None) -> str:
    region_raw = _key(holding.get("region") or holding.get("underlying_region"))
    name = _key(holding.get("asset_name") or holding.get("name") or holding.get("underlying_name"))
    raw_symbol = holding.get("symbol") or holding.get("underlying_symbol")
    symbol = _canonical_symbol(raw_symbol, name)
    asset_type = holding.get("assetType") or resolveAssetType(holding)
    currency = _currency(holding.get("currency") or holding.get("underlying_currency"))
    explicit_region = _text(holding.get("economic_region"))
    if explicit_region:
        return explicit_region
    if asset_type == "黄金 / 商品":
        return "全球"
    if asset_type in {"现金", "债券 / 货币基金", "应收款 / 待到账"}:
        if currency == "CNY":
            return "中国大陆"
        if currency == "HKD":
            return "香港"
        if currency == "USD":
            return "美国"
        if currency == "KRW":
            return "韩国"
        if currency == "SEK":
            return "瑞典"
    if symbol in CHINA_ECONOMIC_EXPOSURE_SYMBOLS or any(token in name for token in ("中国", "华为", "招商", "紫金", "铜陵", "比亚迪", "美团", "腾讯", "阿里", "小米", "快手", "中芯")):
        return "中国大陆"
    if region_raw in {"china", "cn", "mainland china", "中国", "中国大陆"}:
        return "中国大陆"
    if region_raw in {"hong kong", "hk", "香港"}:
        return "香港"
    if region_raw in {"united states", "us", "usa", "美国"}:
        if symbol in {"BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI"}:
            return "中国大陆"
        return "美国"
    if region_raw in {"korea", "south korea", "kr", "韩国"} or _is_kr_symbol(raw_symbol):
        return "韩国"
    if region_raw in {"sweden", "se", "sverige", "瑞典"}:
        return "瑞典"
    if region_raw in {"europe", "eu", "欧洲"}:
        return "欧洲"
    if region_raw in {"global", "全球"}:
        return "全球"
    market = market or resolve_market(holding)
    if market == "港股":
        return "香港"
    if market == "美股":
        return "美国"
    if market == "韩股":
        return "韩国"
    if market == "欧股":
        return "欧洲"
    if market in {"A股", "基金", "现金"}:
        return "中国大陆"
    return "其他"


def _industry_from_label(label: Any) -> IndustryBucket:
    raw = _text(label)
    if not raw:
        return UNMAPPED_INDUSTRY
    if raw in INDUSTRY_BUCKETS:
        return raw  # type: ignore[return-value]
    if raw in FORBIDDEN_INDUSTRY_LABELS:
        return UNMAPPED_INDUSTRY
    if raw in SW_L1_TO_BUCKET:
        return SW_L1_TO_BUCKET[raw]
    key = raw.casefold()
    if key in GICS_SECTOR_TO_BUCKET:
        return GICS_SECTOR_TO_BUCKET[key]
    if "industrials" in key:
        if any(x in key for x in ("transport", "logistics", "rail", "marine", "trucking", "environmental", "facilities")):
            return "稳定公用"
        if any(x in key for x in ("capital goods", "aerospace", "defense", "machinery", "electrical equipment")):
            return "高端制造"
    if "consumer discretionary" in key:
        if "automobile" in key or "auto components" in key:
            return "高端制造"
        return "大消费"
    for bucket, keywords in INDUSTRY_KEYWORD_BUCKETS:
        if any(keyword.casefold() in key for keyword in keywords):
            return bucket
    return UNMAPPED_INDUSTRY


def resolveIndustryBucket(holding: dict[str, Any]) -> dict[str, Any]:
    """Resolve a single industry bucket without leaking other dimensions."""
    asset_type = holding.get("assetType") or resolveAssetType(holding)
    explicit_bucket = _text(holding.get("industry_bucket") or holding.get("industryBucket"))
    if explicit_bucket in INDUSTRY_BUCKETS and explicit_bucket not in FORBIDDEN_INDUSTRY_LABELS and explicit_bucket != UNMAPPED_INDUSTRY:
        return {
            "bucket": explicit_bucket,
            "leaf": _text(holding.get("industryLeaf") or holding.get("industry") or holding.get("underlying_industry")) or None,
            "sub_bucket": _text(holding.get("industry_sub_bucket") or holding.get("industrySubBucket")) or None,
            "reason": "asset_master",
        }
    if asset_type in {"现金", "债券 / 货币基金", "非上市股权", "黄金 / 商品", "房地产 / 实物资产", "应收款 / 待到账"}:
        return {"bucket": None, "leaf": None, "reason": f"{asset_type} not industry eligible"}
    if asset_type == "ETF / 基金" and holding.get("lookthroughStatus") == "lookthrough_missing":
        return {"bucket": UNMAPPED_INDUSTRY, "leaf": None, "reason": "fund without lookthrough"}

    symbol = _canonical_symbol(holding.get("symbol") or holding.get("underlyingSymbol") or holding.get("underlying_symbol"), holding.get("name") or holding.get("underlyingName"))
    if symbol in INDUSTRY_SYMBOL_OVERRIDES:
        return {"bucket": INDUSTRY_SYMBOL_OVERRIDES[symbol], "leaf": holding.get("industryLeaf"), "sub_bucket": holding.get("industrySubBucket"), "reason": "symbol_override"}
    name = _text(holding.get("name") or holding.get("asset_name") or holding.get("underlyingName") or holding.get("underlying_name"))
    if name in INDUSTRY_SYMBOL_OVERRIDES:
        return {"bucket": INDUSTRY_SYMBOL_OVERRIDES[name], "leaf": holding.get("industryLeaf"), "sub_bucket": holding.get("industrySubBucket"), "reason": "name_override"}
    sw_l1 = _text(holding.get("sw_l1"))
    if sw_l1:
        return {"bucket": SW_L1_TO_BUCKET.get(sw_l1, UNMAPPED_INDUSTRY), "leaf": sw_l1, "sub_bucket": holding.get("industrySubBucket"), "reason": "sw_l1"}
    leaf = _text(holding.get("industryLeaf") or holding.get("industry") or holding.get("underlying_industry"))
    bucket = _industry_from_label(leaf)
    if bucket == UNMAPPED_INDUSTRY and name:
        bucket = _industry_from_label(name)
    return {
        "bucket": bucket,
        "leaf": leaf or None,
        "sub_bucket": _text(holding.get("industry_sub_bucket") or holding.get("industrySubBucket")) or None,
        "reason": "industry_label" if bucket != UNMAPPED_INDUSTRY else "unmapped",
    }


def _theme_item(theme_id: str, score: float, *, source: str = "rule", confidence: str = "high") -> ThemeExposure:
    name, family = THEME_DEFINITIONS.get(theme_id, THEME_DEFINITIONS["unmapped_theme"])
    return {
        "themeId": theme_id,
        "themeName": name,
        "family": family,  # type: ignore[typeddict-item]
        "score": score,
        "role": _role(score),
        "source": source,  # type: ignore[typeddict-item]
        "confidence": confidence,  # type: ignore[typeddict-item]
    }


def _merge_theme_rules(rules: list[tuple[str, float]], source: str, confidence: str) -> list[ThemeExposure]:
    best: dict[str, float] = {}
    for theme_id, score in rules:
        if theme_id not in THEME_DEFINITIONS:
            theme_id = "unmapped_theme"
        best[theme_id] = max(best.get(theme_id, 0.0), score)
    return [_theme_item(theme_id, score, source=source, confidence=confidence) for theme_id, score in sorted(best.items())]


def _theme_rules_from_text(text: str) -> list[tuple[str, float]]:
    key = text.casefold()
    rules: list[tuple[str, float]] = []
    for keywords, mapped in THEME_NAME_KEYWORD_RULES:
        if any(keyword.casefold() in key for keyword in keywords):
            rules.extend(mapped)
    return rules


def resolveThemeExposures(holding: dict[str, Any]) -> list[ThemeExposure]:
    """Resolve multi-label theme exposures. Themes are allowed to overlap."""
    explicit = holding.get("themeExposures") or holding.get("theme_exposures")
    if isinstance(explicit, list) and explicit:
        return [
            {
                "themeId": _text(item.get("themeId") or item.get("theme_id")),
                "themeName": _text(item.get("themeName") or item.get("theme_name")),
                "family": _text(item.get("family") or "未映射主题"),  # type: ignore[typeddict-item]
                "score": _num(item.get("score")),
                "role": _text(item.get("role") or _role(_num(item.get("score")))),  # type: ignore[typeddict-item]
                "source": _text(item.get("source") or "manual"),  # type: ignore[typeddict-item]
                "confidence": _text(item.get("confidence") or "medium"),  # type: ignore[typeddict-item]
            }
            for item in explicit
            if _text(item.get("themeName") or item.get("theme_name"))
        ]
    asset_type = holding.get("assetType") or resolveAssetType(holding)
    if asset_type in {"现金", "债券 / 货币基金", "非上市股权", "房地产 / 实物资产", "应收款 / 待到账"}:
        return []
    if asset_type == "ETF / 基金" and holding.get("lookthroughStatus") == "lookthrough_missing":
        return []

    name = _text(holding.get("name") or holding.get("asset_name") or holding.get("underlyingName") or holding.get("underlying_name"))
    symbol = _canonical_symbol(holding.get("symbol") or holding.get("underlyingSymbol") or holding.get("underlying_symbol"), name)
    rules = list(THEME_SYMBOL_RULES.get(symbol, []))
    text_rules = _theme_rules_from_text(" ".join([
        name,
        _text(holding.get("industryLeaf") or holding.get("industry") or holding.get("underlying_industry")),
        _text(holding.get("rawTheme") or holding.get("theme") or holding.get("underlying_theme")),
    ]))
    rules.extend(text_rules)
    ai_sub_rules = _ai_sub_theme_rules(holding)
    if ai_sub_rules:
        rules.append(("ai_compute", max(score for _sub_id, score in ai_sub_rules)))
    if asset_type == "黄金 / 商品":
        rules.extend([("gold_hedge", 1.0), ("precious_copper", 0.7)])
    return _merge_theme_rules(rules, "rule", "high")


def resolve_index_membership_tags(holding: dict[str, Any]) -> list[str]:
    symbol = _canonical_symbol(holding.get("symbol") or holding.get("underlying_symbol"), holding.get("name") or holding.get("asset_name"))
    text = " ".join(
        _text(holding.get(key))
        for key in ("name", "asset_name", "symbol", "industry", "underlying_industry", "rawTheme", "theme", "underlying_theme", "style")
    ).casefold()
    tags: list[str] = []
    if symbol == "QQQ" or "nasdaq" in text or "纳指" in text:
        tags.append("Nasdaq-100 constituent")
    if "s&p 500" in text or "sp500" in text or "标普500" in text:
        tags.append("S&P 500 constituent")
    if "沪深300" in text or "hs300" in text:
        tags.append("沪深300成分")
    if "中证500" in text or "csi500" in text:
        tags.append("中证500成分")
    return tags


def _ai_sub_theme_rules(holding: dict[str, Any]) -> list[tuple[str, float]]:
    explicit = holding.get("aiSubThemes") or holding.get("ai_sub_themes") or holding.get("underlying_ai_sub_themes")
    rules: list[tuple[str, float]] = []
    if isinstance(explicit, list):
        for item in explicit:
            sub_id = _text(item.get("subThemeId") or item.get("sub_theme_id") or item.get("theme_id"))
            score = _num(item.get("score"))
            if sub_id and score > 0:
                rules.append((sub_id, score))
    name = _text(holding.get("name") or holding.get("asset_name") or holding.get("underlyingName") or holding.get("underlying_name"))
    symbol = _canonical_symbol(holding.get("symbol") or holding.get("underlyingSymbol") or holding.get("underlying_symbol"), name)
    rules.extend(AI_SUB_THEME_RULES.get(symbol, []))
    key = name.casefold()
    if any(x in key for x in ("工业富联", "浪潮信息", "server", "服务器")):
        rules.append(("cpu_server", 1.0))
    if any(x in key for x in ("中际旭创", "新易盛", "天孚通信", "coherent", "lumentum", "光模块", "cpo")):
        rules.append(("optical_cpo", 1.0))
    if any(x in key for x in ("沪电股份", "胜宏科技", "深南电路", "pcb")):
        rules.append(("pcb_connector", 1.0))
    if any(x in key for x in ("vertiv", "英维克", "科华数据", "散热", "液冷")):
        rules.append(("power_cooling", 1.0))
    return rules


def normalizeHoldings(rawHoldings: list[dict[str, Any]]) -> list[HoldingNormalized]:
    total = sum(max(_num(h.get("market_value_cny", h.get("marketValue"))), 0.0) for h in rawHoldings)
    out: list[HoldingNormalized] = []
    for idx, raw in enumerate(rawHoldings):
        value = max(_num(raw.get("market_value_cny", raw.get("marketValue"))), 0.0)
        name = _text(raw.get("asset_name") or raw.get("name") or raw.get("underlying_name") or raw.get("symbol") or f"holding-{idx}")
        symbol = _text(raw.get("symbol") or raw.get("asset_id") or "")
        asset_type = resolveAssetType(raw)
        market = resolve_market(raw, asset_type)
        region = resolve_region(raw, market)
        lookthrough_status = "not_applicable"
        if asset_type == "ETF / 基金":
            lookthrough_status = "lookthrough_available" if raw.get("has_lookthrough") else "lookthrough_missing"
        elif asset_type in {"A股个股", "港股个股", "美股个股", "欧股个股", "韩股个股", "黄金 / 商品"}:
            lookthrough_status = "direct"
        item: HoldingNormalized = {
            "id": _text(raw.get("id") or raw.get("asset_id") or symbol or name),
            "symbol": symbol,
            "name": name,
            "market": market,  # type: ignore[typeddict-item]
            "region": region,  # type: ignore[typeddict-item]
            "currency": _currency(raw.get("currency")),
            "assetType": asset_type,
            "assetSubtype": _text(raw.get("asset_subtype")),
            "marketValue": value,
            "portfolioWeight": value / total if total > 0 else 0.0,
            "industryLeaf": _text(raw.get("industry") or raw.get("industryLeaf") or raw.get("underlying_industry")) or None,
            "industrySubBucket": _text(raw.get("industry_sub_bucket") or raw.get("industrySubBucket")) or None,
            "rawTags": [
                _text(raw.get("industry") or raw.get("underlying_industry")),
                _text(raw.get("theme") or raw.get("underlying_theme")),
                _text(raw.get("style")),
            ],
            "indexMembershipTags": resolve_index_membership_tags(raw),
            "lookthroughStatus": lookthrough_status,  # type: ignore[typeddict-item]
            "account": _text(raw.get("account") or raw.get("account_name") or raw.get("account_code")),
            "sw_l1": _text(raw.get("sw_l1")),
        }
        industry = resolveIndustryBucket(item | {"sw_l1": raw.get("sw_l1"), "industry": item.get("industryLeaf")})
        item["industryBucket"] = industry["bucket"]
        item["industrySubBucket"] = _text(industry.get("sub_bucket") or item.get("industrySubBucket")) or None
        item["industryBucketSource"] = _text(industry.get("reason"))
        item["themeExposures"] = resolveThemeExposures(item | raw)
        item["aiSubThemes"] = [
            {
                "subThemeId": sub_id,
                "subThemeName": AI_SUB_THEME_DEFINITIONS.get(sub_id, sub_id),
                "score": score,
                "role": _role(score),
            }
            for sub_id, score in _ai_sub_theme_rules(item | raw)
        ]
        out.append(item)
    return out


def buildLookthroughHoldings(holdings: list[HoldingNormalized], fundUnderlyingData: list[dict[str, Any]]) -> list[UnderlyingHolding]:
    by_id = {h["id"]: h for h in holdings}
    out: list[UnderlyingHolding] = []
    total_value = sum(h["marketValue"] for h in holdings)
    for row in fundUnderlyingData:
        parent_id = _text(row.get("parentHoldingId") or row.get("parent_holding_id") or row.get("parent_symbol"))
        parent = by_id.get(parent_id)
        parent_value = _num(row.get("parentMarketValue", parent.get("marketValue") if parent else row.get("parent_market_value_cny")))
        weight = max(_num(row.get("underlyingWeightInFund", row.get("underlying_weight"))), 0.0)
        effective_value = parent_value * weight
        underlying_symbol = row.get("underlyingSymbol") or row.get("underlying_symbol")
        underlying_currency = row.get("underlying_currency") or row.get("currency")
        underlying_region = row.get("underlying_region") or row.get("region")
        underlying_asset_class = (
            row.get("underlying_asset_class")
            or _infer_stock_asset_class(underlying_symbol, underlying_currency, underlying_region)
            or row.get("asset_type")
        )
        raw = {
            "symbol": underlying_symbol,
            "asset_name": row.get("underlyingName") or row.get("underlying_name"),
            "currency": underlying_currency,
            "asset_type": underlying_asset_class,
            "region": underlying_region,
            "industry": row.get("underlying_industry") or row.get("industry"),
            "industry_bucket": row.get("industry_bucket"),
            "industry_sub_bucket": row.get("industry_sub_bucket"),
            "theme": row.get("theme") or row.get("underlying_theme"),
            "theme_exposures": row.get("theme_exposures") or row.get("underlying_theme_exposures"),
            "ai_sub_themes": row.get("ai_sub_themes") or row.get("underlying_ai_sub_themes"),
            "sw_l1": row.get("sw_l1"),
        }
        asset_type = resolveAssetType(raw)
        industry = resolveIndustryBucket(raw | {"assetType": asset_type})
        market = resolve_market(raw, asset_type)
        region = resolve_region(raw, market)
        out.append(
            {
                "parentHoldingId": parent_id,
                "parentName": _text(row.get("parentName") or row.get("parent_name") or (parent or {}).get("name")),
                "underlyingSymbol": _text(raw.get("symbol")),
                "underlyingName": _text(raw.get("asset_name")),
                "underlyingWeightInFund": weight,
                "effectiveWeightInPortfolio": effective_value / total_value if total_value > 0 else 0.0,
                "effectiveMarketValue": effective_value,
                "assetType": asset_type,
                "industryBucket": industry["bucket"] or UNMAPPED_INDUSTRY,
                "industryLeaf": industry["leaf"],
                "industrySubBucket": industry.get("sub_bucket"),
                "themeExposures": resolveThemeExposures(raw | {"assetType": asset_type}),
                "aiSubThemes": [
                    {
                        "subThemeId": sub_id,
                        "subThemeName": AI_SUB_THEME_DEFINITIONS.get(sub_id, sub_id),
                        "score": score,
                        "role": _role(score),
                    }
                    for sub_id, score in _ai_sub_theme_rules(raw | {"assetType": asset_type})
                ],
                "sw_l1": _text(raw.get("sw_l1")),
                "industryBucketSource": _text(industry.get("reason")),
                "market": market,
                "region": region,
                "currency": _currency(raw.get("currency")),
                "account": _text(row.get("account")),
                "lookthroughType": _text(row.get("lookthrough_type") or "fund_lookthrough").upper(),
                "confidence": _text(row.get("confidence") or "MEDIUM").upper(),
            }
        )
    return out


def _source_details_from_contribs(contribs: list[dict[str, Any]], denominator: float) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in contribs:
        source_name = _text(c.get("source_asset_name") or c.get("name"))
        account = _text(c.get("account") or "")
        lookthrough_type = _text(c.get("lookthrough_type") or c.get("source") or "DIRECT")
        underlying_name = _text(c.get("underlying_name"))
        underlying_symbol = _text(c.get("underlying_symbol"))
        key = (source_name, account, lookthrough_type)
        item = grouped.setdefault(
            key,
            {
                "source_asset_name": source_name,
                "source_symbol": c.get("source_symbol") or c.get("symbol"),
                "account": account,
                "contribution_to_bucket_cny": 0.0,
                "lookthrough_type": lookthrough_type,
                "confidence": c.get("confidence") or "HIGH",
                "underlying_name": "",
                "underlying_symbol": "",
                "underlyings": {},
                "industry_bucket": c.get("industry_bucket"),
                "industry_sub_bucket": c.get("industry_sub_bucket") or "",
                "sw_l1": c.get("sw_l1") or "",
                "industry_bucket_source": c.get("industry_bucket_source") or "",
                "theme_names": c.get("theme_names") or [],
            },
        )
        contribution = _num(c.get("value_cny"))
        item["contribution_to_bucket_cny"] += contribution
        if underlying_name and _is_real_lookthrough_type(lookthrough_type):
            label = f"{underlying_name} ({underlying_symbol})" if underlying_symbol and underlying_symbol != underlying_name else underlying_name
            item["underlyings"][label] = item["underlyings"].get(label, 0.0) + contribution
    rows = list(grouped.values())
    for row in rows:
        underlyings = sorted(row.pop("underlyings", {}).items(), key=lambda kv: -abs(kv[1]))
        row["underlying_count"] = len(underlyings)
        row["underlying_names"] = [name for name, _value in underlyings[:10]]
        row["underlying_top"] = [
            {
                "name": name,
                "value_cny": value,
                "weight": value / row["contribution_to_bucket_cny"] if row["contribution_to_bucket_cny"] else 0.0,
            }
            for name, value in underlyings[:10]
        ]
        if len(underlyings) == 1:
            row["underlying_name"] = underlyings[0][0]
        row["weight"] = row["contribution_to_bucket_cny"] / denominator if denominator > 0 else 0.0
    return sorted(rows, key=lambda r: -abs(_num(r.get("contribution_to_bucket_cny"))))[:30]


def _aggregate_contributions(contributions: list[dict[str, Any]], denominator: float) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for c in contributions:
        name = _text(c.get("name") or UNMAPPED_INDUSTRY)
        item = grouped.setdefault(
            name,
            {
                "name": name,
                "display_name": c.get("display_name") or name,
                "value_cny": 0.0,
                "direct_value_cny": 0.0,
                "indirect_value_cny": 0.0,
                "source_breakdown": defaultdict(float),
                "contribs": [],
            },
        )
        value = _num(c.get("value_cny"))
        item["value_cny"] += value
        if c.get("source") == "fund_lookthrough":
            item["indirect_value_cny"] += value
        else:
            item["direct_value_cny"] += value
        item["source_breakdown"][c.get("source") or "direct"] += value
        item["contribs"].append(c)
    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        value = _num(item["value_cny"])
        contribs = item.pop("contribs")
        details = _source_details_from_contribs(contribs, denominator)
        sub_bucket_values: dict[str, float] = defaultdict(float)
        for contrib in contribs:
            sub_bucket = _text(contrib.get("industry_sub_bucket"))
            if sub_bucket:
                sub_bucket_values[sub_bucket] += _num(contrib.get("value_cny"))
        rows.append(
            {
                "name": item["name"],
                "display_name": item["display_name"],
                "value_cny": value,
                "total_exposure_cny": value,
                "weight": value / denominator if denominator > 0 else 0.0,
                "total_weight": value / denominator if denominator > 0 else 0.0,
                "direct_value_cny": item["direct_value_cny"],
                "indirect_value_cny": item["indirect_value_cny"],
                "source_breakdown": dict(item["source_breakdown"]),
                "main_sources": [
                    {"name": d["source_asset_name"], "value_cny": d["contribution_to_bucket_cny"], "weight": d["weight"]}
                    for d in details[:5]
                ],
                "top_sources": [
                    {"name": d["source_asset_name"], "value_cny": d["contribution_to_bucket_cny"], "weight": d["weight"]}
                    for d in details[:5]
                ],
                "source_details": details,
                "sub_buckets": [
                    {
                        "name": name,
                        "value_cny": sub_value,
                        "weight": sub_value / value if value else 0.0,
                    }
                    for name, sub_value in sorted(sub_bucket_values.items(), key=lambda kv: -abs(kv[1]))[:8]
                ],
                "source_count": len(details),
                "confidence": "HIGH",
                "lookthrough_coverage": 1.0,
            }
        )
    return sorted(rows, key=lambda r: -abs(_num(r["value_cny"])))


def _is_industry_eligible(asset_type: str) -> bool:
    return asset_type in {"A股个股", "港股个股", "美股个股", "欧股个股", "韩股个股", "ETF / 基金"}


def _is_theme_eligible(asset_type: str) -> bool:
    return asset_type not in {"现金", "债券 / 货币基金", "非上市股权", "ETF / 基金", "房地产 / 实物资产", "应收款 / 待到账"}


NON_TRADING_MARKET_BUCKETS = {"现金", "基金", "货币基金 / 债券"}


def _is_market_exposure_eligible(market: str | None, *, lookthrough_type: str = "") -> bool:
    if lookthrough_type in {"PRODUCT_TAG", "MISSING"}:
        return False
    bucket = _text(market)
    return bool(bucket) and bucket not in NON_TRADING_MARKET_BUCKETS


def calculateAssetTypeExposure(normalizedHoldings: list[HoldingNormalized], scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    denominator = sum(h["marketValue"] for h in normalizedHoldings)
    return _aggregate_contributions(
        [
            {
                "name": h["assetType"],
                "value_cny": h["marketValue"],
                "source_asset_name": h["name"],
                "source_symbol": h.get("symbol"),
                "account": h.get("account") or "",
                "source": "direct",
                "lookthrough_type": "DIRECT",
            }
            for h in normalizedHoldings
            if h["marketValue"] > 0
        ],
        denominator,
    )


def calculateCurrencyExposure(normalizedHoldings: list[HoldingNormalized], scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    denominator = sum(h["marketValue"] for h in normalizedHoldings)
    return _aggregate_contributions(
        [
            {
                "name": h["currency"],
                "value_cny": h["marketValue"],
                "source_asset_name": h["name"],
                "source_symbol": h.get("symbol"),
                "account": h.get("account") or "",
                "source": "direct",
                "lookthrough_type": "DIRECT",
            }
            for h in normalizedHoldings
        ],
        denominator,
    )


def calculateMarketExposure(
    normalizedHoldings: list[HoldingNormalized],
    underlyingHoldings: list[UnderlyingHolding] | None = None,
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    contributions: list[dict[str, Any]] = []
    for h in normalizedHoldings:
        if h["marketValue"] <= 0:
            continue
        if h["assetType"] == "ETF / 基金" and h.get("lookthroughStatus") == "lookthrough_available":
            continue
        if not _is_market_exposure_eligible(h.get("market")):
            continue
        contributions.append(
            {
                "name": h["market"],
                "value_cny": h["marketValue"],
                "source_asset_name": h["name"],
                "source_symbol": h.get("symbol"),
                "account": h.get("account") or "",
                "source": "direct",
                "lookthrough_type": "DIRECT",
            }
        )
    for u in underlyingHoldings or []:
        lookthrough_type = _text(u.get("lookthroughType") or "FUND_LOOKTHROUGH").upper()
        if lookthrough_type == "DIRECT":
            continue
        market = "基金" if lookthrough_type in {"PRODUCT_TAG", "MISSING"} else (u.get("market") or "其他")
        if not _is_market_exposure_eligible(market, lookthrough_type=lookthrough_type):
            continue
        contributions.append(
            {
                "name": market,
                "value_cny": u["effectiveMarketValue"],
                "source_asset_name": u["parentName"],
                "source_symbol": u.get("parentHoldingId"),
                "account": u.get("account") or "",
                "underlying_name": u.get("underlyingName"),
                "underlying_symbol": u.get("underlyingSymbol"),
                "source": "fund_lookthrough",
                "lookthrough_type": lookthrough_type,
                "confidence": u.get("confidence") or "MEDIUM",
            }
        )
    denominator = sum(_num(c.get("value_cny")) for c in contributions)
    return _aggregate_contributions(contributions, denominator)


def calculateRegionExposure(
    normalizedHoldings: list[HoldingNormalized],
    underlyingHoldings: list[UnderlyingHolding] | None = None,
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    denominator = sum(h["marketValue"] for h in normalizedHoldings)
    return _aggregate_contributions(
        [
            {
                "name": h["region"],
                "value_cny": h["marketValue"],
                "source_asset_name": h["name"],
                "source_symbol": h.get("symbol"),
                "account": h.get("account") or "",
                "source": "direct",
                "lookthrough_type": "DIRECT",
            }
            for h in normalizedHoldings
        ],
        denominator,
    )


def calculateIndustryExposure(
    normalizedHoldings: list[HoldingNormalized],
    underlyingHoldings: list[UnderlyingHolding] | None = None,
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    contributions: list[dict[str, Any]] = []
    for h in normalizedHoldings:
        if not _is_industry_eligible(h["assetType"]):
            continue
        if h["assetType"] == "ETF / 基金" and h.get("lookthroughStatus") == "lookthrough_available":
            continue
        bucket = h.get("industryBucket") or UNMAPPED_INDUSTRY
        contributions.append(
            {
                "name": bucket,
                "value_cny": h["marketValue"],
                "source_asset_name": h["name"],
                "source_symbol": h.get("symbol"),
                "account": h.get("account") or "",
                "source": "direct",
                "lookthrough_type": "DIRECT",
                "industry_bucket": bucket,
                "industry_sub_bucket": h.get("industrySubBucket") or "",
                "sw_l1": h.get("sw_l1") or "",
                "industry_bucket_source": h.get("industryBucketSource") or "",
            }
        )
    for u in underlyingHoldings or []:
        if u.get("lookthroughType") == "DIRECT":
            continue
        if not _is_industry_eligible(u["assetType"]):
            continue
        bucket = u.get("industryBucket") or UNMAPPED_INDUSTRY
        contributions.append(
            {
                "name": bucket,
                "value_cny": u["effectiveMarketValue"],
                "source_asset_name": u["parentName"],
                "source_symbol": u.get("parentHoldingId"),
                "account": u.get("account") or "",
                "underlying_name": u.get("underlyingName"),
                "underlying_symbol": u.get("underlyingSymbol"),
                "source": "fund_lookthrough",
                "lookthrough_type": u.get("lookthroughType") or "FUND_LOOKTHROUGH",
                "industry_bucket": bucket,
                "industry_sub_bucket": u.get("industrySubBucket") or "",
                "sw_l1": u.get("sw_l1") or "",
                "industry_bucket_source": u.get("industryBucketSource") or "",
            }
        )
    denominator = sum(_num(c.get("value_cny")) for c in contributions)
    rows = _aggregate_contributions(contributions, denominator)
    validateIndustryBuckets(rows, strict=False)
    return rows


def calculateThemeExposure(
    normalizedHoldings: list[HoldingNormalized],
    underlyingHoldings: list[UnderlyingHolding] | None = None,
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    total = sum(h["marketValue"] for h in normalizedHoldings)
    contributions: list[dict[str, Any]] = []
    for h in normalizedHoldings:
        if not _is_theme_eligible(h["assetType"]):
            continue
        for theme in h.get("themeExposures", []):
            if theme["themeName"] in FORBIDDEN_THEME_LABELS:
                continue
            contributions.append(
                {
                    "name": theme["themeName"],
                    "display_name": theme["themeName"],
                    "theme_id": theme["themeId"],
                    "family": theme["family"],
                    "score": theme["score"],
                    "value_cny": h["marketValue"] * _num(theme["score"]),
                    "source_asset_name": h["name"],
                    "source_symbol": h.get("symbol"),
                    "account": h.get("account") or "",
                    "source": theme["source"],
                    "lookthrough_type": "DIRECT",
                    "confidence": theme["confidence"].upper(),
                    "theme_names": [theme["themeName"]],
                }
            )
    for u in underlyingHoldings or []:
        if u.get("lookthroughType") == "DIRECT":
            continue
        if not _is_theme_eligible(u["assetType"]):
            continue
        for theme in u.get("themeExposures", []):
            contributions.append(
                {
                    "name": theme["themeName"],
                    "display_name": theme["themeName"],
                    "theme_id": theme["themeId"],
                    "family": theme["family"],
                    "score": theme["score"],
                    "value_cny": u["effectiveMarketValue"] * _num(theme["score"]),
                    "source_asset_name": u["parentName"],
                    "source_symbol": u.get("parentHoldingId"),
                    "account": u.get("account") or "",
                    "underlying_name": u.get("underlyingName"),
                    "underlying_symbol": u.get("underlyingSymbol"),
                    "source": "fund_lookthrough",
                    "lookthrough_type": u.get("lookthroughType") or "FUND_LOOKTHROUGH",
                    "confidence": theme["confidence"].upper(),
                    "theme_names": [theme["themeName"]],
                }
            )
    rows = _aggregate_contributions(contributions, total)
    for row in rows:
        matches = [c for c in contributions if c["name"] == row["name"]]
        row["family"] = matches[0].get("family") if matches else ""
        row["theme_id"] = matches[0].get("theme_id") if matches else ""
        row["score_weighted"] = (
            sum(_num(c["value_cny"]) * _num(c.get("score")) for c in matches) / sum(_num(c["value_cny"]) for c in matches)
            if matches and sum(_num(c["value_cny"]) for c in matches) > 0
            else 0.0
        )
    validateThemeExposures(rows, strict=False)
    return rows


def _ai_sub_theme_exposure_from_holdings(
    normalizedHoldings: list[HoldingNormalized],
    total: float,
    underlyingHoldings: list[UnderlyingHolding] | None = None,
) -> list[dict[str, Any]]:
    sub_contribs: list[dict[str, Any]] = []
    for h in normalizedHoldings:
        if not _is_theme_eligible(h["assetType"]):
            continue
        rules = _ai_sub_theme_rules({"symbol": h.get("symbol"), "asset_name": h["name"], "ai_sub_themes": h.get("aiSubThemes")})
        if not rules:
            continue
        for sub_id, score in rules:
            sub_contribs.append(
                {
                    "name": AI_SUB_THEME_DEFINITIONS[sub_id],
                    "sub_theme_id": sub_id,
                    "value_cny": h["marketValue"] * score,
                    "score": score,
                    "source_asset_name": h["name"],
                    "source_symbol": h.get("symbol"),
                    "account": h.get("account") or "",
                    "source": "direct",
                    "lookthrough_type": "DIRECT",
                    "status": "待计算",
                    "strength_5d": None,
                    "strength_20d": None,
                }
            )
    for u in underlyingHoldings or []:
        if u.get("lookthroughType") == "DIRECT":
            continue
        if not _is_theme_eligible(u["assetType"]):
            continue
        rules = _ai_sub_theme_rules({"symbol": u.get("underlyingSymbol"), "asset_name": u.get("underlyingName"), "ai_sub_themes": u.get("aiSubThemes")})
        if not rules:
            continue
        for sub_id, score in rules:
            sub_contribs.append(
                {
                    "name": AI_SUB_THEME_DEFINITIONS[sub_id],
                    "sub_theme_id": sub_id,
                    "value_cny": u["effectiveMarketValue"] * score,
                    "score": score,
                    "source_asset_name": u["parentName"],
                    "source_symbol": u.get("parentHoldingId"),
                    "account": u.get("account") or "",
                    "underlying_name": u.get("underlyingName"),
                    "underlying_symbol": u.get("underlyingSymbol"),
                    "source": "fund_lookthrough",
                    "lookthrough_type": u.get("lookthroughType") or "FUND_LOOKTHROUGH",
                    "status": "待计算",
                    "strength_5d": None,
                    "strength_20d": None,
                }
            )
    rows = _aggregate_contributions(sub_contribs, total)
    for row in rows:
        row["status"] = "待计算"
        row["strength_5d"] = None
        row["strength_20d"] = None
    return rows


def calculateUnderlyingExposure(
    normalizedHoldings: list[HoldingNormalized],
    underlyingHoldings: list[UnderlyingHolding],
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    total = sum(h["marketValue"] for h in normalizedHoldings)
    rows: list[dict[str, Any]] = []
    direct_underlying_parent_keys: set[str] = set()
    for u in underlyingHoldings:
        if u.get("lookthroughType") == "DIRECT":
            direct_underlying_parent_keys.update(_identity_keys(u.get("parentHoldingId"), u.get("parentName")))

    for h in normalizedHoldings:
        source = "直接持仓"
        if h["assetType"] == "ETF / 基金" and h.get("lookthroughStatus") == "lookthrough_available":
            continue
        if _identity_keys(h.get("id"), h.get("symbol"), h.get("name")) & direct_underlying_parent_keys:
            continue
        rows.append(
            {
                "name": h["name"],
                "display_name": h["name"],
                "symbol": h.get("symbol"),
                "source": source,
                "source_asset_name": h["name"],
                "value_cny": h["marketValue"],
                "total_exposure_cny": h["marketValue"],
                "weight": h["marketValue"] / total if total > 0 else 0.0,
                "total_weight": h["marketValue"] / total if total > 0 else 0.0,
                "asset_type": h["assetType"],
                "asset_subtype": h.get("assetSubtype") or "",
                "industry_bucket": h.get("industryBucket") or UNMAPPED_INDUSTRY,
                "industry_sub_bucket": h.get("industrySubBucket") or "",
                "theme_names": [t["themeName"] for t in h.get("themeExposures", [])],
                "ai_sub_theme_names": [t.get("subThemeName") for t in h.get("aiSubThemes", [])],
                "market": h.get("market"),
                "region": h.get("region"),
                "currency": h.get("currency"),
                "account": h.get("account") or "",
                "source_details": [
                    {
                        "source_asset_name": h["name"],
                        "source_symbol": h.get("symbol"),
                        "account": h.get("account") or "",
                        "contribution_to_bucket_cny": h["marketValue"],
                        "lookthrough_type": "DIRECT",
                        "confidence": "HIGH",
                        "underlying_name": h["name"],
                        "underlying_symbol": h.get("symbol"),
                        "industry_bucket": h.get("industryBucket") or UNMAPPED_INDUSTRY,
                        "industry_sub_bucket": h.get("industrySubBucket") or "",
                        "sw_l1": h.get("sw_l1") or "",
                        "industry_bucket_source": h.get("industryBucketSource") or "",
                        "theme_names": [t["themeName"] for t in h.get("themeExposures", [])],
                    }
                ],
            }
        )
    for u in underlyingHoldings:
        value = u["effectiveMarketValue"]
        lookthrough_type = u.get("lookthroughType") or "FUND_LOOKTHROUGH"
        rows.append(
            {
                "name": u["underlyingName"],
                "display_name": u["underlyingName"],
                "symbol": u.get("underlyingSymbol"),
                "source": _lookthrough_source_label(lookthrough_type),
                "source_asset_name": u["parentName"],
                "value_cny": value,
                "total_exposure_cny": value,
                "weight": value / total if total > 0 else 0.0,
                "total_weight": value / total if total > 0 else 0.0,
                "asset_type": u["assetType"],
                "asset_subtype": u.get("assetSubtype") or "",
                "industry_bucket": u.get("industryBucket") or UNMAPPED_INDUSTRY,
                "industry_sub_bucket": u.get("industrySubBucket") or "",
                "theme_names": [t["themeName"] for t in u.get("themeExposures", [])],
                "ai_sub_theme_names": [t.get("subThemeName") for t in u.get("aiSubThemes", [])],
                "market": u.get("market"),
                "region": u.get("region"),
                "currency": u.get("currency"),
                "account": u.get("account") or "",
                "underlying_weight_in_fund": u["underlyingWeightInFund"],
                "source_details": [
                    {
                        "source_asset_name": u["parentName"],
                        "source_symbol": u.get("parentHoldingId"),
                        "account": u.get("account") or "",
                        "contribution_to_bucket_cny": value,
                        "lookthrough_type": lookthrough_type,
                        "confidence": u.get("confidence") or "MEDIUM",
                        "underlying_name": u.get("underlyingName"),
                        "underlying_symbol": u.get("underlyingSymbol"),
                        "industry_bucket": u.get("industryBucket") or UNMAPPED_INDUSTRY,
                        "industry_sub_bucket": u.get("industrySubBucket") or "",
                        "sw_l1": u.get("sw_l1") or "",
                        "industry_bucket_source": u.get("industryBucketSource") or "",
                        "theme_names": [t["themeName"] for t in u.get("themeExposures", [])],
                    }
                ],
            }
        )
    return sorted(rows, key=lambda r: -abs(_num(r["value_cny"])))[:200]


def calculateRiskSummary(allExposureResults: dict[str, list[dict[str, Any]]], quality: dict[str, Any] | None = None) -> dict[str, Any]:
    def top(rows: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
        return rows[:n]

    asset_rows = allExposureResults.get("asset_type", [])
    industry_rows = allExposureResults.get("industry", [])
    theme_rows = allExposureResults.get("theme", [])
    market_rows = allExposureResults.get("market", [])
    currency_rows = allExposureResults.get("currency", [])
    underlying_rows = allExposureResults.get("underlying_exposure", [])
    warnings_out: list[str] = []
    tech = next((r for r in industry_rows if r["name"] == "科技TMT"), None)
    ai = next((r for r in theme_rows if r["name"] == "AI / 算力"), None)
    semi = next((r for r in theme_rows if r["name"] == "半导体"), None)
    unlook = _num((quality or {}).get("uncovered_value_cny"))
    total = sum(_num(r.get("value_cny")) for r in asset_rows)
    if tech and _num(tech.get("weight")) > 0.45:
        warnings_out.append("科技TMT 暴露过高")
    if ai and semi and _num(ai.get("weight")) + _num(semi.get("weight")) > 0.50:
        warnings_out.append("AI / 半导体主题重叠过高")
    if unlook > 0:
        warnings_out.append("未穿透基金比例过高，主题判断可信度下降")
    private_equity = next((r for r in asset_rows if r["name"] == "非上市股权"), None)
    if private_equity and _num(private_equity.get("weight")) > 0.2:
        warnings_out.append("非上市股权占比高，流动性风险大")
    cash_bond = sum(_num(r.get("value_cny")) for r in asset_rows if r["name"] in {"现金", "债券 / 货币基金"})
    if total > 0 and cash_bond / total < 0.03:
        warnings_out.append("现金 / 债券防御垫不足")

    return {
        "top_concentration": top(underlying_rows, 5),
        "top_3_holdings": top(underlying_rows, 3),
        "top_3_themes": top(theme_rows, 3),
        "asset_type_mix": asset_rows,
        "unlooked_ratio": unlook / total if total > 0 else 0.0,
        "unlooked_value_cny": unlook,
        "single_currency_top": top(currency_rows, 1),
        "single_market_top": top(market_rows, 1),
        "alerts": warnings_out,
    }


def validateIndustryBuckets(data: list[dict[str, Any]], *, strict: bool | None = None) -> list[str]:
    strict = _strict_mode() if strict is None else strict
    problems: list[str] = []
    for row in data:
        label = _text(row.get("name") or row.get("industry_bucket"))
        if label not in INDUSTRY_BUCKETS or label in FORBIDDEN_INDUSTRY_LABELS:
            problems.append(label)
            row["name"] = UNMAPPED_INDUSTRY
            row["display_name"] = UNMAPPED_INDUSTRY
    if problems:
        _handle_validation("Invalid industry bucket labels: " + ", ".join(sorted(set(problems))), strict)
    return problems


def validateThemeExposures(data: list[dict[str, Any]], *, strict: bool | None = None) -> list[str]:
    strict = _strict_mode() if strict is None else strict
    valid_names = {name for name, _family in THEME_DEFINITIONS.values()}
    problems: list[str] = []
    for row in data:
        label = _text(row.get("name") or row.get("themeName"))
        if label in FORBIDDEN_THEME_LABELS or label not in valid_names:
            problems.append(label)
            row["name"] = UNMAPPED_THEME_NAME
            row["display_name"] = UNMAPPED_THEME_NAME
    if problems:
        _handle_validation("Invalid theme labels: " + ", ".join(sorted(set(problems))), strict)
    return problems


def validateExposureTotals(data: dict[str, list[dict[str, Any]]], *, tolerance: float = 0.01) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for key in ("industry", "asset_type", "market", "region", "currency"):
        rows = data.get(key) or []
        total = sum(_num(row.get("weight")) for row in rows)
        results[key] = {"weight_sum": total, "ok": abs(total - 1.0) <= tolerance if rows else True}
    theme_total = sum(_num(row.get("weight")) for row in data.get("theme") or [])
    results["theme"] = {"weight_sum": theme_total, "ok": True, "note": "theme exposures may overlap"}
    return results


def validateLookthroughCoverage(data: dict[str, Any]) -> dict[str, Any]:
    low_assets = data.get("low_confidence_assets") or []
    return {
        "coverage_ratio": _num(data.get("coverage_ratio")),
        "uncovered_value_cny": _num(data.get("uncovered_value_cny")),
        "uncovered_funds": [
            item for item in low_assets
            if _num(item.get("uncovered_value_cny")) > 0 and "FUND" in _text(item.get("current_mapping")).upper()
        ],
        "uncovered_assets": low_assets,
    }


def _strict_mode() -> bool:
    return os.environ.get("PAMS_EXPOSURE_VALIDATION", "").lower() in {"strict", "dev", "development"}


def _handle_validation(message: str, strict: bool) -> None:
    if strict:
        raise ValueError(message)
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def build_engine_underlyings_from_units(units: list[dict[str, Any]], total_value: float) -> list[UnderlyingHolding]:
    underlyings: list[UnderlyingHolding] = []
    parents_with_real_lookthrough: set[str] = set()
    for unit in units:
        if _is_real_lookthrough_type(unit.get("lookthrough_type")):
            parents_with_real_lookthrough.update(_unit_parent_identity_keys(unit))

    for unit in units:
        lookthrough_type = _text(unit.get("lookthrough_type")).upper()
        if lookthrough_type in {"", "MISSING"}:
            continue
        if lookthrough_type == "PRODUCT_TAG" and not (_unit_parent_identity_keys(unit) & parents_with_real_lookthrough):
            continue
        value = _num(unit.get("value_cny"))
        is_residual = lookthrough_type == "PRODUCT_TAG"
        underlying_symbol = "" if is_residual else unit.get("underlying_symbol")
        underlying_currency = "" if is_residual else unit.get("underlying_currency")
        underlying_region = "" if is_residual else unit.get("underlying_region")
        underlying_asset_class = (
            "fund"
            if is_residual
            else unit.get("underlying_asset_class")
            or _infer_stock_asset_class(underlying_symbol, underlying_currency, underlying_region)
            or ""
        )
        raw = {
            "symbol": underlying_symbol,
            "asset_name": "未穿透残差" if is_residual else unit.get("underlying_name"),
            "currency": underlying_currency,
            "asset_type": underlying_asset_class,
            "region": underlying_region,
            "industry": "" if is_residual else unit.get("underlying_industry"),
            "industry_bucket": "" if is_residual else unit.get("industry_bucket"),
            "industry_sub_bucket": "" if is_residual else unit.get("industry_sub_bucket"),
            "theme": "" if is_residual else unit.get("underlying_theme"),
            "theme_exposures": [] if is_residual else unit.get("underlying_theme_exposures"),
            "ai_sub_themes": [] if is_residual else unit.get("underlying_ai_sub_themes"),
            "sw_l1": "" if is_residual else unit.get("sw_l1"),
        }
        asset_type = resolveAssetType(raw)
        industry = {"bucket": UNMAPPED_INDUSTRY, "leaf": None, "reason": "residual_unmapped"} if is_residual else resolveIndustryBucket(raw | {"assetType": asset_type})
        market = resolve_market(raw, asset_type)
        region = resolve_region(raw, market)
        underlyings.append(
            {
                "parentHoldingId": _text(unit.get("holding_symbol") or unit.get("holding_name")),
                "parentName": _text(unit.get("holding_name") or unit.get("holding_symbol")),
                "underlyingSymbol": _text(raw.get("symbol")),
                "underlyingName": _text(raw.get("asset_name")),
                "underlyingWeightInFund": _num(unit.get("underlying_weight")),
                "effectiveWeightInPortfolio": value / total_value if total_value > 0 else 0.0,
                "effectiveMarketValue": value,
                "assetType": asset_type,
                "industryBucket": industry["bucket"] or UNMAPPED_INDUSTRY,
                "industryLeaf": industry["leaf"],
                "industrySubBucket": industry.get("sub_bucket"),
                "themeExposures": [] if is_residual else resolveThemeExposures(raw | {"assetType": asset_type}),
                "aiSubThemes": [] if is_residual else [
                    {
                        "subThemeId": sub_id,
                        "subThemeName": AI_SUB_THEME_DEFINITIONS.get(sub_id, sub_id),
                        "score": score,
                        "role": _role(score),
                    }
                    for sub_id, score in _ai_sub_theme_rules(raw | {"assetType": asset_type})
                ],
                "sw_l1": "" if is_residual else _text(unit.get("sw_l1")),
                "industryBucketSource": _text(unit.get("industry_bucket_source") or industry.get("reason")),
                "market": market,
                "region": region,
                "currency": _currency(raw.get("currency")),
                "account": _text(unit.get("account")),
                "lookthroughType": lookthrough_type,
                "confidence": _text(unit.get("confidence") or "MEDIUM").upper(),
            }
        )
    return underlyings


def build_exposure_analysis_payload(
    *,
    positions: list[dict[str, Any]],
    units: list[dict[str, Any]],
    quality: dict[str, Any],
    total_value: float,
) -> dict[str, Any]:
    enriched_positions: list[dict[str, Any]] = []
    lookthrough_parent_keys: set[str] = set()
    lookthrough_parent_unit_by_key: dict[str, dict[str, Any]] = {}
    for unit in units:
        if not _is_real_lookthrough_type(unit.get("lookthrough_type")):
            continue
        unit_keys = _unit_parent_identity_keys(unit)
        lookthrough_parent_keys.update(unit_keys)
        for key in unit_keys:
            lookthrough_parent_unit_by_key.setdefault(key, unit)

    direct_units_by_key: dict[str, dict[str, Any]] = {}
    for unit in units:
        if _is_real_lookthrough_type(unit.get("lookthrough_type")):
            continue
        keys = _identity_keys(
            unit.get("holding_symbol"),
            unit.get("holding_name"),
            unit.get("underlying_symbol"),
            unit.get("underlying_name"),
        )
        for key in keys:
            if key:
                direct_units_by_key.setdefault(key, unit)
    for position in positions:
        symbol = _text(position.get("symbol") or position.get("asset_id") or position.get("asset_name"))
        name = _text(position.get("asset_name") or position.get("name") or symbol)
        item = dict(position)
        position_keys = _position_identity_keys(position)
        item["has_lookthrough"] = bool(position_keys & lookthrough_parent_keys)
        lookthrough_parent_unit = None
        for key in position_keys:
            lookthrough_parent_unit = lookthrough_parent_unit_by_key.get(key)
            if lookthrough_parent_unit:
                break
        if lookthrough_parent_unit:
            if lookthrough_parent_unit.get("holding_asset_class"):
                item["asset_type"] = lookthrough_parent_unit.get("holding_asset_class")
                item["asset_class"] = lookthrough_parent_unit.get("holding_asset_class")
            if lookthrough_parent_unit.get("account") and not item.get("account"):
                item["account"] = lookthrough_parent_unit.get("account")
        direct_unit = None
        for key in _identity_keys(symbol, name):
            direct_unit = direct_units_by_key.get(key)
            if direct_unit:
                break
        if direct_unit:
            if direct_unit.get("holding_asset_class"):
                item["asset_type"] = direct_unit.get("holding_asset_class")
                item["asset_class"] = direct_unit.get("holding_asset_class")
            if direct_unit.get("account") and not item.get("account"):
                item["account"] = direct_unit.get("account")
            if direct_unit.get("sw_l1"):
                item["sw_l1"] = direct_unit.get("sw_l1")
            if direct_unit.get("industry_bucket_source"):
                item["industry_bucket_source"] = direct_unit.get("industry_bucket_source")
            if direct_unit.get("underlying_industry") and not item.get("industry"):
                item["industry"] = direct_unit.get("underlying_industry")
            if direct_unit.get("underlying_theme") and not item.get("theme"):
                item["theme"] = direct_unit.get("underlying_theme")
            if direct_unit.get("underlying_asset_class") and not item.get("asset_class"):
                item["asset_class"] = direct_unit.get("underlying_asset_class")
        enriched_positions.append(item)

    normalized = normalizeHoldings(enriched_positions)
    underlyings = build_engine_underlyings_from_units(units, total_value)
    exposures = {
        "asset_type": calculateAssetTypeExposure(normalized),
        "industry": calculateIndustryExposure(normalized, underlyings),
        "theme": calculateThemeExposure(normalized, underlyings),
        "market": calculateMarketExposure(normalized, underlyings),
        "region": calculateRegionExposure(normalized, underlyings),
        "currency": calculateCurrencyExposure(normalized),
    }
    exposures["underlying_exposure"] = calculateUnderlyingExposure(normalized, underlyings)
    exposures["underlying_asset"] = exposures["underlying_exposure"]

    ai_subthemes = _ai_sub_theme_exposure_from_holdings(normalized, sum(h["marketValue"] for h in normalized), underlyings)
    for row in exposures["theme"]:
        if row["name"] == "AI / 算力":
            row["sub_themes"] = ai_subthemes
            row["detail_title"] = "AI 算力链详情"
            row["dedupe_rule"] = "父主题按每只持仓最高 AI 子主题分数去重，子主题暴露允许重叠。"

    validation = {
        "industry": validateIndustryBuckets(exposures["industry"], strict=False),
        "theme": validateThemeExposures(exposures["theme"], strict=False),
        "totals": validateExposureTotals(exposures),
        "lookthrough": validateLookthroughCoverage(quality),
    }
    return {
        "normalized_holdings": normalized,
        "underlying_holdings": underlyings,
        "exposures": exposures,
        "risk_summary": calculateRiskSummary(exposures, quality),
        "validation": validation,
        "model_version": "exposure_engine_v1",
    }
