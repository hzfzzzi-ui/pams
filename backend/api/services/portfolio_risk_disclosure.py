"""Seven-surface portfolio risk disclosure for the investment page."""
from __future__ import annotations

from typing import Any

import psycopg2.extras

from services.lookthrough_state import build_lookthrough_payload
from services.portfolio_state import get_conn
from services.risk_state import build_risk_state


DISCLOSURE_CATEGORIES = [
    {
        "id": "market_direction",
        "name": "市场方向风险",
        "description": "股票、利率、信用、商品和汇率等组合价格波动来源。",
    },
    {
        "id": "region_currency",
        "name": "地域与币种风险",
        "description": "按地区和计价币种穿透，识别单一市场或汇率集中。",
    },
    {
        "id": "industry_theme",
        "name": "行业与主题风险",
        "description": "行业桶和主题标签是多标签披露，不作为目标配置加总。",
    },
    {
        "id": "concentration",
        "name": "集中度风险",
        "description": "单一标的、ESOP、行业、国家和账户集中度。",
    },
    {
        "id": "liquidity_valuation",
        "name": "流动性与估值风险",
        "description": "非上市股权、场外基金、估值滞后和现金可用性。",
    },
    {
        "id": "leverage_financing",
        "name": "杠杆与融资风险",
        "description": "融资成本、负现金、总风险敞口和压力损失。",
    },
    {
        "id": "factor_style",
        "name": "因子风格风险",
        "description": "成长、价值、动量、高波动、红利、质量等风格暴露。",
    },
]

TONE_RANK = {"ok": 0, "info": 1, "warn": 2, "risk": 3}
TONE_SCORE = {"ok": 0, "info": 35, "warn": 70, "risk": 100}
CATEGORY_WEIGHTS = {
    "market_direction": 15,
    "region_currency": 10,
    "industry_theme": 15,
    "concentration": 20,
    "liquidity_valuation": 15,
    "leverage_financing": 20,
    "factor_style": 5,
}
CONFIG_LINKS = {
    "market_direction": "目标配置规则 > 第一层配置 / 股票",
    "region_currency": "目标配置规则 > 股票 > 地域配置",
    "industry_theme": "风险披露，不作为目标配置加总",
    "concentration": "目标配置规则 > 股票 / 非上市权益",
    "liquidity_valuation": "目标配置规则 > 现金短债 / 非上市权益",
    "leverage_financing": "目标配置规则 > 融资负债",
    "factor_style": "风险披露，不作为目标配置加总",
}


