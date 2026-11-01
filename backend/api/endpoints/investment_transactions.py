"""Investment transaction ledger API."""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from domain.assets.alias_policy import identity_alias_sql
from domain.investments.portfolio_state import _load_accounts_config, _owner_ratio
from domain.investments.trade_campaigns import build_position_campaigns_payload, today_local
from domain.investments.trade_attribution import (
    apply_auto_trade_annotations,
    build_trade_auto_review_queue_payload,
    build_trade_attribution_payload,
    build_trade_attribution_summary_payload,
)


router = APIRouter()

REPORTED_RETURNS_CONFIG_PATH = Path(os.environ.get("PAMS_INVESTMENT_RETURNS_CONFIG", "/app/config/investment_returns.yaml"))
LOCAL_REPORTED_RETURNS_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "investment_returns.yaml"


ACCOUNT_LABELS = {
    "tencent_licaitong": "腾讯理财通",
    "tonghuashun": "同花顺",
    "ibkr": "IBKR",
}

CURRENCY_LABELS = {
    "CNY": "人民币",
    "USD": "美元",
    "HKD": "港元",
    "MXN": "墨西哥比索",
    "KRW": "韩元",
}

BUY_REASON_OPTIONS = [
    {"value": "plan_execution", "label": "计划执行"},
    {"value": "allocation_adjustment", "label": "配置调整"},
    {"value": "fundamental_valuation", "label": "基本面 / 估值判断"},
    {"value": "trend_signal", "label": "趋势 / 技术信号"},
    {"value": "event_driven", "label": "事件驱动"},
    {"value": "arbitrage_structure", "label": "套利 / 结构性机会"},
    {"value": "trial_observation", "label": "试仓 / 观察仓"},
    {"value": "cash_management", "label": "现金管理 / 防御"},
    {"value": "uncertain_review", "label": "未确定 / 待复盘"},
    {"value": "other", "label": "其他"},
]

SELL_REASON_OPTIONS = [
    {"value": "planned_take_profit", "label": "计划止盈 / 目标达成"},
    {"value": "risk_control_stop_loss", "label": "风险控制 / 止损"},
    {"value": "thesis_invalidated", "label": "逻辑证伪 / 基本面变化"},
    {"value": "trend_signal_exit", "label": "趋势转弱 / 信号退出"},
    {"value": "risk_reduction_rebalance", "label": "风险降仓 / 再平衡"},
    {"value": "event_completed", "label": "事件兑现 / 催化结束"},
    {"value": "replacement_better_asset", "label": "换仓 / 更优标的替代"},
    {"value": "liquidity_need", "label": "资金需求 / 非投资用途"},
    {"value": "correction", "label": "误操作 / 修正"},
    {"value": "other", "label": "其他"},
]

REVIEW_TAG_OPTIONS = [
    {"value": "chase_high", "label": "追高", "group": "execution"},
    {"value": "early_bottom", "label": "抄底过早", "group": "execution"},
    {"value": "late_confirmation", "label": "确认过晚", "group": "execution"},
    {"value": "rushed_buy", "label": "买入过急", "group": "execution"},
    {"value": "insufficient_batch", "label": "分批不足", "group": "execution"},
    {"value": "sold_too_early", "label": "卖早了", "group": "execution"},
    {"value": "sold_too_late", "label": "卖晚了", "group": "execution"},
    {"value": "oversized", "label": "仓位过大", "group": "execution"},
    {"value": "undersized", "label": "仓位过小", "group": "execution"},
    {"value": "single_trade_too_heavy", "label": "单笔过重", "group": "execution"},
    {"value": "theme_concentration", "label": "主题过度集中", "group": "execution"},
    {"value": "ignored_stop_loss", "label": "未执行止损", "group": "execution"},
    {"value": "ignored_take_profit", "label": "未执行止盈", "group": "execution"},
    {"value": "plan_followed", "label": "按计划执行", "group": "behavior"},
    {"value": "early_execution", "label": "提前执行", "group": "behavior"},
    {"value": "delayed_execution", "label": "延后执行", "group": "behavior"},
    {"value": "good_discipline", "label": "纪律正确", "group": "behavior"},
    {"value": "bad_discipline", "label": "纪律错误", "group": "behavior"},
    {"value": "fomo", "label": "FOMO / 怕错过", "group": "behavior"},
    {"value": "panic", "label": "恐慌", "group": "behavior"},
    {"value": "anchored_cost", "label": "锚定成本", "group": "behavior"},
    {"value": "overconfidence", "label": "过度自信", "group": "behavior"},
    {"value": "hesitation", "label": "犹豫拖延", "group": "behavior"},
    {"value": "impulsive_subjective", "label": "主观冲动", "group": "behavior"},
    {"value": "loss_rationalization", "label": "亏损后找理由", "group": "behavior"},
    {"value": "profit_take_too_early", "label": "盈利后过早落袋", "group": "behavior"},
    {"value": "data_issue", "label": "数据 / 口径问题", "group": "data"},
    {"value": "liquidity_slippage", "label": "流动性 / 滑点", "group": "data"},
    {"value": "operation_error", "label": "误操作", "group": "data"},
    {"value": "external_need", "label": "非投资原因", "group": "data"},
    {"value": "account_restriction", "label": "账户限制 / 交易限制", "group": "data"},
    {"value": "thesis_correct", "label": "逻辑正确", "group": "post_review"},
    {"value": "thesis_wrong", "label": "逻辑错误", "group": "post_review"},
    {"value": "right_reason_poor_execution", "label": "理由正确但执行差", "group": "post_review"},
    {"value": "right_result_weak_reason", "label": "结果正确但理由一般", "group": "post_review"},
    {"value": "lucky_result", "label": "运气成分较大", "group": "post_review"},
    {"value": "timely_stop_loss", "label": "止损及时", "group": "post_review"},
    {"value": "delayed_stop_loss", "label": "止损拖延", "group": "post_review"},
    {"value": "correct_rebalance", "label": "再平衡正确", "group": "post_review"},
    {"value": "wrong_rebalance", "label": "再平衡不当", "group": "post_review"},
    {"value": "identity_correct", "label": "身份判断正确", "group": "post_review"},
    {"value": "identity_wrong", "label": "身份判断错误", "group": "post_review"},
    {"value": "identity_change_reasonable", "label": "身份变化合理", "group": "post_review"},
    {"value": "identity_drift", "label": "身份漂移失控", "group": "post_review"},
]

REVIEW_STATUS_OPTIONS = [
    {"value": "auto", "label": "未复盘"},
    {"value": "needs_review", "label": "待复盘"},
    {"value": "reviewed", "label": "已复盘"},
    {"value": "ignored", "label": "忽略"},
]

POSITION_IDENTITY_OPTIONS = [
    {"value": "allocation", "label": "配置仓"},
    {"value": "value", "label": "价值仓"},
    {"value": "trend", "label": "趋势仓"},
    {"value": "event", "label": "事件仓"},
    {"value": "trial", "label": "试验仓 / 观察仓"},
    {"value": "cash_management", "label": "现金管理仓"},
    {"value": "uncertain", "label": "未确定 / 待判断"},
]

