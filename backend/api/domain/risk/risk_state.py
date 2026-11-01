"""Risk state assembly service.

Endpoint modules should stay thin. This module owns the current risk page data
assembly until the broader PortfolioState contract is introduced.
"""
from __future__ import annotations

import os
import re
import math
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import psycopg2.extras

from domain.investments.portfolio_state import build_portfolio_state  # noqa: E402
from domain.investments.holdings_view import latest_complete_snapshot_date  # noqa: E402
from domain.investments.portfolio_valuation_state import (  # noqa: E402
    is_published_valuation_fresh_for_date,
    latest_published_portfolio_valuation_state,
    published_valuation_state_meta,
    published_valuation_stale_meta,
)
from domain.risk.core.concentration import (  # noqa: E402
    concentration_exposure,
    load_concentration_map,
    normalize_symbol,
)
from domain.risk.core.config import load_factor_map, load_risk_limits  # noqa: E402
from domain.risk.core.factors import factors_for_position  # noqa: E402
from domain.risk.core.risk_engine import (  # noqa: E402
    account_weight,
    ensure_positions,
    factor_exposure,
    gross_exposure,
    negative_cash_ratio,
    position_weight,
    rebalance_suggestions,
    risk_alerts,
    stress_loss,
    total_nav,
)


INVESTMENT_DETAIL_ACCOUNT_TYPES = {"brokerage", "fund_platform"}
SYNTHETIC_ASSET_TYPES_BY_ACCOUNT_TYPE = {
    "funding": "cash",
    "cash_management": "cash",
    "wallet_group": "cash",
    "benefit": "manual_asset",
    "liability": "liability",
    "receivable": "manual_asset",
    "bridge": "cash",
}


def get_conn():
    """Create a PostgreSQL connection using PAMS environment variables."""
    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def _norm(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: _norm(v) for k, v in row.items()} for row in df.to_dict("records")]


def build_risk_latest_payload(cur, *, limit: int) -> dict[str, Any]:
    """Return the legacy /risk/latest risk-event payload."""
    as_of = latest_complete_snapshot_date(cur, view_as_of=_view_as_of())
    if as_of is None:
        return {"as_of": None, "events": [], "rows": []}

    cur.execute(
        """
        SELECT as_of, severity, code, title, details, created_at
        FROM pams_risk_events
        WHERE as_of = %s
        ORDER BY CASE severity WHEN 'crit' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC,
                 created_at DESC
        LIMIT %s
        """,
        (as_of, limit),
    )
    rows = cur.fetchall()
    out = [{key: _norm(value) for key, value in dict(row).items()} for row in rows]
    return {
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "events": out,
        "rows": out,
    }


def _view_as_of() -> str:
    tz_name = os.environ.get("PAMS_TIMEZONE", os.environ.get("TZ", "Asia/Shanghai"))
    try:
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _owner_ratio(account_code: str, limits: dict[str, Any]) -> float:
    data_cfg = limits.get("data", {}) or {}
    ratios = data_cfg.get("account_owner_ratios", {}) or {}
    return float(ratios.get(account_code, data_cfg.get("default_owner_ratio", 1.0)))


def _cn_symbol_keys(value: Any) -> dict[str, Any]:
    raw = normalize_symbol(value)
    if not raw:
        return {"keys": [], "ts_code": None, "internal_symbol": None}

    match = re.search(r"(\d{6})", raw)
    if not match:
        return {"keys": [raw], "ts_code": None, "internal_symbol": None}

    digits = match.group(1)
    exchange = None
    if raw.endswith(".SH") or raw.startswith("SH") or digits.startswith("6"):
        exchange = "SH"
    elif raw.endswith(".SZ") or raw.startswith("SZ") or digits.startswith(("0", "3")):
        exchange = "SZ"

    ts_code = f"{digits}.{exchange}" if exchange else None
    internal_symbol = f"{exchange.lower()}{digits}" if exchange else None
    keys = [raw, digits]
    if ts_code:
        keys.append(ts_code)
    if internal_symbol:
        keys.append(internal_symbol)
    return {"keys": list(dict.fromkeys(keys)), "ts_code": ts_code, "internal_symbol": internal_symbol}


def _put_industry(lookup: dict[str, str], keys: list[str], industry: Any) -> None:
    label = str(industry or "").strip()
    if not label:
        return
    for key in keys:
        norm = normalize_symbol(key)
        if norm:
            lookup[norm] = label
            lookup[norm.lower()] = label


def _load_db_industry_lookup(positions: pd.DataFrame) -> dict[str, str]:
    lookup: dict[str, str] = {}
    if positions.empty:
        return lookup

    ts_to_keys: dict[str, list[str]] = {}
    snapshot_to_keys: dict[str, list[str]] = {}
    for _, row in positions.iterrows():
        for value in [row.get("symbol"), row.get("asset_id")]:
            parsed = _cn_symbol_keys(value)
            if parsed["ts_code"]:
                ts_to_keys.setdefault(parsed["ts_code"], []).extend(parsed["keys"])
            if parsed["internal_symbol"]:
                snapshot_to_keys.setdefault(parsed["internal_symbol"], []).extend(parsed["keys"])

    ts_codes = sorted(ts_to_keys)
    snapshot_symbols = sorted(snapshot_to_keys)
    if not ts_codes and not snapshot_symbols:
        return lookup

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if ts_codes:
                    cur.execute(
                        """
                        SELECT DISTINCT ON (ts_code) ts_code, industry_l2, industry_l1
                        FROM pit_industry_history
                        WHERE ts_code = ANY(%s::text[])
                          AND COALESCE(industry_l2, industry_l1, '') <> ''
                        ORDER BY
                          ts_code,
                          CASE WHEN end_date IS NULL THEN 0 ELSE 1 END,
                          CASE WHEN standard IN ('SW2021', 'SW') THEN 0 ELSE 1 END,
                          start_date DESC
                        """,
                        (ts_codes,),
                    )
                    for row in cur.fetchall():
                        _put_industry(
                            lookup,
                            ts_to_keys.get(str(row["ts_code"]), []),
                            row.get("industry_l2") or row.get("industry_l1"),
                        )

                    missing = [ts for ts in ts_codes if not any(normalize_symbol(k) in lookup for k in ts_to_keys.get(ts, []))]
                    if missing:
                        cur.execute(
                            """
                            SELECT ts_code, industry
                            FROM pit_security_master
                            WHERE ts_code = ANY(%s::text[])
                              AND COALESCE(industry, '') <> ''
                            """,
                            (missing,),
                        )
                        for row in cur.fetchall():
                            _put_industry(lookup, ts_to_keys.get(str(row["ts_code"]), []), row.get("industry"))

                if snapshot_symbols:
                    cur.execute(
                        """
                        SELECT DISTINCT ON (symbol) symbol, industry_code
                        FROM pams_fundamental_snapshot
                        WHERE symbol = ANY(%s::text[])
                          AND COALESCE(industry_code, '') <> ''
                        ORDER BY symbol, as_of DESC
                        """,
                        (snapshot_symbols,),
                    )
                    for row in cur.fetchall():
                        _put_industry(
                            lookup,
                            snapshot_to_keys.get(str(row["symbol"]), []),
                            row.get("industry_code"),
                        )
    except Exception:
        return lookup

    return lookup


STYLE_EXPOSURE_GROUPS = {
    "investment_style": {
        "label": "投资风格",
        "tags": {
            "成长",
            "价值",
            "大盘",
            "中盘",
            "中小盘",
            "科技成长",
            "周期资源",
            "周期制造",
            "周期运输",
            "周期金融",
            "半导体周期",
            "农业消费",
            "防守收益",
            "现金增强",
            "AI链",
            "黄金/贵金属避险",
        },
    },
    "region": {
        "label": "区域",
        "tags": {
            "美股权益",
            "港股权益",
            "韩股权益",
            "海外科技权益",
            "海外权益",
        },
    },
    "currency": {
        "label": "币种",
        "tags": {
            "人民币资产",
            "美元资产",
            "港币资产",
            "韩元资产",
            "墨西哥比索资产",
        },
    },
    "special": {
        "label": "特殊风险",
        "tags": {
            "非流动单一公司",
            "单一公司风险",
            "非标资产",
        },
    },
}
CONCENTRATION_KIND_LABELS = {
    "industry": "行业",
    "style": "风格",
    "investment_style": "投资风格",
    "region": "区域",
    "currency": "币种",
    "region_currency": "区域/币种",
    "special": "特殊风险",
}
CONCENTRATION_ALERT_KINDS = ("industry", "investment_style", "region", "currency", "special")


