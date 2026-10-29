"""Evidence-based adapter for the broker ledger exported by THS, not a writer."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable
import re

PROFILE = "ths_broker_ledger_v1"
REQUIRED = {"成交日期", "证券代码", "证券名称", "操作", "成交数量", "成交均价", "成交金额", "发生金额"}
EXTRA_HEADERS = {"其他杂费", "本次金额", "合同编号", "交易市场", "余额", "定位串", "市场代码"}
MAPPING = {"business_date": "成交日期", "trade_time": "成交时间", "symbol": "证券代码",
           "name": "证券名称", "action": "操作", "quantity": "成交数量", "price": "成交均价",
           "gross_amount": "成交金额", "net_cash": "发生金额", "fee_amount": "手续费", "tax_amount": "印花税"}
ACTIONS = {"证券买入": "buy", "证券卖出": "sell", "红利入账": "dividend", "现金红利": "dividend",
           "红利税补扣": "tax", "利息归本": "interest", "银行转证券": "deposit", "证券转银行": "withdraw"}
CASH_ACTIONS = {"deposit", "withdraw", "interest"}
TRADE_ACTIONS = {"stock": ["buy", "sell", "dividend", "fee", "tax"],
                 "fund": ["buy", "sell", "dividend", "fee", "tax"],
                 "cash": ["deposit", "withdraw", "interest"]}


def matches(headers: list[str]) -> bool:
    return REQUIRED <= set(headers)


def native_row(batch: dict, row: dict) -> bool:
    return batch["account_code"] == "tonghuashun" and batch["kind"] == "trade" and row.get("source", {}).get("profile") == PROFILE


def stable_reference(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text and text not in {"-", "--", "N/A"} and not re.fullmatch(r"0+", text) else None


def normalize(fields: dict, raw: dict, parse_value: Callable, mapping: dict) -> tuple[dict, list[dict]]:
    problems: list[dict] = []
    source: dict = {"profile": PROFILE, "broker_action": raw.get("操作"), "market": raw.get("交易市场"),
                    "market_code": raw.get("市场代码"), "fee_components": {k: raw.get(k) for k in ("手续费", "印花税", "其他杂费")},
                    "trade_actions": {kind: list(actions) for kind, actions in TRADE_ACTIONS.items()}}

    def problem(field, code, message, severity="error"):
        problems.append({"field": field, "code": code, "message": message, "severity": severity})

    def number(header):
        try:
            value = parse_value("fee_amount", raw.get(header))
            return Decimal(value) if value is not None else None
        except (ValueError, ArithmeticError):
            problem("fee_amount", "invalid_fee_component", "费用分项无效：" + header)
            return None

    for field, expected in MAPPING.items():
        if field in mapping and mapping[field] != expected:
            problem(field, "broker_mapping_conflict", "原生券商列映射与字段语义不符，请核对：" + field)
    if ("fee_scope" in mapping or "费用口径" in raw) and fields.get("fee_scope") not in {None, "separate"}:
        problem("fee_scope", "broker_mapping_conflict", "原始手续费、印花税和杂费为分列，不能再按税费合计重复解释")
    operation = ACTIONS.get(str(raw.get("操作") or "").strip())
    if operation:
        fields["action"] = operation
        if "status" not in mapping and not any(h in raw for h in ("状态", "交易状态", "确认状态")):
            fields["status"] = "confirmed"
    else:
        problem("action", "unsupported_broker_action", "尚未支持的券商业务类型：" + str(raw.get("操作") or ""))
    commission, stamp, other = (number(h) for h in ("手续费", "印花税", "其他杂费"))
    if any(v is None for v in (commission, stamp, other)):
        fields.update(fee_amount=None, tax_amount=None)
        problem("fee_amount", "missing_fee_component", "手续费、印花税和其他杂费须有明确数值，缺失不补零")
    elif any(v < 0 for v in (commission, stamp, other)):
        problem("fee_amount", "negative_fee_component", "费用分项不能为负，退款须保留独立业务证据")
        fields.update(fee_amount=None, tax_amount=None)
    else:
        fields.update(fee_amount=str(commission + other), tax_amount=str(stamp), fee_scope="separate")
    if operation in {"dividend", "tax", "interest", "deposit", "withdraw"}:
        # Non-trade share counts are entitlements/balances, not position changes.
        fields.update(quantity=None, price=None)
    if operation in CASH_ACTIONS:
        fields.update(asset_type="cash", symbol="CASH_CNY", name="人民币现金")
    if operation in {"deposit", "withdraw", "interest", "tax"} and fields.get("net_cash") is not None:
        amount = Decimal(fields["net_cash"])
        fields["gross_amount"] = str(abs(amount))
        source["gross_amount_basis"] = "发生金额"
        if operation == "tax" and fields.get("tax_amount") is not None:
            # A standalone tax debit is the tax itself, not an extra trade charge.
            if commission != 0 or stamp != 0 or other != 0:
                problem("tax_amount", "standalone_tax_components", "独立税款与非零费用分项语义不明确，不能重复计税")
            fields["tax_amount"] = str(abs(amount))
    if fields.get("asset_type") is None and fields.get("symbol"):
        problem("asset_type", "asset_master_lookup", "证券类型将在预览中按资产底表唯一匹配，不按代码前缀猜测", "warning")
    reference = stable_reference(raw.get("定位串"))
    reference_kind = "locator"
    if not reference:
        reference, reference_kind = stable_reference(raw.get("成交编号")), "execution"
    if reference:
        source["identity"] = {"type": reference_kind, "value": reference, "business_date": fields.get("business_date"),
                              "market": raw.get("市场代码") or raw.get("交易市场")}
    fields["source_ref"] = reference or stable_reference(fields.get("source_ref"))
    return source, problems