CURRENT_POSITION_IDENTITY_OPTIONS = [
    *POSITION_IDENTITY_OPTIONS,
    {"value": "closed", "label": "已关闭"},
]

IDENTITY_CONFIDENCE_OPTIONS = [
    {"value": "high", "label": "高"},
    {"value": "medium", "label": "中"},
    {"value": "low", "label": "低"},
    {"value": "unknown", "label": "未判断"},
]

SELL_RULE_TYPE_OPTIONS = [
    {"value": "allocation_rebalance", "label": "配置仓：再平衡 / 目标比例偏离"},
    {"value": "value_thesis", "label": "价值仓：证伪 / 高估 / 安全边际消失"},
    {"value": "trend_exit", "label": "趋势仓：趋势破坏 / 止损 / 信号退出"},
    {"value": "event_resolution", "label": "事件仓：兑现 / 失败 / 催化消失"},
    {"value": "trial_review", "label": "试验仓：到期复盘 / 亏损上限 / 不无限加仓"},
    {"value": "cash_need", "label": "现金管理仓：现金需求 / 收益率替代 / 风险变化"},
    {"value": "uncertain_reclassify", "label": "未确定：复盘日前必须重新分类"},
    {"value": "closed", "label": "已关闭"},
    {"value": "other", "label": "其他"},
]

TRANSITION_TYPE_OPTIONS = [
    {"value": "planned_upgrade", "label": "计划内升级"},
    {"value": "planned_downgrade", "label": "计划内降级"},
    {"value": "research_reclassify", "label": "研究加深后重分类"},
    {"value": "risk_downgrade", "label": "风险降级"},
    {"value": "post_stop_review", "label": "止损后复盘"},
    {"value": "loss_rationalization", "label": "亏损后改口"},
    {"value": "position_out_of_control", "label": "仓位失控"},
    {"value": "data_correction", "label": "数据修正"},
    {"value": "other", "label": "其他"},
]

TRANSITION_TRIGGER_OPTIONS = [
    {"value": "new_fundamental_evidence", "label": "基本面新证据"},
    {"value": "valuation_change", "label": "估值变化"},
    {"value": "trend_signal_change", "label": "趋势信号变化"},
    {"value": "risk_budget_exceeded", "label": "风险预算超限"},
    {"value": "position_too_large", "label": "仓位过大"},
    {"value": "theme_concentration", "label": "主题过度集中"},
    {"value": "event_realized", "label": "事件兑现"},
    {"value": "event_failed", "label": "事件失败"},
    {"value": "price_down", "label": "价格下跌"},
    {"value": "price_up", "label": "价格上涨"},
    {"value": "replacement_better_asset", "label": "更优标的切换"},
    {"value": "research_added", "label": "补充研究"},
    {"value": "emotion_influence", "label": "情绪影响"},
    {"value": "data_correction", "label": "数据口径修正"},
]

TRANSITION_QUALITY_OPTIONS = [
    {"value": "reasonable", "label": "合理变化"},
    {"value": "needs_review", "label": "需要复盘"},
    {"value": "high_risk_drift", "label": "高风险漂移"},
    {"value": "suspected_stop_avoidance", "label": "疑似逃避止损"},
    {"value": "data_correction", "label": "数据修正，不计入行为错误"},
]

BUY_REASON_VALUES = {item["value"] for item in BUY_REASON_OPTIONS}
SELL_REASON_VALUES = {item["value"] for item in SELL_REASON_OPTIONS}
REVIEW_TAG_VALUES = {item["value"] for item in REVIEW_TAG_OPTIONS}
REVIEW_STATUS_VALUES = {item["value"] for item in REVIEW_STATUS_OPTIONS}
POSITION_IDENTITY_VALUES = {item["value"] for item in CURRENT_POSITION_IDENTITY_OPTIONS}
INITIAL_POSITION_IDENTITY_VALUES = {item["value"] for item in POSITION_IDENTITY_OPTIONS}
IDENTITY_CONFIDENCE_VALUES = {item["value"] for item in IDENTITY_CONFIDENCE_OPTIONS}
SELL_RULE_TYPE_VALUES = {item["value"] for item in SELL_RULE_TYPE_OPTIONS}
TRANSITION_TYPE_VALUES = {item["value"] for item in TRANSITION_TYPE_OPTIONS}
TRANSITION_TRIGGER_VALUES = {item["value"] for item in TRANSITION_TRIGGER_OPTIONS}