def _concentration_thresholds(limits: dict[str, Any], kind: str) -> dict[str, float]:
    concentration_cfg = (limits.get("limits", {}) or {}).get("concentration", {}) or {}
    threshold_kind = kind if kind in concentration_cfg else ("style" if kind != "industry" else "industry")
    cfg = concentration_cfg.get(threshold_kind, {}) or {}
    defaults = {
        "industry": {"yellow": 0.30, "red": 0.40},
        "investment_style": {"yellow": 0.45, "red": 0.60},
        "style": {"yellow": 0.45, "red": 0.60},
        "region": {"yellow": 0.75, "red": 0.90},
        "currency": {"yellow": 0.75, "red": 0.90},
        "region_currency": {"yellow": 0.75, "red": 0.90},
        "special": {"yellow": 0.30, "red": 0.45},
    }
    fallback = defaults.get(threshold_kind, {"yellow": 0.30, "red": 0.40})
    return {
        "yellow": float(cfg.get("yellow", fallback["yellow"])),
        "red": float(cfg.get("red", fallback["red"])),
    }


def _risk_level(value: float, thresholds: dict[str, float]) -> str:
    ratio = abs(float(value or 0.0))
    if ratio >= thresholds["red"]:
        return "red"
    if ratio >= thresholds["yellow"]:
        return "yellow"
    return "green"