def _n(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed else 0.0


def _pct(value: float) -> str:
    return f"{_n(value) * 100:.1f}%"


def _money(value: float) -> str:
    return f"¥{abs(_n(value)):,.0f}"


def _tone(value: float, *, warn: float = 0.20, risk: float = 0.35) -> str:
    ratio = abs(_n(value))
    if ratio >= risk:
        return "risk"
    if ratio >= warn:
        return "warn"
    return "ok"


def _max_tone(items: list[dict[str, Any]]) -> str:
    if not items:
        return "ok"
    return max((str(item.get("tone") or "ok") for item in items), key=lambda item: TONE_RANK.get(item, 0))


def _score_tone(score: float) -> str:
    value = _n(score)
    if value >= 75:
        return "risk"
    if value >= 55:
        return "warn"
    if value >= 25:
        return "info"
    return "ok"


def _score_label(tone: str) -> str:
    return {
        "risk": "高风险",
        "warn": "偏高",
        "info": "观察",
        "ok": "正常",
    }.get(tone, "观察")


def _threshold_label(value: float | None) -> str:
    return _pct(value) if value is not None else "按该项风控阈值"


def _item_thresholds(item: dict[str, Any]) -> tuple[float | None, float | None]:
    thresholds = item.get("thresholds") or {}
    warn = thresholds.get("warn")
    risk = thresholds.get("risk")
    return (
        _n(warn) if warn is not None else None,
        _n(risk) if risk is not None else None,
    )


def _rule_conditions(item: dict[str, Any]) -> tuple[str, str, str]:
    warn, risk = _item_thresholds(item)
    if warn is None or risk is None:
        return (
            "低于该项风控阈值即满足",
            "接近或超过观察线即进入观察",
            "超过风险线或人工风控等级为红色即不满足",
        )
    return (
        f"当前值 < {_threshold_label(warn)}",
        f"{_threshold_label(warn)} <= 当前值 < {_threshold_label(risk)}",
        f"当前值 >= {_threshold_label(risk)}",
    )


def _rule_template(cat_id: str, item: dict[str, Any]) -> dict[str, str]:
    item_id = str(item.get("id") or "")
    name = str(item.get("name") or "")
    if cat_id == "market_direction":
        if item_id in {"equity_price", "equity_exposure"}:
            return {
                "formula": "权益资产市值 / 组合净资产",
                "numerator": "股票、权益基金底层、ETF权益部分、ESOP等权益价格敏感资产",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "股票 / 非上市权益",
                "adjustment_direction": "降低权益敞口，或用新增资金补现金短债、债券和低相关资产。",
            }
        if item_id == "interest_credit":
            return {
                "formula": "利率/信用资产市值 / 组合净资产",
                "numerator": "债券、货币基金、现金短债和利率型理财资产",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "债券 / 现金短债",
                "adjustment_direction": "按目标配置补足债券或现金短债，不把该项当作权益风险。",
            }
        if item_id == "commodity_price":
            return {
                "formula": "商品敏感资产市值 / 组合净资产",
                "numerator": "黄金、贵金属、资源周期主题和商品相关股票/基金",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "风险披露 > 商品/周期主题",
                "adjustment_direction": "降低贵金属和周期资源来源，或用新增资金配置非商品相关资产。",
            }
        if item_id == "fx_price":
            return {
                "formula": "非人民币资产净敞口 / 组合净资产",
                "numerator": "USD、HKD、KRW等非CNY计价资产折人民币后的净市值",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "地域与币种风险",
                "adjustment_direction": "按目标币种和地区配置补低占比资产，必要时保留对应币种现金。",
            }
    if cat_id == "region_currency":
        is_currency = item_id.startswith("currency:")
        return {
            "formula": f"{'对应币种' if is_currency else '对应地区'}资产市值 / 组合净资产",
            "numerator": f"穿透后归属{name}的直接持仓、基金/ETF底层和ESOP暴露",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "股票 > 地域配置 / 币种披露",
            "adjustment_direction": "减少单一地区或币种集中，新增资金优先投向低占比地区或保留对应币种现金。",
        }
    if cat_id == "industry_theme":
        is_theme = item_id.startswith("theme:")
        return {
            "formula": f"{'主题标签' if is_theme else '行业'}暴露市值 / 组合净资产",
            "numerator": f"穿透后归属{name}的直接持仓和基金/ETF底层；主题可多标签重复披露",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "风险披露 > 行业与主题",
            "adjustment_direction": "降低同一行业/主题的新增投入，优先补非同源、低相关资产。",
        }
    if cat_id == "concentration":
        return {
            "formula": "单一来源或最大集中项市值 / 组合净资产",
            "numerator": f"{name}对应的标的、行业、账户或ESOP集中暴露",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "目标配置规则 > 股票 / 非上市权益",
            "adjustment_direction": "优先压低最大来源标的或行业权重，用新增资金分散到其他资产桶。",
        }
    if cat_id == "liquidity_valuation":
        if item_id == "cash_buffer":
            return {
                "formula": "可立即动用现金和短债 / 组合净资产",
                "numerator": "银行卡现金、证券现金、货币基金和可快速赎回短债",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "现金短债",
                "adjustment_direction": "现金缓冲不足时优先补现金短债；过高时按配置页补目标资产。",
            }
        return {
            "formula": "低流动性或估值滞后资产市值 / 组合净资产",
            "numerator": f"{name}对应的非上市、场外基金、T+1或估值滞后资产",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "非上市权益 / 现金短债",
            "adjustment_direction": "降低低流动性资产占比，或提高现金短债缓冲覆盖赎回和估值滞后风险。",
        }
    if cat_id == "leverage_financing":
        if item_id == "negative_cash_ratio":
            return {
                "formula": "融资负债或负现金绝对值 / 组合净资产",
                "numerator": "所有投资账户负现金、融资余额和待支付融资款",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "融资负债",
                "adjustment_direction": "补足现金或降低融资余额，把负债从现金缓冲池中独立管理。",
            }
        if item_id == "gross_exposure":
            return {
                "formula": "总风险资产市值 / 组合净资产",
                "numerator": "权益、非上市权益、商品敏感资产和其他风险资产总市值",
                "denominator": "组合净资产 NAV",
                "linked_config_rule": "第一层配置 / 融资负债",
                "adjustment_direction": "降低杠杆后风险资产敞口，或把新增资金先补现金短债和低风险资产。",
            }
        return {
            "formula": "压力损失估算 / 组合净资产",
            "numerator": "按风控服务压力场景估算的潜在损失金额",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "融资负债 / 现金短债",
            "adjustment_direction": "降低高波动资产和融资，确保压力损失不超过可承受边界。",
        }
    if cat_id == "factor_style":
        return {
            "formula": "风格因子暴露市值 / 组合净资产",
            "numerator": f"归属{name}风格或主题的直接持仓和基金/ETF底层资产",
            "denominator": "组合净资产 NAV",
            "linked_config_rule": "风险披露 > 因子风格",
            "adjustment_direction": "减少同一风格因子堆叠，新增资金分散到低相关风格或宽基资产。",
        }
    return {
        "formula": "该风险项暴露市值 / 组合净资产",
        "numerator": f"{name}对应的穿透资产市值",
        "denominator": "组合净资产 NAV",
        "linked_config_rule": CONFIG_LINKS.get(cat_id, "风险披露"),
        "adjustment_direction": "将该项暴露降到风险阈值以内，或在配置页确认提高目标上限。",
    }


def _risk_rule(cat_id: str, item: dict[str, Any]) -> dict[str, str]:
    pass_condition, warn_condition, fail_condition = _rule_conditions(item)
    template = _rule_template(cat_id, item)
    return {
        "name": str(item.get("name") or ""),
        "formula": template["formula"],
        "numerator": template["numerator"],
        "denominator": template["denominator"],
        "pass_condition": pass_condition,
        "warn_condition": warn_condition,
        "fail_condition": fail_condition,
        "linked_config_rule": template.get("linked_config_rule") or CONFIG_LINKS.get(cat_id, "风险披露"),
        "adjustment_direction": template["adjustment_direction"],
    }


def _checklist_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ok": 0, "info": 0, "warn": 0, "risk": 0}
    item_scores = []
    for item in items:
        tone = str(item.get("tone") or "ok")
        if tone not in counts:
            tone = "info"
        counts[tone] += 1
        item_scores.append(TONE_SCORE.get(tone, 35))

    total = len(items)
    if total:
        max_score = max(item_scores)
        avg_score = sum(item_scores) / total
        score = round(max_score * 0.55 + avg_score * 0.45)
    else:
        score = 0
    tone = _score_tone(score)

    return {
        "total": total,
        "passed": counts["ok"],
        "watch": counts["info"],
        "warning": counts["warn"],
        "failed": counts["risk"],
        "counts": counts,
        "score": score,
        "score_label": f"{score:.0f}",
        "tone": tone,
        "status_label": _score_label(tone),
    }


def _risk_dashboard(categories: list[dict[str, Any]]) -> dict[str, Any]:
    total_weight = sum(CATEGORY_WEIGHTS.get(str(category.get("id")), 10) for category in categories) or 1
    weighted_score = sum(
        _n((category.get("checklist") or {}).get("score")) * CATEGORY_WEIGHTS.get(str(category.get("id")), 10)
        for category in categories
    ) / total_weight
    score = round(weighted_score)
    tone = _score_tone(score)

    counts = {"ok": 0, "info": 0, "warn": 0, "risk": 0}
    for category in categories:
        category_counts = ((category.get("checklist") or {}).get("counts") or {})
        for key in counts:
            counts[key] += int(_n(category_counts.get(key)))

    top_categories = sorted(
        categories,
        key=lambda category: (
            _n((category.get("checklist") or {}).get("score")),
            TONE_RANK.get(str(category.get("tone") or "ok"), 0),
        ),
        reverse=True,
    )[:3]
    top_items = sorted(
        [item for category in categories for item in category.get("items", [])],
        key=lambda item: (TONE_RANK.get(str(item.get("tone")), 0), abs(_n(item.get("weight")))),
        reverse=True,
    )[:4]

    leading = top_categories[0]["name"] if top_categories else "暂无"
    return {
        "score": score,
        "score_label": f"{score:.0f}",
        "max_score": 100,
        "status_tone": tone,
        "status_label": _score_label(tone),
        "summary": f"{leading}是当前最需要检查的风险面。",
        "checklist_total": len(categories),
        "item_total": sum(_n((category.get("checklist") or {}).get("total")) for category in categories),
        "counts": counts,
        "category_scores": [
            {
                "id": category.get("id"),
                "name": category.get("name"),
                "score": (category.get("checklist") or {}).get("score", 0),
                "tone": (category.get("checklist") or {}).get("tone", "ok"),
                "status_label": (category.get("checklist") or {}).get("status_label", "正常"),
            }
            for category in categories
        ],
        "top_risks": [
            {
                "category_id": category.get("id"),
                "name": category.get("name"),
                "score": (category.get("checklist") or {}).get("score", 0),
                "status_label": (category.get("checklist") or {}).get("status_label", "正常"),
                "tone": (category.get("checklist") or {}).get("tone", "ok"),
            }
            for category in top_categories
        ],
        "top_items": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "tone": item.get("tone"),
                "weight_label": item.get("weight_label"),
                "value_label": item.get("value_label"),
            }
            for item in top_items
        ],
    }