def get_conn():
    """Create a Postgres connection using the same environment as the API."""
    import os

    from domain.imports.isolation import guarded_connect

    return guarded_connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def norm(value: Any):
    """Normalize DB values for JSON responses."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def norm_deep(value: Any):
    """Normalize nested config values for JSON responses."""
    if isinstance(value, dict):
        return {k: norm_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [norm_deep(v) for v in value]
    return norm(value)


@router.get("/investment-returns/reported")
def reported_investment_returns():
    """Return manually confirmed investment return notes shown under investment assets."""
    path = REPORTED_RETURNS_CONFIG_PATH if REPORTED_RETURNS_CONFIG_PATH.exists() else LOCAL_REPORTED_RETURNS_CONFIG_PATH
    if not path.exists():
        return {"as_of": None, "source": None, "sections": {}}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return norm_deep(data)


def ensure_ledger_table(conn) -> None:
    """Investment ledger tables are provisioned by database schema migrations."""
    return None


class InvestmentTransactionCreate(BaseModel):
    """Payload for one manually recorded investment transaction."""

    trade_date: date
    trade_time: Optional[datetime] = None
    account_code: str
    major_category: Optional[str] = None
    minor_category: Optional[str] = None
    symbol: Optional[str] = None
    name: str = Field(min_length=1)
    side: str = Field(min_length=1)
    quantity: Optional[float] = None
    price: Optional[float] = None
    gross_amount: Optional[float] = None
    fees: Optional[float] = 0
    currency: str = "CNY"
    status: Optional[str] = "recorded"
    notes: Optional[str] = None


class InvestmentTradeReviewUpdate(BaseModel):
    """Manual review fields attached to one investment trade fact."""

    buy_reason: Optional[str] = None
    sell_reason: Optional[str] = None
    review_tags: list[str] = Field(default_factory=list)
    trade_intent: Optional[str] = None
    trade_plan: Optional[str] = None
    review_note: Optional[str] = None
    review_due_date: Optional[date] = None
    review_status: Optional[str] = None
    initial_position_identity: Optional[str] = None
    current_position_identity: Optional[str] = None
    identity_confidence: Optional[str] = None
    intended_holding_period: Optional[str] = None
    buy_logic: Optional[str] = None
    add_condition: Optional[str] = None
    reduce_condition: Optional[str] = None
    sell_condition: Optional[str] = None
    max_acceptable_loss_pct: Optional[float] = None
    max_position_pct: Optional[float] = None
    next_review_date: Optional[date] = None
    sell_rule_type: Optional[str] = None
    identity_transition_type: Optional[str] = None
    identity_transition_triggers: list[str] = Field(default_factory=list)
    identity_transition_evidence: Optional[str] = None


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and row["exists"])


def _review_options_payload() -> dict[str, Any]:
    return {
        "buy_reasons": BUY_REASON_OPTIONS,
        "sell_reasons": SELL_REASON_OPTIONS,
        "review_tags": REVIEW_TAG_OPTIONS,
        "statuses": REVIEW_STATUS_OPTIONS,
        "position_identities": POSITION_IDENTITY_OPTIONS,
        "current_position_identities": CURRENT_POSITION_IDENTITY_OPTIONS,
        "identity_confidences": IDENTITY_CONFIDENCE_OPTIONS,
        "sell_rule_types": SELL_RULE_TYPE_OPTIONS,
        "transition_types": TRANSITION_TYPE_OPTIONS,
        "transition_triggers": TRANSITION_TRIGGER_OPTIONS,
        "transition_qualities": TRANSITION_QUALITY_OPTIONS,
    }


def _clean_optional_text(value: Any, *, max_len: int = 4000) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_len]


def _validate_enum(value: str | None, allowed: set[str], field_name: str) -> str | None:
    cleaned = _clean_optional_text(value, max_len=80)
    if cleaned is None:
        return None
    if cleaned not in allowed:
        raise HTTPException(status_code=400, detail=f"invalid {field_name}: {cleaned}")
    return cleaned


def _field_was_provided(model: BaseModel, field_name: str) -> bool:
    fields: Any = getattr(model, "model_fields_set", None)
    if fields is None:
        fields = getattr(model, "__fields_set__", set())
    return field_name in fields


def _validate_review_tags(tags: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in tags or []:
        tag = _clean_optional_text(raw, max_len=80)
        if not tag or tag in cleaned:
            continue
        if tag not in REVIEW_TAG_VALUES:
            raise HTTPException(status_code=400, detail=f"invalid review tag: {tag}")
        cleaned.append(tag)
    execution_tags = {
        item["value"]
        for item in REVIEW_TAG_OPTIONS
        if item.get("group") == "execution"
    }
    if sum(1 for tag in cleaned if tag in execution_tags) > 3:
        raise HTTPException(status_code=400, detail="execution review tags can contain at most 3 items")
    return cleaned


def _validate_transition_triggers(triggers: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in triggers or []:
        trigger = _clean_optional_text(raw, max_len=80)
        if not trigger or trigger in cleaned:
            continue
        if trigger not in TRANSITION_TRIGGER_VALUES:
            raise HTTPException(status_code=400, detail=f"invalid identity transition trigger: {trigger}")
        cleaned.append(trigger)
    return cleaned


def _clean_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"invalid numeric value: {value}")
    if number < 0:
        raise HTTPException(status_code=400, detail=f"numeric value must be non-negative: {value}")
    return number


def _infer_transition_quality(
    from_identity: str | None,
    to_identity: str | None,
    transition_type: str | None,
    triggers: list[str],
    evidence_note: str | None,
) -> str:
    trigger_set = set(triggers or [])
    if transition_type == "data_correction" or "data_correction" in trigger_set:
        return "data_correction"
    if transition_type in {"loss_rationalization", "position_out_of_control"}:
        return "high_risk_drift"
    if from_identity == "trend" and to_identity == "value":
        proof = {"new_fundamental_evidence", "valuation_change", "research_added"}
        if not evidence_note or not (trigger_set & proof):
            return "suspected_stop_avoidance"
        return "needs_review"
    if from_identity == "trial" and to_identity in {"value", "trend", "allocation"}:
        if not evidence_note or not trigger_set:
            return "needs_review"
        return "reasonable"
    if from_identity == "allocation" and to_identity == "trend":
        return "high_risk_drift"
    if evidence_note and trigger_set:
        return "reasonable"
    return "needs_review"


def _infer_review_status(
    review_status: str | None,
    *,
    buy_reason: str | None = None,
    sell_reason: str | None = None,
    review_tags: list[str] | None = None,
    trade_intent: str | None = None,
    trade_plan: str | None = None,
    review_note: str | None = None,
    review_due_date: date | None = None,
    initial_position_identity: str | None = None,
    current_position_identity: str | None = None,
    identity_confidence: str | None = None,
    sell_rule_type: str | None = None,
    next_review_date: date | None = None,
) -> str:
    if review_status is not None:
        return review_status
    has_review_content = any([
        buy_reason,
        sell_reason,
        review_tags,
        trade_intent,
        trade_plan,
        review_note,
        review_due_date,
        initial_position_identity,
        current_position_identity,
        identity_confidence,
        sell_rule_type,
        next_review_date,
    ])
    return "reviewed" if has_review_content else "auto"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _symbol_digits(value: Any) -> str:
    return "".join(ch for ch in _text(value) if ch.isdigit())


def _name_needs_enrichment(symbol: Any, name: Any) -> bool:
    text = _text(name)
    if not text:
        return True
    symbol_text = _text(symbol)
    if symbol_text and text.casefold() == symbol_text.casefold():
        return True
    digits = _symbol_digits(symbol) or _symbol_digits(text)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        raw = text.upper()
        code_like = {
            digits,
            f"{exchange}{digits}",
            f"{digits}.{exchange}",
            f"{digits}.{exchange.lower()}",
            f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
        }
        if raw in {item.upper() for item in code_like}:
            return True
    return any(token in text for token in ("�", "Ã", "Â"))


def _symbol_name_keys(symbol: Any) -> set[str]:
    raw = _text(symbol)
    if not raw:
        return set()
    keys = {raw, raw.upper(), raw.lower()}
    digits = _symbol_digits(raw)
    if len(digits) == 6:
        exchange = "SH" if digits.startswith("6") else "SZ"
        keys.update(
            {
                digits,
                f"{digits}.{exchange}",
                f"{digits}.{exchange.lower()}",
                f"{exchange.lower()}{digits}",
                f"{exchange}{digits}",
                f"SSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
                f"SHSE:{digits}" if exchange == "SH" else f"SZSE:{digits}",
            }
        )
    return {key.casefold() for key in keys if key}


def _put_asset_name(lookup: dict[str, str], symbol: Any, name: Any) -> None:
    label = _text(name)
    if not label or _name_needs_enrichment(symbol, label):
        return
    for key in _symbol_name_keys(symbol):
        lookup.setdefault(key, label)


def _currency_pair_name(symbol: Any) -> str:
    raw = _text(symbol).upper().replace("/", ".")
    parts = raw.split(".")
    if len(parts) != 2 or not all(len(part) == 3 and part.isalpha() for part in parts):
        return ""
    base = CURRENCY_LABELS.get(parts[0], parts[0])
    quote = CURRENCY_LABELS.get(parts[1], parts[1])
    return f"{base}/{quote} 外汇"


def _asset_name_lookup(cur, symbols: set[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    query_symbols = sorted(set().union(*(_symbol_name_keys(symbol) for symbol in symbols)))
    digits = sorted({_symbol_digits(symbol) for symbol in symbols if len(_symbol_digits(symbol)) == 6})
    if not query_symbols and not digits:
        return lookup

    if _table_exists(cur, "pams_asset_master"):
        cur.execute(
            """
            SELECT primary_symbol AS symbol, COALESCE(NULLIF(display_name, ''), canonical_name) AS name
            FROM pams_asset_master
            WHERE COALESCE(primary_symbol, '') <> ''
              AND COALESCE(COALESCE(NULLIF(display_name, ''), canonical_name), '') <> ''
              AND (
                LOWER(primary_symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(primary_symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY confidence DESC NULLS LAST, updated_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pams_asset_alias") and _table_exists(cur, "pams_asset_master"):
        cur.execute(
            f"""
            SELECT al.alias_value AS symbol, COALESCE(NULLIF(am.display_name, ''), am.canonical_name) AS name
            FROM pams_asset_alias al
            JOIN pams_asset_master am ON am.asset_id = al.asset_id
            WHERE {identity_alias_sql('al')}
              AND COALESCE(al.alias_value, '') <> ''
              AND COALESCE(COALESCE(NULLIF(am.display_name, ''), am.canonical_name), '') <> ''
              AND (
                LOWER(al.alias_value) = ANY(%s::text[])
                OR regexp_replace(COALESCE(al.alias_value, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY al.priority DESC NULLS LAST, al.created_at DESC NULLS LAST
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pams_holdings_snapshot"):
        cur.execute(
            """
            SELECT symbol, name
            FROM pams_holdings_snapshot
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            ORDER BY as_of DESC
            """,
            (query_symbols, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))

    if _table_exists(cur, "pit_security_master"):
        cur.execute(
            """
            SELECT symbol, ts_code, name
            FROM pit_security_master
            WHERE COALESCE(name, '') <> ''
              AND (
                LOWER(symbol) = ANY(%s::text[])
                OR LOWER(ts_code) = ANY(%s::text[])
                OR regexp_replace(COALESCE(symbol, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
                OR regexp_replace(COALESCE(ts_code, ''), '[^0-9]', '', 'g') = ANY(%s::text[])
              )
            """,
            (query_symbols, query_symbols, digits, digits),
        )
        for row in cur.fetchall():
            _put_asset_name(lookup, row.get("symbol"), row.get("name"))
            _put_asset_name(lookup, row.get("ts_code"), row.get("name"))

    return lookup


def _enrich_transaction_names(cur, rows: list[dict[str, Any]]) -> None:
    missing_symbols = {
        _text(row.get("symbol"))
        for row in rows
        if _name_needs_enrichment(row.get("symbol"), row.get("name"))
    }
    if not missing_symbols:
        return
    lookup = _asset_name_lookup(cur, missing_symbols)
    for row in rows:
        if not _name_needs_enrichment(row.get("symbol"), row.get("name")):
            continue
        for key in _symbol_name_keys(row.get("symbol")):
            label = lookup.get(key)
            if label:
                row["name"] = label
                break
        else:
            pair_name = _currency_pair_name(row.get("symbol") or row.get("name"))
            if pair_name:
                row["name"] = pair_name


def _asset_filter_display_name(row: dict[str, Any]) -> str:
    symbol = _text(row.get("symbol"))
    name = row.get("name")
    if not _asset_filter_name_needs_enrichment(row):
        return _text(name)
    pair_name = _currency_pair_name(symbol or name or row.get("asset_key"))
    if pair_name:
        return pair_name
    return symbol or _text(name) or _text(row.get("asset_key")) or "未命名标的"


def _asset_filter_name_needs_enrichment(row: dict[str, Any]) -> bool:
    symbol = _text(row.get("symbol"))
    name = row.get("name")
    if symbol:
        return _name_needs_enrichment(symbol, name)
    return _name_needs_enrichment(None, name)


def _asset_filter_lookup_values(row: dict[str, Any]) -> tuple[str, ...]:
    symbol = _text(row.get("symbol"))
    name = _text(row.get("name"))
    asset_key = _text(row.get("asset_key"))
    if symbol:
        return (symbol,)
    if _name_needs_enrichment(None, name):
        return tuple(value for value in (asset_key, name) if value)
    return ()


def _enrich_asset_filter_options(cur, assets: list[dict[str, Any]]) -> None:
    missing_symbols: set[str] = set()
    for row in assets:
        if not _asset_filter_name_needs_enrichment(row):
            continue
        for value in _asset_filter_lookup_values(row):
            text = _text(value)
            if text:
                missing_symbols.add(text)

    lookup = _asset_name_lookup(cur, missing_symbols) if missing_symbols else {}
    for row in assets:
        if _asset_filter_name_needs_enrichment(row):
            for value in _asset_filter_lookup_values(row):
                for key in _symbol_name_keys(value):
                    label = lookup.get(key)
                    if label:
                        row["name"] = label
                        break
                if not _asset_filter_name_needs_enrichment(row):
                    break
        row["display_name"] = _asset_filter_display_name(row)


def _fact_rows_available(cur) -> bool:
    if not _table_exists(cur, "pams_investment_trade_fact"):
        return False
    cur.execute("SELECT EXISTS (SELECT 1 FROM pams_investment_trade_fact) AS has_rows")
    row = cur.fetchone()
    return bool(row and row["has_rows"])


def _dec_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _apply_owner_amount_scope(rows: list[dict[str, Any]], account_cfg: dict[str, Any]) -> None:
    """Add personal-scope amounts while preserving raw account transaction fields."""
    for row in rows:
        owner_ratio = _owner_ratio(account_cfg, str(row.get("account_code") or ""))
        row["owner_ratio"] = owner_ratio
        row["gross_amount_personal_cny"] = None if row.get("gross_amount_cny") is None else _dec_or_zero(row["gross_amount_cny"]) * owner_ratio
        row["fees_personal_cny"] = None if row.get("fees_cny") is None else _dec_or_zero(row["fees_cny"]) * owner_ratio
        row["fees_status"] = "unknown" if row.get("fees_cny") is None else "known"


def _transaction_summary(rows: list[dict[str, Any]], data_source: str) -> dict[str, Any]:
    def total(field: str, side: str | None = None) -> float | None:
        selected = [row for row in rows if side is None or row.get("side_normalized") == side]
        if any(row.get(field) is None for row in selected):
            return None
        return float(sum((_dec_or_zero(row[field]) for row in selected), Decimal("0")))

    owner_adjusted_accounts = sorted(
        {
            str(row.get("account_code"))
            for row in rows
            if row.get("account_code") and _dec_or_zero(row.get("owner_ratio")) != Decimal("1")
        }
    )
    return {
        "count": len(rows),
        "data_source": data_source,
        "amount_scope": "account_original",
        "personal_amount_scope": "owner_ratio_adjusted",
        "has_owner_adjustments": bool(owner_adjusted_accounts),
        "owner_adjusted_accounts": owner_adjusted_accounts,
        "buy_amount_cny": total("gross_amount_cny", "buy"),
        "sell_amount_cny": total("gross_amount_cny", "sell"),
        "fees_cny": total("fees_cny"),
        "buy_amount_personal_cny": total("gross_amount_personal_cny", "buy"),
        "sell_amount_personal_cny": total("gross_amount_personal_cny", "sell"),
        "fees_personal_cny": total("fees_personal_cny"),
        "sources": sorted({row.get("source") for row in rows if row.get("source")}),
    }


def _fact_source_expr() -> str:
    return """
        CASE
            WHEN source_table = 'manual_investment_trade' THEN COALESCE(NULLIF(broker, ''), 'manual')
            WHEN source_table = 'tradingview_csv' THEN 'tradingview'
            WHEN source_table = 'ibkr_flex_trade' THEN 'ibkr'
            ELSE COALESCE(NULLIF(broker, ''), source_table)
        END
    """


def _fact_major_category_expr() -> str:
    raw_major = """
        COALESCE(
            NULLIF(f.asset_major_category, ''),
            NULLIF(f.raw_json #>> '{source_row,major_category}', ''),
            NULLIF(f.market, ''),
            NULLIF(f.trade_type, '')
        )
    """
    return f"""
        CASE
            WHEN {raw_major} IN ('A?', 'CN_A') THEN 'A股'
            WHEN f.account_code = 'tonghuashun' AND COALESCE(f.symbol, '') ~ '^[0-9]{{6}}$' THEN 'A股'
            WHEN {raw_major} IN ('STK', 'US_STOCK') THEN '美股'
            WHEN {raw_major} IN ('CASH') THEN '现金'
            ELSE {raw_major}
        END
    """


def _fact_minor_category_expr() -> str:
    raw_minor = """
        COALESCE(
            NULLIF(f.asset_minor_category, ''),
            NULLIF(f.raw_json #>> '{source_row,minor_category}', ''),
            NULLIF(f.market, ''),
            NULLIF(f.trade_type, '')
        )
    """
    return f"""
        CASE
            WHEN {raw_minor} IN ('A?', 'CN_A') THEN 'A股'
            WHEN f.account_code = 'tonghuashun' AND COALESCE(f.symbol, '') ~ '^[0-9]{{6}}$' THEN 'A股'
            WHEN {raw_minor} IN ('STK', 'US_STOCK') THEN '美股'
            WHEN {raw_minor} IN ('CASH') THEN '现金/活期'
            ELSE {raw_minor}
        END
    """


def _fact_query(where_sql: str) -> str:
    source_expr = _fact_source_expr()
    major_expr = _fact_major_category_expr()
    minor_expr = _fact_minor_category_expr()
    return f"""
        WITH base AS (
            SELECT
                'fact-' || f.trade_id::text AS row_id,
                f.trade_id AS native_id,
                {source_expr} AS source,
                f.source_key,
                f.account_code,
                f.trade_date,
                COALESCE(f.trade_time, f.trade_date::timestamp) AS trade_time,
                {major_expr} AS major_category,
                {minor_expr} AS minor_category,
                f.symbol,
                f.name,
                COALESCE(NULLIF(f.side, ''), f.trade_type) AS side,
                f.quantity,
                f.price,
                f.tax_amount,
                ABS(f.gross_amount) AS gross_amount,
                f.net_cash,
                CASE WHEN f.source_table='ibkr_flex_trade' AND f.fee_amount<>0
                         AND UPPER(f.fee_currency) IS DISTINCT FROM UPPER(f.currency) THEN NULL
                    WHEN f.tax_amount IS NULL AND f.raw_json #>> '{{source_row,fee_scope}}' = 'total_charges'
                    THEN ABS(f.fee_amount) ELSE ABS(f.fee_amount + f.tax_amount) END AS fees,
                f.currency,
                CASE
                    WHEN f.review_status IN ('auto', 'needs_review', 'reviewed', 'ignored') THEN f.review_status
                    ELSE 'auto'
                END AS status,
                COALESCE(
                    NULLIF(f.raw_json #>> '{{source_row,notes}}', ''),
                    NULLIF(f.raw_json #>> '{{source_row,source_file}}', '')
                ) AS notes,
                f.buy_reason,
                f.sell_reason,
                COALESCE(f.review_tags, ARRAY[]::text[]) AS review_tags,
                f.trade_intent,
                f.trade_plan,
                f.review_note,
                f.review_due_date,
                f.reviewed_at,
                f.initial_position_identity,
                f.current_position_identity,
                f.identity_transition_type,
                f.identity_transition_triggers,
                f.identity_transition_evidence,
                f.identity_transition_quality,
                f.identity_transition_at,
                f.identity_confidence,
                f.intended_holding_period,
                f.buy_logic,
                f.add_condition,
                f.reduce_condition,
                f.sell_condition,
                f.max_acceptable_loss_pct,
                f.max_position_pct,
                f.next_review_date,
                f.sell_rule_type,
                f.created_at,
                COALESCE(a.display_name, f.account_code) AS account_name,
                CASE
                    WHEN f.trade_type IN ('buy', 'fund_subscription') THEN 'buy'
                    WHEN f.trade_type IN ('sell', 'fund_redemption') THEN 'sell'
                    WHEN f.trade_type IN ('dividend', 'interest', 'coupon') THEN 'dividend'
                    WHEN f.trade_type = 'fee' THEN 'fee'
                    WHEN f.trade_type = 'tax' THEN 'tax'
                    WHEN f.trade_type = 'fx_conversion' THEN 'fx_conversion'
                    ELSE LOWER(COALESCE(NULLIF(f.side, ''), f.trade_type))
                END AS side_normalized,
                fx.rate AS fx_rate,
                CASE WHEN f.gross_amount=0 THEN 0 ELSE ABS(f.gross_amount) * fx.rate END AS gross_amount_cny,
                CASE WHEN f.net_cash=0 THEN 0 ELSE f.net_cash * fx.rate END AS net_cash_cny,
                fee.amount * COALESCE(fx.rate, CASE WHEN fee.amount=0 THEN 0 END) AS fees_cny
            FROM pams_investment_trade_fact f
            LEFT JOIN pams_accounts a ON a.account_code = f.account_code
            LEFT JOIN LATERAL (
                SELECT CASE WHEN f.source_table='ibkr_flex_trade' AND f.fee_amount<>0
                             AND UPPER(f.fee_currency) IS DISTINCT FROM UPPER(f.currency) THEN NULL
                    WHEN f.tax_amount IS NULL AND f.raw_json #>> '{{source_row,fee_scope}}' = 'total_charges'
                    THEN ABS(f.fee_amount) ELSE ABS(f.fee_amount + f.tax_amount) END AS amount
            ) fee ON TRUE
            LEFT JOIN LATERAL (
                SELECT CASE WHEN UPPER(f.currency) = 'CNY' THEN 1::numeric ELSE (
                    SELECT r.rate FROM pams_fx_rates_daily r
                    WHERE r.base_currency = 'CNY' AND r.quote_currency = UPPER(f.currency)
                      AND r.as_of = f.trade_date AND r.rate > 0
                      AND r.rate::text NOT IN ('NaN', 'Infinity', '-Infinity')
                      AND NULLIF(BTRIM(r.source),'') IS NOT NULL
                    LIMIT 1
                ) END AS rate
            ) fx ON TRUE
        )
        SELECT *
        FROM base
        {where_sql}
        ORDER BY trade_time DESC, row_id DESC
        LIMIT %s
    """


@router.get("/investment-transactions")
def list_investment_transactions(
    start: Optional[date] = None,
    end: Optional[date] = None,
    account_code: Optional[str] = None,
    source: Optional[str] = None,
    side: Optional[str] = None,
    review_status: Optional[str] = None,
    asset_key: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    """List normalized investment transactions across manual and imported ledgers."""
    with get_conn() as conn:
        ensure_ledger_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = []
            params: list[Any] = []
            if start:
                where.append("trade_date >= %s")
                params.append(start)
            if end:
                where.append("trade_date <= %s")
                params.append(end)
            if account_code:
                where.append("account_code = %s")
                params.append(account_code)
            if source:
                where.append("source = %s")
                params.append(source)
            if side:
                where.append(
                    """
                    CASE
                        WHEN lower(side) IN ('buy', 'bought', '买入', '定投') THEN 'buy'
                        WHEN lower(side) IN ('sell', 'sold', '取出', '赎回') THEN 'sell'
                        WHEN lower(side) IN ('dividend', '分红') THEN 'dividend'
                        ELSE lower(side)
                    END = %s
                    """
                )
                params.append(side.lower())
            if review_status:
                status = _validate_enum(review_status, REVIEW_STATUS_VALUES, "review_status")
                where.append("status = %s")
                params.append(status)
            if asset_key:
                key = _clean_optional_text(asset_key, max_len=160)
                if key:
                    where.append("LOWER(COALESCE(NULLIF(symbol, ''), NULLIF(name, ''), '')) = %s")
                    params.append(key.lower())
            if search:
                where.append(
                    """
                    (
                        COALESCE(symbol, '') ILIKE %s
                        OR COALESCE(name, '') ILIKE %s
                        OR COALESCE(notes, '') ILIKE %s
                        OR COALESCE(trade_intent, '') ILIKE %s
                        OR COALESCE(trade_plan, '') ILIKE %s
                        OR COALESCE(review_note, '') ILIKE %s
                    )
                    """
                )
                pattern = f"%{search}%"
                params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

            where_sql = "WHERE " + " AND ".join(where) if where else ""
            if not _fact_rows_available(cur):
                return {"summary": _transaction_summary([], "trade_fact"), "rows": []}
            data_source = "trade_fact"
            query = _fact_query(where_sql)
            cur.execute(query, params + [limit])
            rows = [dict(r) for r in cur.fetchall()]
            _enrich_transaction_names(cur, rows)
            _apply_owner_amount_scope(rows, _load_accounts_config())
            summary = _transaction_summary(rows, data_source)

    return {"summary": summary, "rows": [{k: norm(v) for k, v in r.items()} for r in rows]}


@router.post("/investment-transactions")
def create_investment_transaction(tx: InvestmentTransactionCreate):
    """Legacy entry: all new imports require the shared draft/preview approval."""
    raise HTTPException(409, detail={"code": "import_review_required",
        "message": "请通过导入资料统一核对后入库", "destination": "account_import.html?kind=trade&mode=manual"})


@router.get("/investment-transactions/attribution/summary")
def investment_trade_attribution_summary(
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(300, ge=1, le=1000),
):
    """Return event-level attribution grouped by manual review enums."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                return {
                    "as_of": today_local().isoformat(),
                    "sample_count": 0,
                    "missing_price_count": 0,
                    "groups": {
                        "buy_reason": [],
                        "sell_reason": [],
                        "review_tags": [],
                    },
                    "events": [],
                    "data_quality": {"note": "investment trade fact table not found"},
                }
            return build_trade_attribution_summary_payload(cur, start=start, end=end, limit=limit)


@router.get("/investment-transactions/campaigns")
def investment_trade_campaigns(
    start: Optional[date] = None,
    end: Optional[date] = None,
    account_code: Optional[str] = None,
    asset_key: Optional[str] = None,
    status: str = Query("all"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return read-only account/asset position lifecycle campaigns."""
    allowed_status = {"all", "open", "closed", "amount_only", "orphan_sell"}
    normalized_status = status if status in allowed_status else "all"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                return {
                    "as_of": date.today().isoformat(),
                    "start": start.isoformat() if start else None,
                    "end": end.isoformat() if end else None,
                    "account_code": account_code,
                    "asset_key": asset_key,
                    "status": normalized_status,
                    "limit": limit,
                    "summary": {
                        "total_campaigns": 0,
                        "status_counts": {},
                        "open_campaigns": 0,
                        "closed_campaigns": 0,
                        "amount_only_campaigns": 0,
                        "orphan_sell_campaigns": 0,
                    },
                    "campaigns": [],
                    "data_quality": {"note": "investment trade fact table not found"},
                }
            return build_position_campaigns_payload(
                cur,
                start=start,
                end=end,
                account_code=account_code,
                asset_key=asset_key,
                status=normalized_status,
                limit=limit,
            )


@router.get("/investment-transactions/auto-review/queue")
def investment_trade_auto_review_queue(
    kind: str = Query("needs_human_review"),
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(100, ge=1, le=300),
):
    """Return system review queue for passive trade-review agents."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                return {
                    "as_of": date.today().isoformat(),
                    "kind": kind,
                    "limit": limit,
                    "items": [],
                    "counts": {},
                    "data_quality": {"note": "investment trade fact table not found"},
                }
            return build_trade_auto_review_queue_payload(cur, kind=kind, start=start, end=end, limit=limit)


@router.post("/investment-transactions/auto-review/apply")
def investment_trade_auto_review_apply(
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = Query(1000, ge=1, le=5000),
    dry_run: bool = Query(True),
    include_reviewed_tags: bool = Query(True),
):
    """Conservatively fill missing trade review annotations from system review."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                return {
                    "as_of": date.today().isoformat(),
                    "dry_run": dry_run,
                    "scanned_count": 0,
                    "changed_count": 0,
                    "updated_count": 0,
                    "field_counts": {},
                    "examples": [],
                    "data_quality": {"note": "investment trade fact table not found"},
                }
            result = apply_auto_trade_annotations(
                cur,
                start=start,
                end=end,
                limit=limit,
                dry_run=dry_run,
                include_reviewed_tags=include_reviewed_tags,
            )
            if not dry_run:
                conn.commit()
            return result


@router.get("/investment-transactions/{trade_id}/attribution")
def investment_trade_attribution(trade_id: int):
    """Return automatic post-trade attribution for one trade event."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                raise HTTPException(status_code=404, detail="investment trade fact table not found")
            payload = build_trade_attribution_payload(cur, trade_id)
            if not payload.get("found"):
                raise HTTPException(status_code=404, detail=f"trade not found: {trade_id}")
            return payload


@router.post("/investment-transactions/{trade_id}/review")
def update_investment_trade_review(trade_id: int, review: InvestmentTradeReviewUpdate):
    """Update manual review fields for one investment trade fact."""
    buy_reason = _validate_enum(review.buy_reason, BUY_REASON_VALUES, "buy_reason")
    sell_reason = _validate_enum(review.sell_reason, SELL_REASON_VALUES, "sell_reason")
    review_status = _validate_enum(review.review_status, REVIEW_STATUS_VALUES, "review_status")
    review_tags = _validate_review_tags(review.review_tags)
    initial_position_identity = _validate_enum(
        review.initial_position_identity,
        INITIAL_POSITION_IDENTITY_VALUES,
        "initial_position_identity",
    )
    current_position_identity = _validate_enum(
        review.current_position_identity,
        POSITION_IDENTITY_VALUES,
        "current_position_identity",
    )
    identity_confidence = _validate_enum(review.identity_confidence, IDENTITY_CONFIDENCE_VALUES, "identity_confidence")
    sell_rule_type = _validate_enum(review.sell_rule_type, SELL_RULE_TYPE_VALUES, "sell_rule_type")
    transition_type = _validate_enum(review.identity_transition_type, TRANSITION_TYPE_VALUES, "identity_transition_type")
    transition_triggers = _validate_transition_triggers(review.identity_transition_triggers)
    trade_intent_provided = _field_was_provided(review, "trade_intent")
    trade_plan_provided = _field_was_provided(review, "trade_plan")
    review_note_provided = _field_was_provided(review, "review_note")
    review_due_date_provided = _field_was_provided(review, "review_due_date")
    identity_fields_provided = {
        field: _field_was_provided(review, field)
        for field in [
            "initial_position_identity",
            "current_position_identity",
            "identity_confidence",
            "intended_holding_period",
            "buy_logic",
            "add_condition",
            "reduce_condition",
            "sell_condition",
            "max_acceptable_loss_pct",
            "max_position_pct",
            "next_review_date",
            "sell_rule_type",
        ]
    }
    trade_intent = _clean_optional_text(review.trade_intent) if trade_intent_provided else None
    trade_plan = _clean_optional_text(review.trade_plan) if trade_plan_provided else None
    review_note = _clean_optional_text(review.review_note)
    intended_holding_period = _clean_optional_text(review.intended_holding_period, max_len=120)
    buy_logic = _clean_optional_text(review.buy_logic)
    add_condition = _clean_optional_text(review.add_condition)
    reduce_condition = _clean_optional_text(review.reduce_condition)
    sell_condition = _clean_optional_text(review.sell_condition)
    max_acceptable_loss_pct = _clean_optional_float(review.max_acceptable_loss_pct)
    max_position_pct = _clean_optional_float(review.max_position_pct)
    transition_evidence = _clean_optional_text(review.identity_transition_evidence) or review_note

    review_status = _infer_review_status(
        review_status,
        buy_reason=buy_reason,
        sell_reason=sell_reason,
        review_tags=review_tags,
        trade_intent=trade_intent if trade_intent_provided else None,
        trade_plan=trade_plan if trade_plan_provided else None,
        review_note=review_note,
        review_due_date=review.review_due_date if review_due_date_provided else None,
        initial_position_identity=initial_position_identity if identity_fields_provided["initial_position_identity"] else None,
        current_position_identity=current_position_identity if identity_fields_provided["current_position_identity"] else None,
        identity_confidence=identity_confidence if identity_fields_provided["identity_confidence"] else None,
        sell_rule_type=sell_rule_type if identity_fields_provided["sell_rule_type"] else None,
        next_review_date=review.next_review_date if identity_fields_provided["next_review_date"] else None,
    )

    with get_conn() as conn:
        ensure_ledger_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _table_exists(cur, "pams_investment_trade_fact"):
                raise HTTPException(status_code=404, detail="investment trade fact table not found")
            cur.execute(
                """
                SELECT current_position_identity, review_status
                FROM pams_investment_trade_fact
                WHERE trade_id = %s
                FOR UPDATE
                """,
                (trade_id,),
            )
            before_row = cur.fetchone()
            if not before_row:
                raise HTTPException(status_code=404, detail=f"trade not found: {trade_id}")
            if (not _field_was_provided(review, "review_status")
                    and before_row.get("review_status") in {"reviewed", "ignored"}):
                review_status = before_row["review_status"]
            old_identity = before_row.get("current_position_identity")
            record_transition = bool(
                identity_fields_provided["current_position_identity"]
                and old_identity and current_position_identity and old_identity != current_position_identity
            )
            transition_quality = (
                _infer_transition_quality(old_identity, current_position_identity, transition_type,
                                          transition_triggers, transition_evidence)
                if record_transition else None
            )
            cur.execute(
                """
                UPDATE pams_investment_trade_fact
                SET
                    buy_reason = %s,
                    sell_reason = %s,
                    review_tags = %s::text[],
                    trade_intent = CASE WHEN %s THEN %s ELSE trade_intent END,
                    trade_plan = CASE WHEN %s THEN %s ELSE trade_plan END,
                    review_note = CASE WHEN %s THEN %s ELSE review_note END,
                    review_due_date = CASE WHEN %s THEN %s ELSE review_due_date END,
                    initial_position_identity = CASE WHEN %s THEN %s ELSE initial_position_identity END,
                    current_position_identity = CASE WHEN %s THEN %s ELSE current_position_identity END,
                    identity_transition_type = CASE WHEN %s THEN %s ELSE identity_transition_type END,
                    identity_transition_triggers = CASE WHEN %s THEN %s::text[] ELSE identity_transition_triggers END,
                    identity_transition_evidence = CASE WHEN %s THEN %s ELSE identity_transition_evidence END,
                    identity_transition_quality = CASE WHEN %s THEN %s ELSE identity_transition_quality END,
                    identity_transition_at = CASE WHEN %s THEN NOW() ELSE identity_transition_at END,
                    identity_confidence = CASE WHEN %s THEN %s ELSE identity_confidence END,
                    intended_holding_period = CASE WHEN %s THEN %s ELSE intended_holding_period END,
                    buy_logic = CASE WHEN %s THEN %s ELSE buy_logic END,
                    add_condition = CASE WHEN %s THEN %s ELSE add_condition END,
                    reduce_condition = CASE WHEN %s THEN %s ELSE reduce_condition END,
                    sell_condition = CASE WHEN %s THEN %s ELSE sell_condition END,
                    max_acceptable_loss_pct = CASE WHEN %s THEN %s ELSE max_acceptable_loss_pct END,
                    max_position_pct = CASE WHEN %s THEN %s ELSE max_position_pct END,
                    next_review_date = CASE WHEN %s THEN %s ELSE next_review_date END,
                    sell_rule_type = CASE WHEN %s THEN %s ELSE sell_rule_type END,
                    review_status = %s,
                    reviewed_at = CASE WHEN %s = 'reviewed' THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE trade_id = %s
                RETURNING
                    trade_id,
                    review_status,
                    buy_reason,
                    sell_reason,
                    review_tags,
                    trade_intent,
                    trade_plan,
                    review_note,
                    review_due_date,
                    initial_position_identity,
                    current_position_identity,
                    identity_transition_type,
                    identity_transition_triggers,
                    identity_transition_evidence,
                    identity_transition_quality,
                    identity_transition_at,
                    identity_confidence,
                    intended_holding_period,
                    buy_logic,
                    add_condition,
                    reduce_condition,
                    sell_condition,
                    max_acceptable_loss_pct,
                    max_position_pct,
                    next_review_date,
                    sell_rule_type,
                    reviewed_at
                """,
                (
                    buy_reason,
                    sell_reason,
                    review_tags,
                    trade_intent_provided,
                    trade_intent,
                    trade_plan_provided,
                    trade_plan,
                    review_note_provided,
                    review_note,
                    review_due_date_provided,
                    review.review_due_date,
                    identity_fields_provided["initial_position_identity"],
                    initial_position_identity,
                    identity_fields_provided["current_position_identity"],
                    current_position_identity,
                    record_transition,
                    transition_type or "other",
                    record_transition,
                    transition_triggers,
                    record_transition,
                    transition_evidence,
                    record_transition,
                    transition_quality,
                    record_transition,
                    identity_fields_provided["identity_confidence"],
                    identity_confidence,
                    identity_fields_provided["intended_holding_period"],
                    intended_holding_period,
                    identity_fields_provided["buy_logic"],
                    buy_logic,
                    identity_fields_provided["add_condition"],
                    add_condition,
                    identity_fields_provided["reduce_condition"],
                    reduce_condition,
                    identity_fields_provided["sell_condition"],
                    sell_condition,
                    identity_fields_provided["max_acceptable_loss_pct"],
                    max_acceptable_loss_pct,
                    identity_fields_provided["max_position_pct"],
                    max_position_pct,
                    identity_fields_provided["next_review_date"],
                    review.next_review_date,
                    identity_fields_provided["sell_rule_type"],
                    sell_rule_type,
                    review_status,
                    review_status,
                    trade_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"trade not found: {trade_id}")
        conn.commit()

    return {"trade": {k: norm(v) for k, v in dict(row).items()}, "review_options": _review_options_payload()}


@router.get("/investment-transactions/options")
def investment_transaction_options():
    """Return filter and form options for the investment ledger page."""
    with get_conn() as conn:
        ensure_ledger_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            major_expr = _fact_major_category_expr()
            minor_expr = _fact_minor_category_expr()
            cur.execute(
                """
                SELECT account_code, display_name, base_currency
                FROM pams_accounts
                WHERE active = true
                ORDER BY display_name
                """
            )
            accounts = [dict(r) for r in cur.fetchall()]
            cur.execute(
                f"""
                WITH categories AS (
                    SELECT DISTINCT major_category, minor_category
                    FROM pams_holdings_snapshot
                    WHERE COALESCE(major_category, '') <> ''
                    UNION
                    SELECT DISTINCT
                        {major_expr} AS major_category,
                        {minor_expr} AS minor_category
                    FROM pams_investment_trade_fact f
                    UNION
                    SELECT DISTINCT asset_type AS major_category, asset_subtype AS minor_category
                    FROM pams_asset_master
                    WHERE COALESCE(asset_type, '') <> ''
                )
                SELECT major_category, minor_category
                FROM categories
                WHERE COALESCE(major_category, '') <> ''
                ORDER BY major_category, minor_category NULLS LAST
                """
            )
            categories = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                WITH sources AS (
                    SELECT DISTINCT
                        CASE
                            WHEN source_table = 'manual_investment_trade' THEN COALESCE(NULLIF(broker, ''), 'manual')
                            WHEN source_table = 'tradingview_csv' THEN 'tradingview'
                            WHEN source_table = 'ibkr_flex_trade' THEN 'ibkr'
                            ELSE COALESCE(NULLIF(broker, ''), source_table)
                        END AS source
                    FROM pams_investment_trade_fact
                    UNION SELECT 'manual'
                )
                SELECT source
                FROM sources
                WHERE COALESCE(source, '') <> ''
                ORDER BY source
                """
            )
            sources = [r["source"] for r in cur.fetchall()]
            assets: list[dict[str, Any]] = []
            if _table_exists(cur, "pams_investment_trade_fact"):
                cur.execute(
                    """
                    WITH reviewable AS (
                        SELECT
                            LOWER(COALESCE(NULLIF(symbol, ''), NULLIF(name, ''), '')) AS asset_key,
                            NULLIF(symbol, '') AS symbol,
                            NULLIF(name, '') AS name,
                            trade_type,
                            buy_reason,
                            sell_reason,
                            initial_position_identity,
                            current_position_identity
                        FROM pams_investment_trade_fact
                        WHERE trade_type IN ('buy', 'sell', 'fund_subscription', 'fund_redemption')
                          AND COALESCE(COALESCE(NULLIF(symbol, ''), NULLIF(name, '')), '') <> ''
                    )
                    SELECT
                        asset_key,
                        MAX(symbol) AS symbol,
                        MAX(name) AS name,
                        COUNT(*) AS trade_count,
                        COUNT(*) FILTER (
                            WHERE
                                (
                                    trade_type IN ('buy', 'fund_subscription')
                                    AND (
                                        buy_reason IS NULL
                                        OR initial_position_identity IS NULL
                                        OR current_position_identity IS NULL
                                    )
                                )
                                OR (trade_type IN ('sell', 'fund_redemption') AND sell_reason IS NULL)
                        ) AS pending_count
                    FROM reviewable
                    GROUP BY asset_key
                    ORDER BY pending_count DESC, trade_count DESC, COALESCE(MAX(name), MAX(symbol), asset_key)
                    LIMIT 500
                    """
                )
                assets = [dict(r) for r in cur.fetchall()]
                _enrich_asset_filter_options(cur, assets)

    return {
        "accounts": [{k: norm(v) for k, v in r.items()} for r in accounts],
        "categories": [{k: norm(v) for k, v in r.items()} for r in categories],
        "sources": sources,
        "assets": [{k: norm(v) for k, v in r.items()} for r in assets],
        "side_options": ["buy", "sell", "deposit", "withdraw", "dividend", "fx_conversion", "fee", "other"],
        "review_options": _review_options_payload(),
    }