def _annotate_concentration(rows: list[dict[str, Any]], *, kind: str, limits: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = _concentration_thresholds(limits, kind)
    annotated: list[dict[str, Any]] = []
    for row in rows:
        ratio = float(row.get("exposure_ratio") or 0.0)
        annotated.append(
            {
                **row,
                "kind": kind,
                "level": _risk_level(ratio, thresholds),
                "yellow_threshold": thresholds["yellow"],
                "red_threshold": thresholds["red"],
            }
        )
    return annotated


def _style_exposure_group(label: Any) -> str:
    text = str(label or "").strip()
    for group_key, group in STYLE_EXPOSURE_GROUPS.items():
        if text in group["tags"]:
            return group_key
    if "单一公司" in text or "非流动" in text:
        return "special"
    if "资产" in text or text.startswith(("美元", "人民币", "港币", "韩元", "墨西哥比索")):
        return "currency"
    if text.startswith(("美股", "港股", "韩股", "海外")):
        return "region"
    return "investment_style"


def _split_style_exposure_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = {key: [] for key in STYLE_EXPOSURE_GROUPS}
    for row in rows:
        group_key = _style_exposure_group(row.get("label"))
        groups[group_key].append({**row, "kind": group_key})
    return groups


SPECIAL_ASSET_TYPE_LABELS = {
    "非上市股权",
    "房地产 / 实物资产",
    "应收款 / 待到账",
    "其他资产",
}


def _exposure_source_value(source: dict[str, Any]) -> float:
    for key in ("contribution_to_bucket_cny", "value_cny", "exposure_cny", "contribution_cny"):
        if source.get(key) not in (None, ""):
            return abs(_safe_float(source.get(key)))
    return 0.0


def _exposure_top_positions(row: dict[str, Any], nav: float, *, limit: int = 4) -> list[dict[str, Any]]:
    sources = row.get("source_details") or row.get("top_sources") or row.get("main_sources") or []
    out: list[dict[str, Any]] = []
    for source in sources[:limit]:
        value = _exposure_source_value(source)
        name = (
            source.get("source_asset_name")
            or source.get("asset_name")
            or source.get("underlying_name")
            or source.get("name")
            or source.get("source_symbol")
            or source.get("asset_id")
            or ""
        )
        symbol = source.get("source_symbol") or source.get("asset_id") or source.get("underlying_symbol") or name
        out.append(
            {
                "asset_id": str(symbol or ""),
                "asset_name": str(name or symbol or ""),
                "exposure_cny": value,
                "exposure_ratio": value / nav if nav > 0 else 0.0,
                "weight": _safe_float(source.get("weight")),
                "source": source.get("source"),
                "lookthrough_type": source.get("lookthrough_type"),
            }
        )
    return out


def _exposure_rows_to_concentration(
    rows: list[dict[str, Any]],
    *,
    nav: float,
    kind: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        label = str(row.get("display_name") or row.get("name") or row.get("label") or "").strip()
        if not label:
            continue
        exposure = abs(_safe_float(row.get("value_cny", row.get("total_exposure_cny", row.get("exposure_cny")))))
        top_positions = _exposure_top_positions(row, nav)
        out.append(
            {
                "label": label,
                "kind": kind,
                "exposure_cny": exposure,
                "exposure_ratio": exposure / nav if nav > 0 else 0.0,
                "position_count": int(row.get("source_count") or len(top_positions) or 0),
                "top_positions": top_positions,
                "top_position_names": "、".join(p["asset_name"] for p in top_positions if p.get("asset_name")),
                "source": "exposure_engine",
                "raw_weight": _safe_float(row.get("weight")),
                "direct_value_cny": _safe_float(row.get("direct_value_cny")),
                "indirect_value_cny": _safe_float(row.get("indirect_value_cny")),
            }
        )
    return sorted(out, key=lambda r: abs(float(r.get("exposure_cny") or 0.0)), reverse=True)[:limit]


def _uncovered_quality_concentration_row(quality: dict[str, Any], nav: float) -> dict[str, Any] | None:
    uncovered = abs(_safe_float(quality.get("uncovered_value_cny")))
    if uncovered <= 0:
        return None
    top_positions = []
    for item in (quality.get("low_confidence_assets") or quality.get("items") or [])[:4]:
        value = abs(_safe_float(item.get("uncovered_value") or item.get("market_value_cny") or item.get("value_cny")))
        top_positions.append(
            {
                "asset_id": str(item.get("symbol") or item.get("asset_id") or item.get("asset_name") or ""),
                "asset_name": str(item.get("asset_name") or item.get("name") or item.get("symbol") or ""),
                "exposure_cny": value,
                "exposure_ratio": value / nav if nav > 0 else 0.0,
                "weight": _safe_float(item.get("weight")),
                "source": item.get("quality_status") or "lookthrough_quality",
            }
        )
    return {
        "label": "未穿透基金",
        "kind": "special",
        "exposure_cny": uncovered,
        "exposure_ratio": uncovered / nav if nav > 0 else 0.0,
        "position_count": len(top_positions),
        "top_positions": top_positions,
        "top_position_names": "、".join(p["asset_name"] for p in top_positions if p.get("asset_name")),
        "source": "exposure_engine_quality",
    }


def _special_concentration_from_exposure_payload(exposures: dict[str, list[dict[str, Any]]], quality: dict[str, Any], nav: float) -> list[dict[str, Any]]:
    asset_type_rows = [
        row
        for row in exposures.get("asset_type", []) or []
        if str(row.get("name") or row.get("display_name") or "") in SPECIAL_ASSET_TYPE_LABELS
    ]
    rows = _exposure_rows_to_concentration(asset_type_rows, nav=nav, kind="special")
    uncovered = _uncovered_quality_concentration_row(quality, nav)
    if uncovered:
        rows.append(uncovered)
    return sorted(rows, key=lambda r: abs(float(r.get("exposure_cny") or 0.0)), reverse=True)


def _concentration_from_exposure_payload(
    payload: dict[str, Any] | None,
    *,
    nav: float,
    limits: dict[str, Any],
) -> dict[str, Any] | None:
    """Adapt exposure_engine output to the risk concentration payload shape."""

    if not payload or payload.get("status") not in {"ok", "empty"}:
        return None
    exposures = payload.get("exposures") or {}
    if not any(exposures.get(key) for key in ("industry", "theme", "region", "currency", "asset_type")):
        return None

    industry = _annotate_concentration(
        _exposure_rows_to_concentration(exposures.get("industry", []), nav=nav, kind="industry"),
        kind="industry",
        limits=limits,
    )
    investment_style = _annotate_concentration(
        _exposure_rows_to_concentration(exposures.get("theme", []), nav=nav, kind="investment_style"),
        kind="investment_style",
        limits=limits,
    )
    region = _annotate_concentration(
        _exposure_rows_to_concentration(exposures.get("region", []), nav=nav, kind="region"),
        kind="region",
        limits=limits,
    )
    currency = _annotate_concentration(
        _exposure_rows_to_concentration(exposures.get("currency", []), nav=nav, kind="currency"),
        kind="currency",
        limits=limits,
    )
    special = _annotate_concentration(
        _special_concentration_from_exposure_payload(exposures, payload.get("quality") or {}, nav),
        kind="special",
        limits=limits,
    )
    style_groups = {
        "investment_style": investment_style,
        "region": region,
        "currency": currency,
        "special": special,
    }
    return {
        "source": "exposure_engine",
        "industry": industry,
        "style": investment_style,
        "style_groups": style_groups,
        "quality": payload.get("quality") or {},
        "validation": payload.get("validation") or {},
        "model_version": payload.get("model_version"),
    }


def _load_latest_exposure_payload(base_currency: str = "CNY") -> dict[str, Any] | None:
    try:
        from domain.assets.lookthrough_state import build_lookthrough_payload

        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return build_lookthrough_payload(cur, base_currency=base_currency)
    except Exception as exc:
        return {"status": "error", "error": str(exc), "exposures": {}}


def _concentration_alerts(concentration: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for kind in CONCENTRATION_ALERT_KINDS:
        for row in concentration.get(kind, []):
            if row.get("level") == "green":
                continue
            label = str(row.get("label") or "未分类")
            ratio = float(row.get("exposure_ratio") or 0.0)
            alerts.append(
                {
                    "level": row["level"],
                    "rule": f"{kind}_concentration:{label}",
                    "metric": ratio,
                    "threshold": row.get("red_threshold") if row["level"] == "red" else row.get("yellow_threshold"),
                    "message": f"{CONCENTRATION_KIND_LABELS[kind]}「{label}」集中度为 {ratio:.2%}",
                }
            )
    return alerts


def _concentration_rebalance(concentration: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for kind in CONCENTRATION_ALERT_KINDS:
        for row in concentration.get(kind, []):
            if row.get("level") == "green":
                continue
            label = str(row.get("label") or "未分类")
            ratio = float(row.get("exposure_ratio") or 0.0)
            suggestions.append(
                {
                    "level": row["level"],
                    "rule": f"{kind}_concentration:{label}",
                    "action": "reduce" if row["level"] == "red" else "review",
                    "message": f"{CONCENTRATION_KIND_LABELS[kind]}「{label}」集中度为 {ratio:.2%}，优先检查新增资金是否继续流入该方向。",
                }
            )
    return suggestions


def _concentration_rules_payload(limits: dict[str, Any]) -> dict[str, Any]:
    industry = _concentration_thresholds(limits, "industry")
    style = _concentration_thresholds(limits, "style")
    investment_style = _concentration_thresholds(limits, "investment_style")
    region = _concentration_thresholds(limits, "region")
    currency = _concentration_thresholds(limits, "currency")
    special = _concentration_thresholds(limits, "special")
    style_groups = [
        {
            "key": "investment_style",
            "label": "投资风格暴露",
            "yellow": investment_style["yellow"],
            "red": investment_style["red"],
            "method": "多标签口径，观察成长/价值、大小盘、科技成长、周期资源、防守收益、现金增强等投资属性。",
        },
        {
            "key": "region",
            "label": "区域暴露",
            "yellow": region["yellow"],
            "red": region["red"],
            "method": "多标签口径，单独观察 A股、港股、美股、欧股、韩股、海外权益等市场区域暴露。",
        },
        {
            "key": "currency",
            "label": "币种暴露",
            "yellow": currency["yellow"],
            "red": currency["red"],
            "method": "多标签口径，单独观察人民币、美元、港币、瑞典克朗、韩元等币种敞口；币种风险不等同于市场区域风险。",
        },
        {
            "key": "special",
            "label": "特殊风险标签",
            "yellow": special["yellow"],
            "red": special["red"],
            "method": "多标签口径，单独观察单一公司、非流动、非标资产等不能用普通行业分散解释的风险。",
        },
    ]
    return {
        "scope": [
            "当前集中度只看未平仓持仓。",
            "同花顺等非全额个人持有账户按个人权益口径计算。",
            "现金、负债、应收款不进入行业/风格/区域/币种/特殊风险集中度。",
            "建行理财、腾讯理财通等低波产品按低波固收/现金增强处理。",
        ],
        "industry": {
            "label": "二级行业集中度",
            "yellow": industry["yellow"],
            "red": industry["red"],
            "method": "A股个股优先使用申万二级行业；基金/ETF按主题或指数成分代理拆分到二级行业。",
        },
        "style": {
            "label": "多标签风险暴露",
            "yellow": style["yellow"],
            "red": style["red"],
            "method": "原风格标签拆成投资风格、区域、币种、特殊风险四组；组内仍是多标签暴露，合计可超过 100%。",
        },
        "style_groups": style_groups,
        "investment_style": style_groups[0],
        "region": style_groups[1],
        "currency": style_groups[2],
        "special": style_groups[3],
    }


def _asset_contributor(row: dict[str, Any], *, contribution_cny: float, nav: float, note: str = "") -> dict[str, Any]:
    return {
        "asset_id": str(row.get("asset_id") or row.get("symbol") or ""),
        "asset_name": str(row.get("asset_name") or row.get("asset_id") or row.get("symbol") or ""),
        "account_name": str(row.get("account_name") or row.get("account_code") or ""),
        "asset_type": str(row.get("asset_type") or ""),
        "contribution_cny": float(contribution_cny),
        "contribution_ratio": float(contribution_cny) / nav if nav > 0 else 0.0,
        "note": note,
    }


def _top_contributors(items: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    meaningful = [item for item in items if abs(float(item.get("contribution_cny") or 0.0)) >= 1.0]
    source = meaningful if meaningful else items
    return sorted(source, key=lambda x: abs(float(x.get("contribution_cny") or 0.0)), reverse=True)[:limit]


def _concentration_contributors(rule: str, concentration: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    kind, _, label = rule.partition("_concentration:")
    if not label:
        return []
    for row in concentration.get(kind, []):
        if str(row.get("label") or "") != label:
            continue
        contributors = []
        for item in row.get("top_positions") or []:
            contributors.append(
                {
                    "asset_id": str(item.get("asset_id") or ""),
                    "asset_name": str(item.get("asset_name") or item.get("asset_id") or ""),
                    "account_name": "",
                    "asset_type": "",
                    "contribution_cny": float(item.get("exposure_cny") or 0.0),
                    "contribution_ratio": float(item.get("exposure_ratio") or 0.0),
                    "note": f"{label} 暴露",
                }
            )
        return contributors
    return []


def _factor_contributors(df: pd.DataFrame, nav: float, factor_map: dict[str, Any], factor: str) -> list[dict[str, Any]]:
    contributors: list[dict[str, Any]] = []
    for _, row in df.loc[~df["is_liability"]].iterrows():
        weights = factors_for_position(row, factor_map)
        weight = float(weights.get(factor) or 0.0)
        if weight <= 0:
            continue
        value = abs(float(row.get("market_value_cny") or 0.0) * weight)
        if value > 0:
            contributors.append(_asset_contributor(row.to_dict(), contribution_cny=value, nav=nav, note=factor))
    return _top_contributors(contributors)


def _stress_contributors(
    df: pd.DataFrame,
    nav: float,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    scenario_id: str,
) -> list[dict[str, Any]]:
    scenario = (stress_tests or {}).get(scenario_id) or {}
    shocks = scenario.get("shocks", {}) if isinstance(scenario, dict) else {}
    contributors: list[dict[str, Any]] = []
    for _, row in df.loc[~df["is_liability"]].iterrows():
        value_cny = float(row.get("market_value_cny") or 0.0)
        factor_weights = factors_for_position(row, factor_map)
        shock = sum(float(shocks.get(factor, 0.0) or 0.0) * weight for factor, weight in factor_weights.items())
        loss = max(0.0, -(value_cny * shock))
        if loss > 0:
            contributors.append(_asset_contributor(row.to_dict(), contribution_cny=loss, nav=nav, note=scenario_id))
    return _top_contributors(contributors)


def _alert_contributors(
    alert: dict[str, Any],
    positions: pd.DataFrame,
    nav: float,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    concentration: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rule = str(alert.get("rule") or "")
    df = ensure_positions(positions)
    if rule == "negative_cash_ratio":
        rows = [
            _asset_contributor(row.to_dict(), contribution_cny=abs(float(row.get("market_value_cny") or 0.0)), nav=nav, note="负现金")
            for _, row in df.loc[df["is_cash"] & (df["market_value_cny"] < 0)].iterrows()
        ]
        return _top_contributors(rows)
    if rule == "gross_exposure":
        rows = [
            _asset_contributor(row.to_dict(), contribution_cny=abs(float(row.get("market_value_cny") or 0.0)), nav=nav, note="非现金风险资产")
            for _, row in df.loc[~df["is_cash"] & ~df["is_liability"]].iterrows()
        ]
        return _top_contributors(rows)
    if rule.startswith("position_weight:"):
        asset_id = rule.split(":", 1)[1]
        rows = [
            _asset_contributor(row.to_dict(), contribution_cny=abs(float(row.get("market_value_cny") or 0.0)), nav=nav, note="单资产占比")
            for _, row in df.loc[df["asset_id"].astype(str) == asset_id].iterrows()
        ]
        return _top_contributors(rows)
    if rule.startswith("factor_exposure:"):
        return _factor_contributors(df, nav, factor_map, rule.split(":", 1)[1])
    if rule.startswith("stress_loss:"):
        return _stress_contributors(df, nav, factor_map, stress_tests, rule.split(":", 1)[1])
    if "_concentration:" in rule:
        return _concentration_contributors(rule, concentration)
    if rule.startswith("account_weight:"):
        account_code = rule.split(":", 1)[1]
        rows = [
            _asset_contributor(row.to_dict(), contribution_cny=float(row.get("market_value_cny") or 0.0), nav=nav, note="账户资产")
            for _, row in df.loc[df["account_code"].astype(str) == account_code].iterrows()
        ]
        return _top_contributors(rows)
    return []


def _risk_action_category(alert: dict[str, Any]) -> tuple[str, str]:
    level = str(alert.get("level") or "green")
    rule = str(alert.get("rule") or "")
    if level == "green":
        return "info", "信息提示"
    if level == "red" or rule.startswith(("negative_cash_ratio", "gross_exposure", "stress_loss", "position_weight")):
        return "must_handle", "必须处理"
    return "watch", "需要观察"


def _action_label_for_alert(alert: dict[str, Any]) -> str:
    rule = str(alert.get("rule") or "")
    level = str(alert.get("level") or "")
    if rule == "negative_cash_ratio":
        return "优先核对 IBKR/现金账户，降低负现金"
    if rule == "gross_exposure":
        return "检查杠杆和风险资产总敞口"
    if rule.startswith("stress_loss:"):
        return "评估压力情景下的可承受损失"
    if rule.startswith("position_weight:"):
        return "限制新增，评估是否分散单一资产"
    if "_concentration:" in rule:
        return "限制新增资金继续流入该方向"
    if rule.startswith("factor_exposure:"):
        return "检查因子暴露是否符合目标"
    return "复核" if level != "green" else "记录"


def _build_risk_actions(
    alerts: list[dict[str, Any]],
    positions: pd.DataFrame,
    nav: float,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    concentration: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    buckets = {
        "must_handle": {"label": "必须处理", "items": []},
        "watch": {"label": "需要观察", "items": []},
        "info": {"label": "信息提示", "items": []},
    }
    for alert in alerts:
        category, label = _risk_action_category(alert)
        item = {
            **alert,
            "category": category,
            "category_label": label,
            "action": _action_label_for_alert(alert),
            "contributors": _alert_contributors(alert, positions, nav, factor_map, stress_tests, concentration),
        }
        buckets[category]["items"].append(item)
    return {
        "must_handle": buckets["must_handle"],
        "watch": buckets["watch"],
        "info": buckets["info"],
        "summary": {
            "must_handle": len(buckets["must_handle"]["items"]),
            "watch": len(buckets["watch"]["items"]),
            "info": len(buckets["info"]["items"]),
        },
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _task_status(level: str) -> str:
    if level == "red":
        return "BREACH"
    if level == "yellow":
        return "WATCH"
    if level == "green":
        return "OK"
    return "NEEDS_REVIEW"


def _task_severity(level: str, group: str) -> str:
    if group == "data_check":
        return "blue"
    if level in {"red", "yellow"}:
        return level
    return "gray"


def _asset_name_lookup(positions: pd.DataFrame) -> dict[str, str]:
    lookup: dict[str, str] = {}
    if positions.empty:
        return lookup
    for _, row in positions.iterrows():
        asset_id = str(row.get("asset_id") or row.get("symbol") or "")
        if asset_id:
            lookup[asset_id] = str(row.get("asset_name") or asset_id)
    return lookup


def _scenario_title(scenario: str) -> str:
    labels = {
        "equity_global_down_20": "全球权益 -20%",
        "cn_equity_down_25": "中国资产 -25%",
        "liquidity_risk_off": "流动性冲击",
        "usd_down_5": "USD/CNY -5%",
        "crypto_down_50": "Crypto -50%",
    }
    return labels.get(scenario, scenario)


def _factor_title(factor: str) -> str:
    labels = {
        "CN_EQUITY": "中国权益",
        "US_EQUITY": "美国权益",
        "ESOP_SINGLE_COMPANY": "ESOP 单一公司",
        "USD_FX": "美元汇率",
        "GOLD": "黄金",
        "CASH": "现金",
        "OTHER": "其他",
    }
    return labels.get(factor, factor)


def _task_title(alert: dict[str, Any], names: dict[str, str]) -> str:
    rule = str(alert.get("rule") or "")
    if rule == "negative_cash_ratio":
        return "负现金比例超阈值"
    if rule == "gross_exposure":
        return "毛敞口高于目标"
    if rule.startswith("stress_loss:"):
        return f"压力测试：{_scenario_title(rule.split(':', 1)[1])}"
    if rule.startswith("position_weight:"):
        asset_id = rule.split(":", 1)[1]
        return f"单资产集中度：{names.get(asset_id, asset_id)}"
    if rule.startswith("factor_exposure:"):
        return f"风险因子：{_factor_title(rule.split(':', 1)[1])}"
    if "_concentration:" in rule:
        _, _, label = rule.partition(":")
        return f"集中度：{label}"
    if rule == "account_weight":
        return "账户占比"
    if rule == "total_nav":
        return "总净资产"
    return rule or "风险任务"


def _recommendation_for_task(alert: dict[str, Any], group: str) -> str:
    rule = str(alert.get("rule") or "")
    if group == "data_check":
        return "先核准数据口径，再判断是否需要真实处理。"
    if rule == "negative_cash_ratio":
        return "优先核准 IBKR 现金和负现金口径；若确认融资余额真实存在，再评估换汇、补现金或降低融资敞口。"
    if rule == "gross_exposure":
        return "检查是否存在融资、负现金或重复风险暴露；暂缓继续增加高波动资产敞口。"
    if rule.startswith("stress_loss:"):
        return "先确认最大损失来源和可承受损失区间；若压力损失继续扩大，再进入减风险评估。"
    if rule.startswith("position_weight:"):
        return "该资产已超过或接近单资产阈值，暂缓新增资金流入；若继续上升或趋势转弱，再进入减仓评估。"
    if rule.startswith("factor_exposure:") or "_concentration:" in rule:
        return "该项作为观察风险，避免新增资金继续流入同一方向。"
    return "保持观察，必要时补充数据后再处理。"


def _short_recommendation_for_task(alert: dict[str, Any], group: str) -> str:
    rule = str(alert.get("rule") or "")
    if group == "data_check":
        return "建议：先核准数据口径"
    if rule == "negative_cash_ratio":
        return "建议：先核准 IBKR 现金与保证金口径"
    if rule == "gross_exposure":
        return "建议：暂停新增高风险敞口，优先降低融资/补现金"
    if rule == "stress_loss:equity_global_down_20":
        return "建议：查看主要损失来源，评估权益风险预算"
    if rule.startswith("stress_loss:"):
        return "建议：查看压力损失来源"
    if rule.startswith("position_weight:"):
        return "建议：暂停新增该标的，纳入集中度观察"
    if rule.startswith("factor_exposure:") or "_concentration:" in rule:
        return "建议：保持观察，避免继续加码同方向"
    return "建议：先观察，必要时再核准"


def _limit_thresholds(limits: dict[str, Any], section: str, key: str | None = None, fallback: dict[str, float] | None = None) -> dict[str, float]:
    cfg = ((limits.get("limits") or {}).get(section) or {})
    if key is not None:
        cfg = (cfg.get(key) or {})
    fallback = fallback or {"yellow": 0.0, "red": 0.0}
    return {
        "yellow": _safe_float(cfg.get("yellow"), fallback["yellow"]),
        "red": _safe_float(cfg.get("red"), fallback["red"]),
    }


def _position_thresholds(limits: dict[str, Any], asset_type: Any) -> dict[str, float]:
    cfg = ((limits.get("limits") or {}).get("position_weight") or {})
    default = cfg.get("default") or {"yellow": 0.10, "red": 0.15}
    by_type = (cfg.get("by_asset_type") or {}).get(str(asset_type or ""), {}) or {}
    return {
        "yellow": _safe_float(by_type.get("yellow"), _safe_float(default.get("yellow"), 0.10)),
        "red": _safe_float(by_type.get("red"), _safe_float(default.get("red"), 0.15)),
    }


def _factor_thresholds(limits: dict[str, Any], factor: str) -> dict[str, float]:
    cfg = (((limits.get("limits") or {}).get("factor_exposure") or {}).get(factor) or {})
    return {
        "yellow": _safe_float(cfg.get("yellow"), 0.0),
        "red": _safe_float(cfg.get("red"), 0.0),
    }


def _risk_metric_level(value: float, yellow: float | None, red: float | None) -> str:
    if red and value >= red:
        return "red"
    if yellow and value >= yellow:
        return "yellow"
    return "green"


def _worst_level(levels: list[str]) -> str:
    order = {"red": 3, "yellow": 2, "green": 1, "gray": 0}
    return max(levels or ["gray"], key=lambda item: order.get(item, 0))


def _risk_metric_payload(
    *,
    key: str,
    title: str,
    current_value: float,
    yellow_threshold: float | None,
    red_threshold: float | None,
    unit: str = "ratio",
    group: str = "核心指标",
    note: str = "",
    sources: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    yellow = _safe_float(yellow_threshold) if yellow_threshold is not None else None
    red = _safe_float(red_threshold) if red_threshold is not None else None
    current = _safe_float(current_value)
    return {
        "key": key,
        "title": title,
        "group": group,
        "unit": unit,
        "current_value": current,
        "yellow_threshold": yellow,
        "red_threshold": red,
        "level": level or _risk_metric_level(current, yellow, red),
        "utilization_to_red": current / red if red and red > 0 else None,
        "room_to_yellow": yellow - current if yellow is not None else None,
        "note": note,
        "sources": sources or [],
        "components": components or [],
    }


def _factor_row_lookup(factor_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("factor") or ""): row for row in factor_rows}


def _build_user_risk_metrics(
    *,
    limits: dict[str, Any],
    metrics: dict[str, Any],
    factor_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
    stress_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build user-facing risk indicators from configured limits and current state."""

    out: list[dict[str, Any]] = []
    limit_cfg = limits.get("limits") or {}
    factor_lookup = _factor_row_lookup(factor_rows)
    equity_factors = ["CN_EQUITY", "US_EQUITY", "ESOP_SINGLE_COMPANY"]
    equity_components = []
    for factor in equity_factors:
        row = factor_lookup.get(factor) or {}
        thresholds = _factor_thresholds(limits, factor)
        value = _safe_float(row.get("abs_exposure_ratio", row.get("exposure_ratio")))
        equity_components.append(
            {
                "key": factor,
                "label": _factor_title(factor),
                "current_value": value,
                "yellow_threshold": thresholds["yellow"],
                "red_threshold": thresholds["red"],
                "level": _risk_metric_level(value, thresholds["yellow"], thresholds["red"]),
                "exposure_cny": _safe_float(row.get("exposure_cny")),
            }
        )
    out.append(
        _risk_metric_payload(
            key="equity_exposure",
            title="股票 / 权益暴露",
            current_value=sum(item["current_value"] for item in equity_components),
            yellow_threshold=None,
            red_threshold=None,
            group="暴露",
            note="按中国权益、美国权益、ESOP 单一公司三个已配置因子分项观察，不把多标签简单当作单一阈值。",
            components=equity_components,
            level=_worst_level([item["level"] for item in equity_components]),
        )
    )

    gross_thresholds = {
        "yellow": _safe_float((limit_cfg.get("gross_exposure") or {}).get("yellow"), 0.95),
        "red": _safe_float((limit_cfg.get("gross_exposure") or {}).get("red"), 1.10),
    }
    out.append(
        _risk_metric_payload(
            key="gross_exposure",
            title="总风险资产敞口",
            current_value=_safe_float(metrics.get("gross_exposure_ratio")),
            yellow_threshold=gross_thresholds["yellow"],
            red_threshold=gross_thresholds["red"],
            group="暴露",
            note="衡量投资资产相对净资产的总暴露，接近黄色阈值时应暂停继续加杠杆或加仓高波动资产。",
        )
    )

    negative_cash_thresholds = {
        "yellow": _safe_float((limit_cfg.get("negative_cash_ratio") or {}).get("yellow"), 0.01),
        "red": _safe_float((limit_cfg.get("negative_cash_ratio") or {}).get("red"), 0.03),
    }
    out.append(
        _risk_metric_payload(
            key="negative_cash_ratio",
            title="负现金 / 融资占比",
            current_value=_safe_float(metrics.get("negative_cash_ratio")),
            yellow_threshold=negative_cash_thresholds["yellow"],
            red_threshold=negative_cash_thresholds["red"],
            group="现金与杠杆",
            note="先核准 IBKR 现金、保证金和到账口径，再决定是否需要补现金或降低融资敞口。",
        )
    )

    enriched_positions = []
    for row in position_rows:
        thresholds = _position_thresholds(limits, row.get("asset_type"))
        value = _safe_float(row.get("position_weight"))
        enriched_positions.append(
            {
                **row,
                "current_value": value,
                "yellow_threshold": thresholds["yellow"],
                "red_threshold": thresholds["red"],
                "level": _risk_metric_level(value, thresholds["yellow"], thresholds["red"]),
                "utilization_to_red": value / thresholds["red"] if thresholds["red"] > 0 else 0.0,
            }
        )
    top_position = max(enriched_positions, key=lambda row: row["utilization_to_red"], default={})
    out.append(
        _risk_metric_payload(
            key="single_position",
            title="单票 / 单资产暴露",
            current_value=_safe_float(top_position.get("current_value")),
            yellow_threshold=top_position.get("yellow_threshold"),
            red_threshold=top_position.get("red_threshold"),
            group="集中度",
            note=f"当前最接近阈值：{top_position.get('asset_name') or top_position.get('asset_id') or '暂无'}。",
            sources=[top_position] if top_position else [],
            level=top_position.get("level") or None,
        )
    )

    top_industry = industry_rows[0] if industry_rows else {}
    out.append(
        _risk_metric_payload(
            key="industry_concentration",
            title="行业暴露",
            current_value=_safe_float(top_industry.get("exposure_ratio")),
            yellow_threshold=_safe_float(top_industry.get("yellow_threshold"), _concentration_thresholds(limits, "industry")["yellow"]),
            red_threshold=_safe_float(top_industry.get("red_threshold"), _concentration_thresholds(limits, "industry")["red"]),
            group="集中度",
            note=f"当前最大行业：{top_industry.get('label') or '暂无'}。",
            sources=top_industry.get("top_positions") or [],
            level=top_industry.get("level") or None,
        )
    )

    stress_thresholds = {
        "yellow": _safe_float((limit_cfg.get("stress_loss") or {}).get("yellow"), 0.08),
        "red": _safe_float((limit_cfg.get("stress_loss") or {}).get("red"), 0.15),
    }
    top_stress = max(stress_rows, key=lambda row: _safe_float(row.get("loss_ratio")), default={})
    out.append(
        _risk_metric_payload(
            key="stress_loss",
            title="压力损失",
            current_value=_safe_float(top_stress.get("loss_ratio")),
            yellow_threshold=stress_thresholds["yellow"],
            red_threshold=stress_thresholds["red"],
            group="压力测试",
            note=f"当前最大情景：{top_stress.get('label') or top_stress.get('scenario') or '暂无'}。",
            sources=[top_stress] if top_stress else [],
        )
    )
    return out


def _action_label_for_task(alert: dict[str, Any], group: str) -> str:
    rule = str(alert.get("rule") or "")
    if group == "data_check":
        return "核准"
    if group == "watch":
        return "观察"
    if rule == "negative_cash_ratio":
        return "核准"
    if rule == "gross_exposure":
        return "敞口"
    if rule.startswith("stress_loss:"):
        return "来源"
    if rule.startswith("position_weight:"):
        return "集中"
    return "查看"


def _task_detail(alert: dict[str, Any], contributors: list[dict[str, Any]]) -> str:
    message = str(alert.get("message") or "")
    if contributors:
        names = "、".join(str(c.get("asset_name") or c.get("asset_id") or "") for c in contributors[:4])
        return f"{message}。主要来源：{names}。"
    return message


def _task_group(alert: dict[str, Any]) -> str:
    level = str(alert.get("level") or "green")
    rule = str(alert.get("rule") or "")
    metric = _safe_float(alert.get("metric"))
    threshold = _safe_float(alert.get("threshold"))
    asset_id = rule.split(":", 1)[1] if rule.startswith("position_weight:") and ":" in rule else ""

    if level == "green":
        return "info"
    if asset_id.upper() in {"HUAWEI_ESOP", "ESOP"}:
        return "watch"
    if rule == "negative_cash_ratio":
        return "p1"
    if rule == "gross_exposure":
        return "p1" if metric > threshold else "watch"
    if rule == "stress_loss:equity_global_down_20" and level == "red":
        return "p1"
    if rule.startswith("position_weight:") and level in {"red", "yellow"}:
        return "p1"
    if rule == "factor_exposure:ESOP_SINGLE_COMPANY":
        return "info"
    if rule.startswith("stress_loss:"):
        return "watch"
    if rule.startswith("factor_exposure:") or "_concentration:" in rule:
        return "watch"
    return "watch" if level in {"red", "yellow"} else "info"


def _comparison_for_rule(rule: str) -> str:
    if rule == "total_nav":
        return ">="
    return "<="


def _make_risk_task(
    alert: dict[str, Any],
    *,
    group: str,
    names: dict[str, str],
    contributors: list[dict[str, Any]],
    as_of: str,
) -> dict[str, Any]:
    rule = str(alert.get("rule") or "")
    level = str(alert.get("level") or "green")
    return {
        "id": rule or f"{group}:{len(contributors)}",
        "group": group,
        "title": _task_title(alert, names),
        "severity": _task_severity(level, group),
        "status": _task_status(level) if group != "data_check" else "NEEDS_REVIEW",
        "current_value": _safe_float(alert.get("metric")),
        "limit_value": _safe_float(alert.get("threshold")),
        "comparison": _comparison_for_rule(rule),
        "unit": "ratio" if rule != "total_nav" else "cny",
        "message": str(alert.get("message") or ""),
        "recommendation": _recommendation_for_task(alert, group),
        "short_recommendation": _short_recommendation_for_task(alert, group),
        "action_label": _action_label_for_task(alert, group),
        "detail": _task_detail(alert, contributors),
        "source": contributors,
        "as_of": as_of,
        "related_metrics": {
            "rule": rule,
            "level": level,
            "threshold": alert.get("threshold"),
            "metric": alert.get("metric"),
        },
        "is_expandable": True,
    }


def _manual_data_task(
    *,
    task_id: str,
    title: str,
    message: str,
    recommendation: str,
    as_of: str,
    source: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "group": "data_check",
        "title": title,
        "severity": "blue",
        "status": "NEEDS_REVIEW",
        "current_value": None,
        "limit_value": None,
        "comparison": None,
        "unit": "text",
        "message": message,
        "recommendation": recommendation,
        "detail": message,
        "source": source or [],
        "as_of": as_of,
        "related_metrics": {},
        "is_expandable": True,
    }


def _latest_ibkr_performance() -> dict[str, Any] | None:
    try:
        from domain.brokers.ibkr.ibkr_performance import read_published_performance

        return read_published_performance()
    except Exception:
        return None


def _data_check_tasks(
    *,
    positions: pd.DataFrame,
    nav: float,
    as_of: str,
    data_as_of_min: str,
    data_as_of_max: str,
    ibkr_performance: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    perf_as_of = str((ibkr_performance or {}).get("as_of") or "")
    quality = (ibkr_performance or {}).get("report_quality") or {}
    if quality and quality.get("status") != "ok":
        tasks.append(_manual_data_task(
            task_id="data_check:ibkr_report_publication",
            title="IBKR 有效报告待核准",
            message=f"{quality.get('label')}；有效日期 {perf_as_of}，应有日期 {quality.get('expected_statement_as_of')}。",
            recommendation="先核对 Flex 同步中心；上一有效值不是当日日终确认，不用零值代替缺失报告。",
            as_of=as_of,
        ))
    if perf_as_of and perf_as_of != as_of:
        tasks.append(
            _manual_data_task(
                task_id="data_check:ibkr_performance_asof",
                title="IBKR 负现金 / 保证金口径待核准",
                message=(
                    f"风控快照日期为 {as_of}，IBKR performance 日期为 {perf_as_of}。"
                    "USD 负现金、全账户负现金、NLV、gross exposure 和 leverage 口径需要统一。"
                ),
                recommendation="先核准 80,000 MXN 到账、USD 负现金、NLV、gross exposure 与 leverage，再输出真实处理建议。",
                as_of=as_of,
            )
        )
    elif ibkr_performance is None:
        tasks.append(
            _manual_data_task(
                task_id="data_check:ibkr_performance_missing",
                title="IBKR 保证金字段缺失",
                message="未获得 IBKR performance 或保证金完整字段，无法判断压力后保证金安全垫。",
                recommendation="补齐 IBKR NLV、初始保证金、维持保证金、剩余流动性和购买力。",
                as_of=as_of,
            )
        )

    esop_rows = [
        row.to_dict()
        for _, row in positions.iterrows()
        if "esop" in str(row.get("asset_type") or "").lower()
        or "HUAWEI" in str(row.get("asset_id") or "").upper()
    ]
    if esop_rows:
        esop_value = sum(abs(_safe_float(row.get("market_value_cny"))) for row in esop_rows)
        tasks.append(
            _manual_data_task(
                task_id="data_check:esop_valuation",
                title="内部股份估值和流动性待核准",
                message=f"内部股份约占 {esop_value / nav:.2%}，属于低频估值和非流动资产。",
                recommendation="补充分红政策、估值方法、退出限制和折价口径；不要把它等同于现金或可快速交易资产。",
                as_of=as_of,
                source=[
                    _asset_contributor(row, contribution_cny=_safe_float(row.get("market_value_cny")), nav=nav, note="ESOP")
                    for row in esop_rows[:4]
                ],
            )
        )

    fund_rows = [
        row.to_dict()
        for _, row in positions.iterrows()
        if "fund" in str(row.get("asset_type") or "").lower() or "ETF" in str(row.get("asset_name") or "").upper()
    ]
    if fund_rows:
        tasks.append(
            _manual_data_task(
                task_id="data_check:fund_lookthrough",
                title="基金 / ETF 穿透数据未完整接入",
                message="当前基金和 ETF 主要按产品名称与简化映射估算行业、风格和主题暴露，真实底层持仓可能被低估或重复计算。",
                recommendation="保持为数据核准项；短期不做完整穿透，先避免把估算暴露当作精确风控结论。",
                as_of=as_of,
                source=[
                    _asset_contributor(row, contribution_cny=_safe_float(row.get("market_value_cny")), nav=nav, note="基金/ETF")
                    for row in fund_rows[:4]
                ],
            )
        )

    if data_as_of_min and data_as_of_min != data_as_of_max:
        tasks.append(
            _manual_data_task(
                task_id="data_check:data_freshness",
                title="部分资产快照日期滞后",
                message=f"当前数据覆盖从 {data_as_of_min} 到 {data_as_of_max}，低频资产和手工资产可能不是同一估值日。",
                recommendation="风控结论按保守口径阅读；低频资产需要单独核准日期和估值来源。",
                as_of=as_of,
            )
        )
    return tasks


def _p1_sort_priority(task: dict[str, Any]) -> int:
    rule = str((task.get("related_metrics") or {}).get("rule") or task.get("id") or "")
    if rule == "negative_cash_ratio":
        return 0
    if rule == "gross_exposure":
        return 1
    if rule == "stress_loss:equity_global_down_20":
        return 2
    if rule.startswith("position_weight:"):
        return 3
    return 20


def _build_risk_tasks(
    alerts: list[dict[str, Any]],
    positions: pd.DataFrame,
    nav: float,
    factor_map: dict[str, Any],
    stress_tests: dict[str, Any],
    concentration: dict[str, list[dict[str, Any]]],
    *,
    as_of: str,
    data_as_of_min: str,
    data_as_of_max: str,
    ibkr_performance: dict[str, Any] | None,
    metrics: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    names = _asset_name_lookup(positions)
    tasks: dict[str, list[dict[str, Any]]] = {"p1": [], "watch": [], "data_check": [], "info": []}
    for alert in alerts:
        rule = str(alert.get("rule") or "")
        if "crypto" in rule.lower():
            continue
        contributors = _alert_contributors(alert, positions, nav, factor_map, stress_tests, concentration)
        group = _task_group(alert)
        task = _make_risk_task(alert, group=group, names=names, contributors=contributors, as_of=as_of)
        if rule == "negative_cash_ratio":
            ibkr = ibkr_performance or {}
            base_currency = str(ibkr.get("base_currency") or "USD")
            task["review_context"] = {
                "risk_summary_as_of": as_of,
                "ibkr_performance_as_of": ibkr.get("as_of"),
                "risk_summary_negative_cash_cny": metrics.get("negative_cash_cny"),
                "ibkr_negative_cash_usd": ibkr.get("negative_cash_base") if base_currency == "USD" else None,
                "ibkr_negative_cash_cny": ibkr.get("negative_cash_cny"),
                "ibkr_leverage": ibkr.get("leverage_ratio"),
                "ibkr_gross_exposure_usd": ibkr.get("gross_exposure_base") if base_currency == "USD" else None,
                "ibkr_gross_exposure_cny": ibkr.get("gross_exposure_cny"),
                "pending_checks": [
                    "80,000 MXN 到账是否已反映",
                    "USD 负现金是否真实存在",
                    "现金币种是否需要换汇",
                ],
                "note": "数据未核准前，仅提示风险，不生成交易动作。",
            }
        tasks[group].append(task)
    tasks["data_check"].extend(
        _data_check_tasks(
            positions=positions,
            nav=nav,
            as_of=as_of,
            data_as_of_min=data_as_of_min,
            data_as_of_max=data_as_of_max,
            ibkr_performance=ibkr_performance,
        )
    )
    for key in tasks:
        if key == "p1":
            tasks[key] = sorted(tasks[key], key=lambda item: (_p1_sort_priority(item), str(item.get("title"))))
        else:
            tasks[key] = sorted(
                tasks[key],
                key=lambda item: (
                    {"red": 0, "yellow": 1, "blue": 2, "gray": 3}.get(str(item.get("severity")), 4),
                    -abs(_safe_float(item.get("current_value"))),
                    str(item.get("title")),
                ),
            )
    return tasks


def _max_stress_row(stress_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    visible = [row for row in stress_rows if "crypto" not in str(row.get("scenario") or "").lower()]
    if not visible:
        return None
    return max(visible, key=lambda row: abs(_safe_float(row.get("loss_ratio"))))


def _risk_summary_payload(
    *,
    nav: float,
    metrics: dict[str, Any],
    stress_rows: list[dict[str, Any]],
    risk_tasks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    max_stress = _max_stress_row(stress_rows)
    p1_count = len(risk_tasks.get("p1", []))
    watch_count = len(risk_tasks.get("watch", []))
    data_count = len(risk_tasks.get("data_check", []))
    risk_level = "高风险" if p1_count >= 5 else ("中高风险" if p1_count > 0 else ("中风险" if watch_count else "低风险"))
    chips = []
    titles = " ".join(item.get("title", "") for item in [*risk_tasks.get("p1", []), *risk_tasks.get("watch", [])])
    if "负现金" in titles:
        chips.append("IBKR负现金")
    if "压力测试" in titles:
        chips.append("压力损失")
    if "单资产" in titles:
        chips.append("单资产集中")
    if "ESOP" in titles or "内部股份" in titles:
        chips.append("ESOP流动性")
    if data_count:
        chips.append("数据口径")
    if not chips:
        chips.append("暂无高优先级风险")
    return {
        "total_net_worth_cny": nav,
        "gross_exposure_cny": metrics.get("gross_exposure_cny"),
        "gross_exposure_ratio": metrics.get("gross_exposure_ratio"),
        "negative_cash_cny": metrics.get("negative_cash_cny"),
        "negative_cash_ratio": metrics.get("negative_cash_ratio"),
        "risk_level": risk_level,
        "risk_budget_ratio": metrics.get("gross_exposure_ratio"),
        "max_stress_loss_pct": _safe_float((max_stress or {}).get("loss_ratio")),
        "max_stress_loss_cny": _safe_float((max_stress or {}).get("loss_cny")),
        "max_stress_scenario": (max_stress or {}).get("scenario"),
        "p1_count": p1_count,
        "watch_count": watch_count,
        "data_check_count": data_count,
        "risk_chips": chips[:5],
    }


def _is_liability_marker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"liability", "debt"} or "负债" in text


def _infer_asset_type(row: dict[str, Any]) -> str:
    account = str(row.get("account_code") or "").lower()
    account_type = str(row.get("account_type") or "").lower()
    asset_liability = str(row.get("asset_liability") or "")
    symbol = str(row.get("symbol") or "").upper()
    name = str(row.get("asset_name") or "").lower()
    major = str(row.get("major_category") or "").lower()
    minor = str(row.get("minor_category") or "").lower()
    text = " ".join([account, account_type, symbol.lower(), name, major, minor])

    if symbol.startswith("CASH") or "cash" in text or "现金" in text:
        return "cash"
    if _is_liability_marker(asset_liability) or account_type == "liability":
        return "liability"
    if account_type in SYNTHETIC_ASSET_TYPES_BY_ACCOUNT_TYPE:
        return SYNTHETIC_ASSET_TYPES_BY_ACCOUNT_TYPE[account_type]
    if "gold" in text or "precious_metal" in text or "黄金" in text or symbol in {"GLD", "XAU", "GOLD"}:
        return "gold"
    if "crypto" in text or "binance" in text or "加密" in text or symbol in {"BTC", "ETH"}:
        return "crypto"
    if "esop" in text or "huawei" in text or "员工" in text:
        return "esop"
    if "ibkr" in text or "us_" in text:
        return "us_stock"
    if "fund" in text or "基金" in text or "licaitong" in text:
        return "a_share_fund"
    if "a_share" in text or "tonghuashun" in text or "ths" in text:
        return "a_share_stock"
    return "manual_asset"


def _account_balance_position(account: dict[str, Any], fx_rates: dict[str, float]) -> dict[str, Any] | None:
    if account.get("amount") in (None, ""):
        return None

    account_code = str(account.get("account_code") or "unknown")
    currency = str(account.get("currency") or account.get("base_currency") or "CNY").upper()
    amount = float(account.get("amount") or 0.0)
    raw_amount_cny = account.get("amount_cny")
    amount_cny = float(raw_amount_cny) if raw_amount_cny not in (None, "") else (0.0 if amount == 0 else None)
    account_type = str(account.get("account_type") or "")
    display_name = account.get("display_name") or account_code
    fx_rate = amount_cny / amount if amount and amount_cny is not None else (fx_rates.get(currency) if amount == 0 else None)
    asset_type = _infer_asset_type(
        {
            "account_code": account_code,
            "account_type": account_type,
            "asset_name": display_name,
            "asset_liability": "liability" if account_type == "liability" else "asset",
        }
    )
    return {
        "as_of": str(account.get("as_of") or ""),
        "source_type": "account_center",
        "account_code": account_code,
        "account_name": display_name,
        "owner_ratio": 1.0,
        "symbol": account_code.upper(),
        "asset_id": account_code,
        "asset_name": display_name,
        "asset_type": asset_type,
        "currency": currency,
        "quantity": 1.0,
        "price": amount,
        "market_value": amount,
        "cost": amount,
        "fx_rate_to_cny": fx_rate,
        "market_value_cny": amount_cny,
        "cost_cny": amount_cny,
    }


def load_latest_positions() -> pd.DataFrame:
    """Load current risk positions from the unified PortfolioState payload."""
    state = build_portfolio_state(base_currency="CNY")
    account_rows = [
        a
        for a in (state.get("account_balances") or [])
        if a.get("include_in_nav") and a.get("amount") not in (None, "")
    ]
    accounts_by_code = {str(a.get("account_code")): a for a in account_rows}
    account_names = {
        code: str(account.get("display_name") or code)
        for code, account in accounts_by_code.items()
    }
    detail_account_codes = [
        code
        for code, account in accounts_by_code.items()
        if str(account.get("account_type") or "") in INVESTMENT_DETAIL_ACCOUNT_TYPES
    ]
    for code in state.get('snapshot_scope_unavailable_accounts') or []:
        if code not in detail_account_codes:
            detail_account_codes.append(code)
    for code in state.get('fx_unavailable_accounts') or []:
        if code not in detail_account_codes:
            detail_account_codes.append(code)
    fx_rates: dict[str, float] = {"CNY": 1.0}
    for entry in state.get("fx_rates") or []:
        currency = str(entry.get("quote_currency") or "").upper()
        if not currency or not str(entry.get("source") or "").strip():
            continue
        try:
            rate = float(entry.get("rate"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0:
            fx_rates[currency] = rate
    rows = [
        dict(p)
        for p in (state.get("investment_positions") or [])
        if str(p.get("account_code") or "") in detail_account_codes
    ]

    out: list[dict[str, Any]] = []
    represented_accounts: set[str] = set()
    for row in rows:
        currency = str(row.get("currency") or "CNY").upper()
        native_market_value = float(row.get("market_value") or 0.0)
        cny_market_value = float(row['market_value_cny']) if row.get('market_value_cny') is not None else None
        native_cost = float(row['cost']) if row.get('cost') is not None else None
        cny_cost = float(row['cost_cny']) if row.get('cost_cny') is not None else None
        fx = (cny_market_value / native_market_value if native_market_value else fx_rates.get(currency)) if cny_market_value is not None else None
        symbol = str(row.get("symbol") or "").strip()
        asset_name = str(row.get("asset_name") or symbol or "UNKNOWN").strip()
        asset_id = str(row.get("asset_id") or symbol or asset_name).strip()
        account_code = str(row.get("account_code") or "unknown")
        represented_accounts.add(account_code)
        out.append(
            {
                "as_of": str(row.get("as_of") or ""),
                "source_type": row.get("source_type") or "portfolio_state",
                "account_code": account_code,
                "account_name": account_names.get(account_code, account_code),
                "owner_ratio": 1.0,
                "symbol": symbol,
                "asset_id": asset_id,
                "asset_name": asset_name,
                "asset_type": _infer_asset_type({**row, "asset_name": asset_name}),
                "currency": currency,
                "quantity": float(row.get("quantity") or 1.0),
                "price": native_market_value,
                "market_value": native_market_value,
                "cost": native_cost if native_cost is not None else cny_cost / fx if cny_cost is not None and fx else cny_cost,
                "fx_rate_to_cny": fx,
                "market_value_cny": cny_market_value,
                "cost_cny": cny_cost,
            }
        )
    for account_code, account in accounts_by_code.items():
        if account_code in represented_accounts:
            continue
        position = _account_balance_position(account, fx_rates)
        if position:
            out.append(position)
    positions = pd.DataFrame(out)
    positions.attrs['snapshot_scope_status'] = state.get('snapshot_scope_status')
    positions.attrs['snapshot_scope_unavailable_accounts'] = state.get('snapshot_scope_unavailable_accounts') or []
    missing_fx_accounts = {str(item.get('account_code') or '') for item in out
                           if item.get('market_value_cny') is None and item.get('market_value') is not None}
    missing_fx_accounts.discard('')
    positions.attrs['fx_status'] = 'incomplete' if missing_fx_accounts else state.get('fx_status')
    positions.attrs['fx_unavailable_accounts'] = sorted(set(state.get('fx_unavailable_accounts') or []) | missing_fx_accounts)
    return positions


def _latest_published_valuation_state(base_currency: str = "CNY") -> dict[str, Any] | None:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                return latest_published_portfolio_valuation_state(cur, base_currency=base_currency)
    except Exception:
        return None


def _positions_reconciled_to_published_nav(
    positions: pd.DataFrame,
    limits: dict[str, Any],
    valuation_state: dict[str, Any] | None,
) -> tuple[pd.DataFrame, float, dict[str, Any] | None]:
    """Return calculation positions whose total NAV equals the published state."""
    df = ensure_positions(positions, limits)
    raw_nav = total_nav(df, limits)
    if not valuation_state or valuation_state.get("net_liquidation_value") in (None, ""):
        return df, raw_nav, None

    positions_as_of = ""
    if "as_of" in df.columns and not df.empty:
        dates = [str(value)[:10] for value in df["as_of"].dropna().tolist() if str(value or "")[:10]]
        positions_as_of = max(dates) if dates else ""
    if not is_published_valuation_fresh_for_date(valuation_state, positions_as_of):
        meta = published_valuation_stale_meta(valuation_state, target_as_of=positions_as_of)
        if meta is not None:
            meta.update(
                {
                    "raw_positions_nav_cny": raw_nav,
                    "nav_reconciliation_cny": 0.0,
                }
            )
        return df, raw_nav, meta

    try:
        target_nav = float(valuation_state["net_liquidation_value"])
    except (TypeError, ValueError, OverflowError):
        target_nav = float("nan")
    if not math.isfinite(target_nav) or valuation_state.get("data_completeness") != "complete":
        meta = published_valuation_state_meta(valuation_state) or {}
        meta["reconciliation_status"] = "unavailable"
        meta["exposure_scope_status"] = "incomplete"
        return df, raw_nav, meta
    adjustment = target_nav - raw_nav
    meta = published_valuation_state_meta(valuation_state) or {}
    meta.update(
        {
            "raw_positions_nav_cny": raw_nav,
            "nav_reconciliation_cny": adjustment,
        }
    )
    if abs(adjustment) <= 0.005:
        meta["reconciliation_status"] = "matched"
        return df, target_nav, meta

    meta["reconciliation_status"] = "unexplained"
    meta["exposure_scope_status"] = "incomplete"
    return df, target_nav, meta


def build_risk_state() -> dict[str, Any]:
    """Build the current risk page payload."""
    limits = load_risk_limits()
    factor_map = load_factor_map()
    concentration_map = load_concentration_map()
    positions = load_latest_positions()
    if positions.attrs.get('snapshot_scope_status') == 'incomplete':
        return {'status': 'unavailable', 'reason': 'snapshot_scope_incomplete',
                'snapshot_scope_unavailable_accounts': positions.attrs.get('snapshot_scope_unavailable_accounts', []),
                'metrics': {'total_nav': None}, 'alerts': [], 'holdings': _records(positions)}
    if positions.attrs.get('fx_status') == 'incomplete':
        return {'status': 'unavailable', 'reason': 'missing_fx',
                'fx_unavailable_accounts': positions.attrs.get('fx_unavailable_accounts', []),
                'metrics': {'total_nav': None}, 'alerts': [], 'holdings': _records(positions)}
    if positions.empty:
        return {"status": "empty", "alerts": [], "holdings": []}

    valuation_state = _latest_published_valuation_state("CNY")
    calc_positions, nav, valuation_meta = _positions_reconciled_to_published_nav(
        positions,
        limits,
        valuation_state,
    )
    if valuation_meta and valuation_meta.get("reconciliation_status") == "unavailable":
        return {"status": "unavailable", "reason": "invalid_published_valuation",
                "metrics": {"total_nav": None}, "valuation_state": valuation_meta,
                "alerts": [], "holdings": _records(positions)}
    if not math.isfinite(nav) or nav <= 0:
        return {"status": "unavailable", "reason": "nonpositive_nav" if math.isfinite(nav) else "invalid_nav",
                "metrics": {"total_nav": nav if math.isfinite(nav) else None,
                            "gross_exposure_ratio": None, "negative_cash_ratio": None},
                "valuation_state": valuation_meta or {},
                "alerts": risk_alerts(calc_positions, limits, factor_map, nav_override=nav),
                "holdings": _records(positions)}
    alerts = risk_alerts(calc_positions, limits, factor_map, nav_override=nav)
    legacy_concentration = concentration_exposure(
        calc_positions,
        nav,
        concentration_map,
        _load_db_industry_lookup(calc_positions),
        limits,
    )
    raw_style_exposure = legacy_concentration["style"]
    legacy_concentration_payload = {
        "industry": _annotate_concentration(legacy_concentration["industry"], kind="industry", limits=limits),
        "style": _annotate_concentration(raw_style_exposure, kind="style", limits=limits),
    }
    legacy_style_groups = {
        kind: _annotate_concentration(rows, kind=kind, limits=limits)
        for kind, rows in _split_style_exposure_groups(raw_style_exposure).items()
    }
    exposure_payload = _load_latest_exposure_payload("CNY")
    exposure_concentration = _concentration_from_exposure_payload(exposure_payload, nav=nav, limits=limits)
    if exposure_concentration:
        concentration_source = "exposure_engine"
        concentration = {
            "industry": exposure_concentration["industry"],
            "style": exposure_concentration["style"],
        }
        style_groups = exposure_concentration["style_groups"]
    else:
        concentration_source = "legacy_concentration_map"
        concentration = legacy_concentration_payload
        style_groups = legacy_style_groups
    alert_concentration = {"industry": concentration["industry"], **style_groups}
    concentration_alerts = _concentration_alerts(alert_concentration)
    all_alerts = [*alerts, *concentration_alerts]
    rebalance = [*rebalance_suggestions(positions, limits, factor_map, nav_override=nav), *_concentration_rebalance(alert_concentration)]
    as_of = _view_as_of()
    data_as_of_min = str(positions["as_of"].min())
    data_as_of_max = str(positions["as_of"].max())
    stress_rows = _records(stress_loss(calc_positions, factor_map, limits.get("stress_tests", {}), limits, nav_override=nav))
    stress_rows = [row for row in stress_rows if "crypto" not in str(row.get("scenario") or "").lower()]
    metrics = {
        "total_nav": nav,
        **gross_exposure(calc_positions, limits, nav_override=nav),
        **negative_cash_ratio(calc_positions, limits, nav_override=nav),
        "red_alerts": sum(1 for a in all_alerts if a["level"] == "red"),
        "yellow_alerts": sum(1 for a in all_alerts if a["level"] == "yellow"),
    }
    account_rows = _records(account_weight(calc_positions, limits, nav_override=nav))
    position_rows = _records(position_weight(calc_positions, limits, nav_override=nav))
    factor_rows = _records(factor_exposure(calc_positions, factor_map, limits, nav_override=nav))
    ibkr_performance = _latest_ibkr_performance()
    risk_tasks = _build_risk_tasks(
        all_alerts,
        calc_positions,
        nav,
        factor_map,
        limits.get("stress_tests", {}),
        alert_concentration,
        as_of=as_of,
        data_as_of_min=data_as_of_min,
        data_as_of_max=data_as_of_max,
        ibkr_performance=ibkr_performance,
        metrics=metrics,
    )
    if valuation_meta and valuation_meta.get("reconciliation_status") == "unexplained":
        risk_tasks["data_check"].append(_manual_data_task(
            task_id="data_check:valuation_unexplained",
            title="资产净值差额待核对",
            message="已发布净值与已知持仓存在未解释差额；不能将差额归类为现金或负债。",
            recommendation="核对账户范围、来源和估值构成后再判断完整风险敞口。",
            as_of=as_of,
        ))
    terminal_summary = _risk_summary_payload(
        nav=nav,
        metrics=metrics,
        stress_rows=stress_rows,
        risk_tasks=risk_tasks,
    )
    risk_actions = _build_risk_actions(
        all_alerts,
        calc_positions,
        nav,
        factor_map,
        limits.get("stress_tests", {}),
        alert_concentration,
    )
    user_metrics = _build_user_risk_metrics(
        limits=limits,
        metrics=metrics,
        factor_rows=factor_rows,
        position_rows=position_rows,
        industry_rows=concentration["industry"],
        stress_rows=stress_rows,
    )
    return {
        "status": "review" if valuation_meta and valuation_meta.get("reconciliation_status") == "unexplained" else "ok",
        "as_of": as_of,
        "coverage_start": data_as_of_min,
        "coverage_end": data_as_of_max,
        "data_as_of_min": data_as_of_min,
        "data_as_of_max": data_as_of_max,
        "summary": terminal_summary,
        "valuation_state": valuation_meta or {},
        "metrics": metrics,
        "alerts": all_alerts,
        "user_metrics": user_metrics,
        "accounts": account_rows,
        "positions": position_rows,
        "holdings": _records(positions),
        "factors": factor_rows,
        "industry_exposure": concentration["industry"],
        "style_exposure": concentration["style"],
        "investment_style_exposure": style_groups["investment_style"],
        "region_exposure": style_groups["region"],
        "currency_exposure": style_groups["currency"],
        "region_currency_exposure": [*style_groups["region"], *style_groups["currency"]],
        "special_risk_exposure": style_groups["special"],
        "concentration": {
            "industry": concentration["industry"],
            "investment_style": style_groups["investment_style"],
            "region": style_groups["region"],
            "currency": style_groups["currency"],
            "special": style_groups["special"],
        },
        "concentration_source": concentration_source,
        "exposure_engine_quality": (exposure_concentration or {}).get("quality") or {},
        "exposure_engine_validation": (exposure_concentration or {}).get("validation") or {},
        "concentration_rules": _concentration_rules_payload(limits),
        "stress": stress_rows,
        "stress_tests": stress_rows,
        "rebalance": rebalance,
        "risk_tasks": risk_tasks,
        "risk_actions": risk_actions,
        "ibkr": ibkr_performance or {},
        "currency_exposure_payload": style_groups["currency"],
        "data_quality": {
            "status": "NEEDS_REVIEW" if risk_tasks["data_check"] else "OK",
            "issues": risk_tasks["data_check"],
        },
        "recommendations": {
            "actions": risk_tasks["p1"],
            "watch": risk_tasks["watch"],
            "data_checks": risk_tasks["data_check"],
        },
    }