def _rows(payload: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    rows = (payload.get("exposures") or {}).get(dimension)
    return rows if isinstance(rows, list) else []


def _label(row: dict[str, Any]) -> str:
    return str(
        row.get("display_name")
        or row.get("name")
        or row.get("label")
        or row.get("bucket")
        or row.get("key")
        or row.get("asset_name")
        or row.get("asset_id")
        or ""
    )


def _value(row: dict[str, Any]) -> float:
    return _n(
        row.get("value_cny")
        or row.get("total_exposure_cny")
        or row.get("market_value_cny")
        or row.get("position_value_cny")
        or row.get("exposure_cny")
        or row.get("amount_cny")
        or row.get("value")
    )


def _weight(row: dict[str, Any], nav: float) -> float:
    explicit = _n(row.get("weight") or row.get("total_weight") or row.get("exposure_ratio") or row.get("position_weight"))
    if explicit > 0:
        return explicit / 100 if explicit > 1 else explicit
    value = _value(row)
    return value / nav if nav > 0 else 0.0


def _source_name(row: dict[str, Any]) -> str:
    return str(
        row.get("name")
        or row.get("source_asset_name")
        or row.get("asset_name")
        or row.get("source_symbol")
        or row.get("asset_id")
        or ""
    )


def _source_value(row: dict[str, Any]) -> float:
    return _n(
        row.get("value_cny")
        or row.get("contribution_to_bucket_cny")
        or row.get("exposure_cny")
        or row.get("position_value_cny")
        or row.get("loss_cny")
    )


def _sources_from_row(row: dict[str, Any], nav: float, *, limit: int = 5) -> list[dict[str, Any]]:
    raw_sources = row.get("main_sources") or row.get("top_sources") or row.get("source_details") or row.get("sources") or []
    sources: list[dict[str, Any]] = []
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        name = _source_name(source)
        value = _source_value(source)
        if not name and value <= 0:
            continue
        weight = _weight(source, nav) if _weight(source, nav) > 0 else (value / nav if nav > 0 else 0.0)
        sources.append({
            "name": name or "未命名来源",
            "value_cny": value,
            "value_label": _money(value),
            "weight": weight,
            "weight_label": _pct(weight),
            "meta": source.get("account") or source.get("lookthrough_type") or source.get("asset_type") or "",
        })
    sources.sort(key=lambda item: -abs(_n(item.get("value_cny"))))
    return sources[:limit]


def _item(
    item_id: str,
    name: str,
    value_cny: float,
    nav: float,
    *,
    weight: float | None = None,
    tone: str | None = None,
    note: str = "",
    sources: list[dict[str, Any]] | None = None,
    unit: str = "ratio",
    warn_threshold: float | None = None,
    risk_threshold: float | None = None,
) -> dict[str, Any]:
    ratio = _n(weight) if weight is not None else (_n(value_cny) / nav if nav > 0 else 0.0)
    return {
        "id": item_id,
        "name": name,
        "value_cny": _n(value_cny),
        "value_label": _money(value_cny),
        "weight": ratio,
        "weight_label": _pct(ratio),
        "tone": tone or _tone(ratio),
        "note": note,
        "unit": unit,
        "thresholds": {
            "warn": warn_threshold,
            "risk": risk_threshold,
        },
        "sources": sources or [],
    }


def _metric_by_key(risk: dict[str, Any], key: str) -> dict[str, Any] | None:
    for row in risk.get("user_metrics") or []:
        if row.get("key") == key:
            return row
    return None


def _metric_item(risk: dict[str, Any], key: str, nav: float, *, name: str | None = None) -> dict[str, Any] | None:
    metric = _metric_by_key(risk, key)
    if not metric:
        return None
    ratio = _n(metric.get("current_value"))
    return _item(
        key,
        name or str(metric.get("title") or key),
        ratio * nav,
        nav,
        weight=ratio,
        tone={"red": "risk", "yellow": "warn", "green": "ok"}.get(str(metric.get("level")), "info"),
        note=str(metric.get("note") or ""),
        sources=_sources_from_row({"sources": metric.get("sources") or []}, nav),
    )


def _exposure_items(
    lookthrough: dict[str, Any],
    dimension: str,
    nav: float,
    *,
    names: list[str] | None = None,
    limit: int = 5,
    tone_warn: float = 0.20,
    tone_risk: float = 0.35,
) -> list[dict[str, Any]]:
    selected = []
    for row in _rows(lookthrough, dimension):
        label = _label(row)
        if names and not any(name == label or name in label for name in names):
            continue
        weight = _weight(row, nav)
        selected.append(_item(
            f"{dimension}:{label}",
            label,
            _value(row),
            nav,
            weight=weight,
            tone=_tone(weight, warn=tone_warn, risk=tone_risk),
            warn_threshold=tone_warn,
            risk_threshold=tone_risk,
            sources=_sources_from_row(row, nav),
        ))
    selected.sort(key=lambda item: -abs(_n(item.get("value_cny"))))
    return selected[:limit]


def _combined_exposure_item(
    lookthrough: dict[str, Any],
    dimension: str,
    names: list[str],
    nav: float,
    *,
    item_id: str,
    label: str,
    tone_warn: float = 0.20,
    tone_risk: float = 0.35,
) -> dict[str, Any]:
    matched = _exposure_items(lookthrough, dimension, nav, names=names, limit=20)
    value = sum(_n(row.get("value_cny")) for row in matched)
    sources = []
    for row in matched:
        sources.extend(row.get("sources") or [{
            "name": row["name"],
            "value_cny": row["value_cny"],
            "value_label": row["value_label"],
            "weight": row["weight"],
            "weight_label": row["weight_label"],
            "meta": dimension,
        }])
    ratio = value / nav if nav > 0 else 0.0
    sources = sorted(sources, key=lambda item: -abs(_n(item.get("value_cny"))))[:5]
    return _item(
        item_id,
        label,
        value,
        nav,
        weight=ratio,
        tone=_tone(ratio, warn=tone_warn, risk=tone_risk),
        warn_threshold=tone_warn,
        risk_threshold=tone_risk,
        sources=sources,
    )


def _top_positions(risk: dict[str, Any], nav: float, *, limit: int = 6) -> list[dict[str, Any]]:
    rows = []
    for row in risk.get("positions") or []:
        value = _n(row.get("position_value_cny"))
        if value <= 0:
            continue
        weight = _n(row.get("position_weight")) or (value / nav if nav > 0 else 0.0)
        rows.append(_item(
            f"position:{row.get('asset_id') or row.get('asset_name')}",
            str(row.get("asset_name") or row.get("asset_id") or "未命名持仓"),
            value,
            nav,
            weight=weight,
            tone=_tone(weight, warn=0.08, risk=0.15),
            warn_threshold=0.08,
            risk_threshold=0.15,
            note=str(row.get("asset_type") or ""),
        ))
    rows.sort(key=lambda item: -_n(item.get("weight")))
    return rows[:limit]


def _category(cat_id: str, items: list[dict[str, Any]], *, headline: str = "") -> dict[str, Any]:
    meta = next(row for row in DISCLOSURE_CATEGORIES if row["id"] == cat_id)
    visible_items = [item for item in items if item]
    for item in visible_items:
        item["rule"] = _risk_rule(cat_id, item)
    top = max(visible_items, key=lambda item: abs(_n(item.get("weight"))), default=None)
    checklist = _checklist_summary(visible_items)
    return {
        **meta,
        "tone": checklist["tone"] or _max_tone(visible_items),
        "headline": headline or (f"{top['name']} {top['weight_label']}" if top else "暂无显著暴露"),
        "score": checklist["score"],
        "score_label": checklist["score_label"],
        "checklist": checklist,
        "items": visible_items,
    }


def build_portfolio_config_input_summary(risk: dict[str, Any], lookthrough: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe the read-only inputs that feed target allocation rules."""

    as_of = risk.get("as_of") or lookthrough.get("as_of")
    return [
        {
            "id": "holdings",
            "name": "持仓事实",
            "href": "#holdings-smart-table",
            "role": "配置输入",
            "as_of": as_of,
            "detail": "市值、份数、账户和成本口径。",
        },
        {
            "id": "risk_disclosure",
            "name": "风险披露",
            "href": "#portfolio-risk-disclosure",
            "role": "配置输入",
            "as_of": as_of,
            "detail": "集中度、杠杆、行业主题、币种和流动性暴露。",
        },
    ]


def build_portfolio_risk_disclosure_payload_from_inputs(
    risk: dict[str, Any],
    lookthrough: dict[str, Any],
    *,
    base_currency: str = "CNY",
) -> dict[str, Any]:
    """Build the investment page risk disclosure payload from current portfolio facts."""

    nav = _n((risk.get("summary") or {}).get("total_net_worth_cny")) or _n(lookthrough.get("total_market_value_cny"))
    asset_type = _rows(lookthrough, "asset_type")
    non_cny_currency = [
        row for row in _rows(lookthrough, "currency")
        if _label(row).upper() not in {"CNY", "人民币", "CNH"}
    ]
    non_cny_value = sum(_value(row) for row in non_cny_currency)

    equity_item = _metric_item(risk, "equity_exposure", nav, name="股票/权益价格")
    if not equity_item:
        equity_item = _combined_exposure_item(
            lookthrough,
            "asset_type",
            ["非上市股权", "A股个股", "港股个股", "美股个股", "ETF / 基金"],
            nav,
            item_id="equity_price",
            label="股票/权益价格",
            tone_warn=0.55,
            tone_risk=0.80,
        )
    market_items = [
        equity_item,
        _combined_exposure_item(
            lookthrough,
            "asset_type",
            ["债券", "货币基金", "短债", "现金"],
            nav,
            item_id="interest_credit",
            label="利率/信用价格",
            tone_warn=0.20,
            tone_risk=0.35,
        ),
        _combined_exposure_item(
            lookthrough,
            "theme",
            ["贵金属与铜", "黄金", "工业金属", "周期资源"],
            nav,
            item_id="commodity_price",
            label="商品价格敏感",
            tone_warn=0.10,
            tone_risk=0.20,
        ),
        _item(
            "fx_price",
            "汇率价格",
            non_cny_value,
            nav,
            tone=_tone(non_cny_value / nav if nav > 0 else 0.0, warn=0.20, risk=0.40),
            warn_threshold=0.20,
            risk_threshold=0.40,
            sources=[
                {
                    "name": _label(row),
                    "value_cny": _value(row),
                    "value_label": _money(_value(row)),
                    "weight": _weight(row, nav),
                    "weight_label": _pct(_weight(row, nav)),
                    "meta": "currency",
                }
                for row in non_cny_currency[:5]
            ],
        ),
    ]

    region_items = _exposure_items(lookthrough, "region", nav, limit=4, tone_warn=0.25, tone_risk=0.45)
    currency_items = _exposure_items(lookthrough, "currency", nav, limit=4, tone_warn=0.25, tone_risk=0.45)
    industry_items = _exposure_items(lookthrough, "industry", nav, limit=5, tone_warn=0.25, tone_risk=0.40)
    theme_items = _exposure_items(lookthrough, "theme", nav, limit=6, tone_warn=0.18, tone_risk=0.30)

    concentration_items = []
    single_metric = _metric_item(risk, "single_position", nav, name="单一标的")
    industry_metric = _metric_item(risk, "industry_concentration", nav, name="最大行业")
    if single_metric:
        concentration_items.append(single_metric)
    if industry_metric:
        concentration_items.append(industry_metric)
    concentration_items.extend(_top_positions(risk, nav, limit=5))

    nonlisted = _combined_exposure_item(
        lookthrough,
        "asset_type",
        ["非上市股权"],
        nav,
        item_id="illiquid_private_equity",
        label="非上市权益估值",
        tone_warn=0.20,
        tone_risk=0.35,
    )
    fund_like = _combined_exposure_item(
        lookthrough,
        "asset_type",
        ["ETF / 基金", "基金"],
        nav,
        item_id="fund_lookthrough",
        label="基金/ETF 穿透依赖",
        tone_warn=0.20,
        tone_risk=0.40,
    )
    cash_like = _combined_exposure_item(
        lookthrough,
        "asset_type",
        ["现金", "货币基金", "短债"],
        nav,
        item_id="cash_buffer",
        label="现金可用性",
        tone_warn=0.08,
        tone_risk=0.20,
    )
    liquidity_items = [nonlisted, fund_like, cash_like]

    leverage_items = [
        _metric_item(risk, "negative_cash_ratio", nav, name="负现金/融资"),
        _metric_item(risk, "gross_exposure", nav, name="总风险资产敞口"),
        _metric_item(risk, "stress_loss", nav, name="压力损失"),
    ]

    style_rows = risk.get("investment_style_exposure") or []
    style_items = [
        _item(
            f"style:{row.get('label') or row.get('key')}",
            str(row.get("label") or row.get("key") or "风格暴露"),
            _n(row.get("exposure_cny")) or _n(row.get("exposure_ratio")) * nav,
            nav,
            weight=_n(row.get("exposure_ratio")),
            tone={"red": "risk", "yellow": "warn", "green": "ok"}.get(str(row.get("level")), _tone(_n(row.get("exposure_ratio")), warn=0.18, risk=0.30)),
            warn_threshold=0.18,
            risk_threshold=0.30,
            sources=_sources_from_row({"sources": row.get("top_positions") or []}, nav),
        )
        for row in style_rows[:6]
    ]
    if not style_items:
        style_items = theme_items[:4]

    categories = [
        _category("market_direction", [item for item in market_items if item]),
        _category("region_currency", [*region_items, *currency_items]),
        _category("industry_theme", [*industry_items, *theme_items]),
        _category("concentration", concentration_items),
        _category("liquidity_valuation", liquidity_items),
        _category("leverage_financing", [item for item in leverage_items if item]),
        _category("factor_style", style_items),
    ]

    highlights = sorted(
        [item for category in categories for item in category["items"]],
        key=lambda item: (TONE_RANK.get(str(item.get("tone")), 0), abs(_n(item.get("weight")))),
        reverse=True,
    )[:6]
    dashboard = _risk_dashboard(categories)

    return {
        "as_of": risk.get("as_of") or lookthrough.get("as_of"),
        "base_currency": base_currency,
        "nav_cny": nav,
        "nav_label": _money(nav),
        "inputs": build_portfolio_config_input_summary(risk, lookthrough),
        "dashboard": dashboard,
        "categories": categories,
        "highlights": highlights,
        "method": {
            "target_config": "目标配置只使用互斥资产分类做再平衡。",
            "risk_disclosure": "风险披露允许同一持仓进入多个风险标签，不要求加总为 100%。",
        },
    }


def build_portfolio_risk_disclosure_payload(*, base_currency: str = "CNY") -> dict[str, Any]:
    """Build the live /api/portfolio/risk-disclosure payload."""

    risk = build_risk_state()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            lookthrough = build_lookthrough_payload(cur, base_currency=base_currency)
    return build_portfolio_risk_disclosure_payload_from_inputs(risk, lookthrough, base_currency=base_currency)
