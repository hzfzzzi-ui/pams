"""Cash ledger workbench adapters.

The ledger page is for personal cashflow review, not investment trading.
This module reads imported bank/payment statement rows, excludes recognized
internal fund movements, and groups the remaining rows into coarse consumption
and income buckets.
"""

from __future__ import annotations

import calendar
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from domain.imports.source_row_read import source_row_relation


ACCOUNTS_CONFIG_PATH = Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml"))
LOCAL_ACCOUNTS_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "accounts.yaml"

INVESTMENT_ACCOUNT_CODES = {
    "tencent_licaitong",
    "ccb_yhs300_fund",
    "tonghuashun",
    "ibkr",
}


TRANSFER_MATCH_TYPES = {
    "account_transfer",
    "investment_funding",
    "credit_card_payment",
    "payment_proxy",
    "internal_transfer",
}

INVESTMENT_TRANSFER_COUNTERPARTIES = {"direct_investment"}

TRANSFER_KEYWORDS = (
    "信用卡还款",
    "理财通",
    "腾安基金",
    "基金销售",
    "基金下账",
    "同花顺",
    "盈透",
    "ibkr",
    "证券",
    "积存金",
)

EXPENSE_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("餐饮", ("餐饮", "吃饭", "饭", "咖啡", "外卖", "美团", "饿了么", "restaurant", "rest ", "uber eats", "pickup coffee", "oxxo", "满客")),
    ("交通出行", ("交通", "打车", "滴滴", "uber ride", "taxi", "地铁", "公交", "铁路", "机票", "航旅", "trip", "suica")),
    ("居住", ("房租", "租金", "物业", "水费", "电费", "燃气", "renta")),
    ("通讯网络", ("宽带", "网络", "通信", "通讯", "移动", "联通", "电信", "话费", "手机充值", "mobile reload", "izzi")),
    ("工具订阅", ("工具软件", "订阅", "软件", "openai", "chatgpt", "claude", "cursor", "icloud", "apple.com/bill", "midjourney")),
    ("娱乐休闲", ("爱奇艺", "腾讯视频", "netflix", "spotify", "电影", "游戏", "演出", "票务", "ktv")),
    ("医疗健康", ("医院", "药", "医保", "医疗", "诊所", "体检")),
    ("学习成长", ("课程", "教育", "培训", "图书", "书店", "kindle", "得到")),
    ("人情往来", ("红包", "群收款", "扫二维码付款", "二维码收款", "微信转账", "转账备注", "transferencia a ", "transf int")),
    ("购物", ("购物", "淘宝", "天猫", "京东", "拼多多", "抖音", "商户消费", "pos", "消费", "zara", "sears", "abercrombie", "miniso", "super")),
)

EXPENSE_CATEGORY_MAP = {
    "餐饮": "餐饮",
    "交通": "交通出行",
    "居住": "居住",
    "房租": "居住",
    "通讯": "通讯网络",
    "话费": "通讯网络",
    "工具软件": "工具订阅",
    "日常消费": "购物",
    "医疗": "医疗健康",
    "交际": "人情往来",
    "往来款": "借还款/往来",
    "其他": "其他支出",
}

CLASSIFICATION_RULES = [
    {
        "name": "资金流转优先",
        "description": "只有白名单账户之间的划转、信用卡还款和投资入金不进入消费；支付通道的资金侧用于去重，商户侧仍按消费统计。",
    },
    {
        "name": "投资收益剥离",
        "description": "一般投资收益留在投资页；ESOP 分红按用户口径作为建行收入展示。",
    },
    {
        "name": "粗分类消费",
        "description": "消费先按账单现有类别归并，再用商户/摘要关键词修正为餐饮、交通、居住通信、工具订阅、购物消费、医疗健康、人情往来。",
    },
    {
        "name": "无金额特判",
        "description": "分类不根据 500、520 等固定金额判断，避免未来流水规模变化后口径漂移。",
    },
]


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def _serialize_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _month_bounds(month: str | None, latest_date: date | None = None) -> tuple[date, date]:
    if month:
        year, month_no = [int(part) for part in month.split("-", 1)]
    else:
        anchor = date.today()
        year, month_no = anchor.year, anchor.month
    end_day = calendar.monthrange(year, month_no)[1]
    month_start = date(year, month_no, 1)
    month_end = date(year, month_no, end_day)
    today = date.today()
    if year == today.year and month_no == today.month:
        month_end = min(month_end, today)
    return month_start, month_end


def _recent_start_for_account_window(month_start: date, month_end: date, recent_days: int) -> date:
    trailing_start = month_end - timedelta(days=max(recent_days, 1) - 1)
    return min(month_start, trailing_start)


def _row_text(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("category"),
        row.get("subcategory"),
        row.get("summary"),
        row.get("description"),
        row.get("counterparty"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _expense_keyword_category(text: str) -> str | None:
    for label, keywords in EXPENSE_KEYWORD_RULES:
        if _contains_any(text, keywords):
            return label
    return None


def _expense_display_category(category: str, subcategory: str, text: str) -> tuple[str, bool]:
    keyword_label = _expense_keyword_category(text)
    if keyword_label:
        return keyword_label, False
    if category == "居住" and subcategory == "网络通讯":
        return "通讯网络", False
    if category in EXPENSE_CATEGORY_MAP:
        return EXPENSE_CATEGORY_MAP[category], False
    if category in {"未分类", ""}:
        return "待确认支出", True
    return "其他支出", False


def _income_display_category(category: str, subcategory: str, text: str) -> str:
    if "退款" in text or "退货" in text or category == "消费退款":
        return "退款返现"
    if subcategory == "ESOP分红":
        return "投资收益"
    if "年终" in text or subcategory == "年终奖":
        return "奖金"
    if "工资" in text or "薪金" in text or "huawei technologies" in text or subcategory == "工资":
        return "工资薪酬"
    if "差旅" in text or "报销" in text or "补助" in text or subcategory == "差旅报销补助":
        return "报销补贴"
    if "公积金" in text or "医保" in text or category == "福利资产":
        return "福利保障"
    if "利息" in text or subcategory == "利息":
        return "利息收入"
    if "借款归还" in text or category == "往来款":
        return "借还款/往来"
    if "红包" in text or "微信转账" in text or "转账红包" in text or category == "交际":
        return "人情往来收入"
    return "其他收入"


def _payment_proxy_role(row: Mapping[str, Any]) -> str | None:
    # Roles are supplied only by verified immutable user confirmations.
    return row.get('confirmed_payment_proxy_role')


def _is_configured_transfer_match(match: Mapping[str, Any], configured_accounts: set[str]) -> bool:
    match_type = str(match.get("match_type") or "").lower()
    from_account = str(match.get("from_account_code") or "")
    to_account = str(match.get("to_account_code") or "")
    if match_type == "payment_proxy":
        return False
    if match_type == "investment_funding":
        return (
            from_account in configured_accounts
            and (to_account in configured_accounts or to_account in INVESTMENT_TRANSFER_COUNTERPARTIES)
        ) or (
            to_account in configured_accounts
            and (from_account in configured_accounts or from_account in INVESTMENT_TRANSFER_COUNTERPARTIES)
        )
    return bool(from_account and to_account and from_account in configured_accounts and to_account in configured_accounts)


def _is_transfer_like(row: Mapping[str, Any], transfer_row_ids: set[int], payment_proxy_source_row_ids: set[int] | None = None) -> bool:
    payment_proxy_source_row_ids = payment_proxy_source_row_ids or set()
    row_id = row.get("id")
    source_id = str(row.get("source_id") or "")
    source_row_id = int(source_id) if source_id.isdigit() else None
    payment_proxy_role = _payment_proxy_role(row)
    if payment_proxy_role == "target":
        return False
    if payment_proxy_role == "source" or (isinstance(source_row_id, int) and source_row_id in payment_proxy_source_row_ids):
        return True
    category = str(row.get("category") or "")
    subcategory = str(row.get("subcategory") or "")
    cashflow_type = str(row.get("cashflow_type") or "")
    text = _row_text(row)
    if row.get("is_internal_transfer") is True:
        return True
    if cashflow_type in {
        "internal_transfer",
        "investment_funding",
        "investment_withdrawal",
        "debt_repayment",
        "balance_adjustment",
    }:
        return True
    if isinstance(row_id, int) and row_id in transfer_row_ids:
        return True
    if category in {"内部划转", "支付通道"}:
        return True
    if "信用卡还款" in subcategory or "银行卡扣款映射" in subcategory:
        return True
    return _contains_any(text, TRANSFER_KEYWORDS)


def classify_ledger_row(
    row: Mapping[str, Any],
    transfer_row_ids: set[int] | None = None,
    payment_proxy_source_row_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Classify one imported statement row for the cashflow workbench.

    The classifier intentionally avoids amount thresholds. It first removes
    recognized internal fund movement, then buckets positive rows as income and
    negative rows as coarse consumption categories.
    """

    transfer_row_ids = transfer_row_ids or set()
    amount = _to_float(row.get("amount") if row.get("amount_cny") is None else row.get("amount_cny"))
    category = str(row.get("category") or "")
    subcategory = str(row.get("subcategory") or "")
    cashflow_type = str(row.get("cashflow_type") or "")
    text = _row_text(row)

    if category == "福利资产" and amount >= 0:
        return {
            "flow_type": "income",
            "coarse_category": _income_display_category(category, subcategory, text),
            "needs_review": False,
            "is_operating": True,
        }

    if _is_transfer_like(row, transfer_row_ids, payment_proxy_source_row_ids):
        if _payment_proxy_role(row) == "source":
            return {
                "flow_type": "payment_proxy_dedup",
                "coarse_category": "支付通道去重",
                "needs_review": False,
                "is_operating": False,
            }
        return {
            "flow_type": "transfer",
            "coarse_category": "资金流转",
            "needs_review": False,
            "is_operating": False,
        }

    if cashflow_type == "investment_return_cash":
        return {
            "flow_type": "investment_return",
            "coarse_category": subcategory or "鎶曡祫鏀剁泭",
            "needs_review": False,
            "is_operating": False,
        }

    if category == "福利资产":
        return {
            "flow_type": "income" if amount >= 0 else "expense",
            "coarse_category": _income_display_category(category, subcategory, text) if amount >= 0 else "医疗健康",
            "needs_review": False,
            "is_operating": True,
        }

    if category == "投资收益" and subcategory != "ESOP分红":
        return {
            "flow_type": "investment_return",
            "coarse_category": subcategory or "投资收益",
            "needs_review": False,
            "is_operating": False,
        }

    if amount >= 0:
        return {
            "flow_type": "income",
            "coarse_category": _income_display_category(category, subcategory, text),
            "needs_review": False,
            "is_operating": True,
        }

    label, needs_review = _expense_display_category(category, subcategory, text)

    return {
        "flow_type": "expense",
        "coarse_category": label,
        "needs_review": needs_review,
        "is_operating": True,
    }


def _investment_transfer_sign(match: Mapping[str, Any]) -> int:
    match_type = str(match.get("match_type") or "").lower()
    if match_type == "investment_funding":
        return -1
    if match_type == "investment_withdrawal":
        return 1
    return 0


def summarize_period(rows: list[dict[str, Any]], transfers: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    """Summarize operating cashflow and one-way internal fund movement for a period."""

    period_rows = [r for r in rows if (r.get("transaction_date_obj") and start <= r["transaction_date_obj"] <= end)]
    period_transfers = [t for t in transfers if (t.get("match_date_obj") and start <= t["match_date_obj"] <= end)]
    known_rows = [r for r in period_rows if r.get("amount_cny") is not None]
    missing_source = sum(1 for r in period_rows if r.get('amount_cny') is None
                         and str(r.get('source_issue') or '').startswith('source_evidence_'))
    missing_row_fx = sum(1 for r in period_rows if r.get('amount_cny') is None
                         and not str(r.get('source_issue') or '').startswith('source_evidence_'))
    missing_income_fx = any(r.get("amount_cny") is None and r.get("flow_type") == "income" for r in period_rows)
    missing_expense_fx = any(r.get("amount_cny") is None and r.get("flow_type") == "expense" for r in period_rows)
    missing_transfer_row_fx = any(r.get("amount_cny") is None and r.get("flow_type") == "transfer" for r in period_rows)
    income = sum(r["amount_cny"] for r in known_rows if r["flow_type"] == "income" and r["amount_cny"] > 0)
    expense = sum(abs(r["amount_cny"]) for r in known_rows if r["flow_type"] == "expense" and r["amount_cny"] < 0)
    known_transfers = [t for t in period_transfers if t.get("amount_cny") is not None]
    missing_fx = len(period_transfers) - len(known_transfers)
    transfer = sum(abs(t["amount_cny"]) for t in known_transfers)
    investment_transfer = sum(abs(t["amount_cny"]) for t in known_transfers if _investment_transfer_sign(t))
    investment_transfer_net = sum(abs(t["amount_cny"]) * _investment_transfer_sign(t) for t in known_transfers)
    disposable_net = income - expense
    cash_net = disposable_net + investment_transfer_net
    other_transfer = max(transfer - investment_transfer, 0.0)
    result = {
        "income": round(income, 2),
        "expense": round(expense, 2),
        "net": round(disposable_net, 2),
        "disposable_net": round(disposable_net, 2),
        "cash_net": round(cash_net, 2),
        "investment_transfer": round(investment_transfer, 2),
        "investment_transfer_net": round(investment_transfer_net, 2),
        "other_transfer": round(other_transfer, 2),
        "internal_transfer": round(transfer, 2),
        "pending_count": sum(1 for r in period_rows if r.get("needs_review") or r.get("amount_cny") is None),
        "row_count": len(period_rows),
        "income_count": sum(1 for r in period_rows if r["flow_type"] == "income"),
        "expense_count": sum(1 for r in period_rows if r["flow_type"] == "expense"),
    }
    if missing_fx or missing_transfer_row_fx:
        for key in ("cash_net", "investment_transfer", "investment_transfer_net", "other_transfer", "internal_transfer"):
            result[key] = None
    if missing_income_fx or missing_expense_fx:
        if missing_income_fx:
            result['income'] = None
        if missing_expense_fx:
            result['expense'] = None
        for key in ("net", "disposable_net", "cash_net"):
            result[key] = None
    if missing_fx or missing_row_fx:
        result.update(fx_status="incomplete", fx_missing_count=missing_fx + missing_row_fx)
    elif any(row.get('fx_basis') == 'daily_market_projection' for row in period_rows):
        result['fx_status'] = 'estimated'
    if missing_source:
        result.update(source_status='incomplete', source_unverified_count=missing_source)
    return result


def _group_amounts(rows: list[dict[str, Any]], flow_type: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["flow_type"] != flow_type:
            continue
        label = row["coarse_category"]
        item = grouped.setdefault(label, {"category": label, "amount": 0.0, "count": 0, "fx_missing": False})
        if row.get("amount_cny") is None:
            item["fx_missing"] = True
        else:
            item["amount"] += abs(row["amount_cny"])
        item["count"] += 1
    total = sum(item["amount"] for item in grouped.values())
    missing_fx = any(item["fx_missing"] for item in grouped.values())
    result = []
    for item in grouped.values():
        amount = round(item["amount"], 2)
        result.append(
            {
                "category": item["category"],
                "amount": None if item["fx_missing"] else amount,
                "count": item["count"],
                "share": None if missing_fx else round(amount / total, 4) if total else 0,
            }
        )
    return sorted(result, key=lambda item: item["amount"] if item["amount"] is not None else -1, reverse=True)


def _load_accounts_config() -> dict[str, Any]:
    path = ACCOUNTS_CONFIG_PATH if ACCOUNTS_CONFIG_PATH.exists() else LOCAL_ACCOUNTS_CONFIG_PATH
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _load_configured_account_codes(account_config: Mapping[str, Any] | None = None) -> set[str]:
    cfg = dict(account_config or _load_accounts_config())
    accounts = cfg.get("accounts") or {}
    return {str(code) for code in accounts.keys() if code}


def _account_capital_config(account_config: Mapping[str, Any] | None, account_code: str) -> dict[str, Any]:
    accounts = (account_config or {}).get("accounts") or {}
    cfg = accounts.get(account_code) or {}
    defaults = (account_config or {}).get("defaults") or {}
    owner_ratio = _to_float(cfg.get("owner_ratio", defaults.get("owner_ratio", 1)))
    personal_capital = cfg.get("personal_capital")
    capital_base = cfg.get("capital_base")
    return {
        "owner_ratio": owner_ratio or 1.0,
        "personal_capital": _to_float(personal_capital) if personal_capital not in (None, "") else None,
        "capital_base": _to_float(capital_base) if capital_base not in (None, "") else None,
    }


def _apply_personal_capital_override(item: dict[str, Any], account_config: Mapping[str, Any] | None) -> None:
    capital = _account_capital_config(account_config, str(item.get("account_code") or ""))
    item["owner_ratio"] = capital["owner_ratio"]
    item["personal_capital"] = capital["personal_capital"]
    item["capital_base"] = capital["capital_base"]
    if item.get("account_kind") != "investment" or capital["personal_capital"] is None:
        return

    # Co-owned investment accounts such as Tonghuashun publish gross platform
    # cash events. The account page must show the user's capital share instead.
    item["investment_inflow"] = capital["personal_capital"]
    item["net"] = (
        item["investment_inflow"]
        - item["investment_outflow"]
        + item["investment_return"]
        - item["investment_fee"]
    )
    item["funding_scope"] = "personal_capital"


def _account_summary(
    rows: list[dict[str, Any]],
    configured_accounts: set[str] | None = None,
    account_config: Mapping[str, Any] | None = None,
    base_currency: str = 'CNY',
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        account_code = str(row.get("account_code") or "unknown")
        if configured_accounts and account_code not in configured_accounts:
            continue
        item = grouped.setdefault(
            account_code,
            {
                "account_code": account_code,
                "account_kind": row.get("account_kind") or "cash",
                "income": 0.0,
                "expense": 0.0,
                "transfer": 0.0,
                "investment_inflow": 0.0,
                "investment_outflow": 0.0,
                "investment_return": 0.0,
                "investment_fee": 0.0,
                "net": 0.0,
                "row_count": 0,
                "pending_count": 0,
                "latest_sync_at": None,
                "fx_missing_count": 0,
                "fx_missing_flows": set(),
                "direction_conflict_count": 0,
                "source_unverified_count": 0,
            },
        )
        if row.get("account_kind") == "investment":
            item["account_kind"] = "investment"
        sync_at = row.get("latest_sync_at") or row.get("imported_at") or row.get("updated_at") or row.get("created_at")
        if sync_at and (item["latest_sync_at"] is None or str(sync_at) > str(item["latest_sync_at"])):
            item["latest_sync_at"] = sync_at
        flow_type = row.get("flow_type")
        item["row_count"] += 1
        if row.get("amount_cny") is None:
            if row.get('source_issue') == 'direction_conflict':
                item["direction_conflict_count"] += 1
            elif str(row.get('source_issue') or '').startswith('source_evidence_'):
                item["source_unverified_count"] += 1
            else:
                item["fx_missing_count"] += 1
            item["fx_missing_flows"].add(str(flow_type or "unknown"))
            item["pending_count"] += 1
            continue
        amount = _to_float(row.get("amount_cny"))
        item["net"] += amount
        if row.get("needs_review"):
            item["pending_count"] += 1
        if flow_type == "income" and amount > 0:
            item["income"] += amount
        elif flow_type == "expense" and amount < 0:
            item["expense"] += abs(amount)
        elif flow_type == "transfer":
            item["transfer"] += abs(amount)
        elif flow_type == "investment_inflow" and amount > 0:
            item["investment_inflow"] += amount
        elif flow_type == "investment_outflow" and amount < 0:
            item["investment_outflow"] += abs(amount)
        elif flow_type == "investment_return":
            item["investment_return"] += abs(amount)
        elif flow_type == "investment_fee":
            item["investment_fee"] += abs(amount)

    result = []
    for item in grouped.values():
        capital = _account_capital_config(account_config, str(item.get('account_code') or ''))
        capital_unavailable = base_currency != 'CNY' and item.get('account_kind') == 'investment' and capital['personal_capital'] is not None
        if capital_unavailable:
            item['owner_ratio']=capital['owner_ratio']
            item['personal_capital']=None
            item['capital_base']=None
            item['funding_scope']='personal_capital'
        else:
            _apply_personal_capital_override(item, account_config)
        activity = (
            item["income"]
            + item["expense"]
            + item["transfer"]
            + item["investment_inflow"]
            + item["investment_outflow"]
            + item["investment_return"]
            + item["investment_fee"]
        )
        result_item = {
                "account_code": item["account_code"],
                "account_kind": item["account_kind"],
                "income": round(item["income"], 2),
                "expense": round(item["expense"], 2),
                "transfer": round(item["transfer"], 2),
                "investment_inflow": round(item["investment_inflow"], 2),
                "investment_outflow": round(item["investment_outflow"], 2),
                "investment_return": round(item["investment_return"], 2),
                "investment_fee": round(item["investment_fee"], 2),
                "net": round(item["net"], 2),
                "row_count": item["row_count"],
                "pending_count": item["pending_count"],
                "activity": round(activity, 2),
                "latest_sync_at": _serialize_date(item["latest_sync_at"]),
                "owner_ratio": item.get("owner_ratio"),
                "personal_capital": (
                    round(item["personal_capital"], 2) if item.get("personal_capital") is not None else None
                ),
                "capital_base": (
                    round(item["capital_base"], 2) if item.get("capital_base") is not None else None
                ),
                "funding_scope": item.get("funding_scope"),
            }
        if item["fx_missing_count"] or item["direction_conflict_count"] or item["source_unverified_count"]:
            for field in ("income", "expense", "transfer", "investment_inflow", "investment_outflow",
                          "investment_return", "investment_fee"):
                if field in item["fx_missing_flows"]:
                    result_item[field] = None
            for field in ("net", "activity"):
                result_item[field] = None
            if item["fx_missing_count"]:
                result_item.update(fx_status="incomplete", fx_missing_count=item["fx_missing_count"])
            if item["direction_conflict_count"]:
                result_item.update(source_status="inconsistent", direction_conflict_count=item["direction_conflict_count"])
            if item["source_unverified_count"]:
                result_item.update(source_status="incomplete", source_unverified_count=item["source_unverified_count"])
        if capital_unavailable:
            result_item.update(investment_inflow=None,net=None,activity=None,capital_status='unavailable')
        result.append(result_item)
    return sorted(result, key=lambda item: item["activity"] if item["activity"] is not None else -1, reverse=True)


def _trend(rows: list[dict[str, Any]], start: date, end: date, grain: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for row in rows:
        d = row.get("transaction_date_obj")
        if not d or d < start or d > end:
            continue
        if grain == "weekly":
            key_date = d - timedelta(days=d.weekday())
            key = key_date.isoformat()
        else:
            key = d.strftime("%Y-%m")
        if row.get("amount_cny") is None and row["flow_type"] in {"income", "expense"}:
            buckets[key]["fx_missing_" + row["flow_type"]] = True
            continue
        if row["flow_type"] == "income" and row["amount_cny"] > 0:
            buckets[key]["income"] += row["amount_cny"]
        elif row["flow_type"] == "expense" and row["amount_cny"] < 0:
            buckets[key]["expense"] += abs(row["amount_cny"])

    result = []
    for key in sorted(buckets):
        income = round(buckets[key]["income"], 2)
        expense = round(buckets[key]["expense"], 2)
        missing_income = buckets[key].get("fx_missing_income")
        missing_expense = buckets[key].get("fx_missing_expense")
        result.append({"period": key, "income": None if missing_income else income,
                       "expense": None if missing_expense else expense,
                       "net": None if missing_income or missing_expense else round(income - expense, 2)})
    return result


def _load_latest_statement_date(cur, verifier=None) -> date | None:
    visibility = _visible_fact_sql(cur, alias='cf', cashflow=True)
    cur.execute(f"""SELECT cf.cashflow_id,cf.event_date,cf.source_table,to_jsonb(cf)->>'source_item_id' AS source_item_id
                    FROM pams_cashflow_fact cf WHERE {visibility} ORDER BY cf.event_date DESC,cf.cashflow_id DESC""")
    rows = [dict(row) for row in cur.fetchall()]
    verifier = verifier or _source_fact_verifier(cur)
    for row in rows:
        if not row.get('source_item_id') and row.get('source_table') not in {'source_items'}:
            return _as_date(row['event_date'])
        if row.get('source_item_id') and verifier.verify(table='pams_cashflow_fact',fact_id=row['cashflow_id']).get('status') == 'verified':
            return _as_date(row['event_date'])
    return None


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table_name}",))
    row = cur.fetchone()
    if isinstance(row, Mapping):
        return bool(row.get("exists"))
    return bool(row and row[0])


def _daily_fx_join(cur, *, currency_sql: str, date_sql: str, alias: str = 'fx') -> str:
    if not _table_exists(cur, 'pams_fx_rates_daily'):
        return f'LEFT JOIN LATERAL (SELECT NULL::numeric AS rate) {alias} ON TRUE'
    return f"""LEFT JOIN LATERAL (
        SELECT CASE WHEN {currency_sql}=%(base)s THEN 1::numeric
          WHEN {currency_sql}='CNY' THEN 1::numeric / NULLIF(base_fx.rate,0)
          WHEN %(base)s='CNY' THEN input_fx.rate
          ELSE input_fx.rate / NULLIF(base_fx.rate,0) END AS rate
        FROM (SELECT 1) seed
        LEFT JOIN pams_fx_rates_daily input_fx ON input_fx.base_currency='CNY'
          AND input_fx.quote_currency={currency_sql} AND input_fx.as_of={date_sql}
          AND input_fx.rate>0 AND input_fx.rate::text NOT IN ('NaN','Infinity','-Infinity') AND NULLIF(BTRIM(input_fx.source),'') IS NOT NULL
        LEFT JOIN pams_fx_rates_daily base_fx ON base_fx.base_currency='CNY'
          AND base_fx.quote_currency=%(base)s AND base_fx.as_of={date_sql}
          AND base_fx.rate>0 AND base_fx.rate::text NOT IN ('NaN','Infinity','-Infinity') AND NULLIF(BTRIM(base_fx.source),'') IS NOT NULL
    ) {alias} ON TRUE"""


def _visible_fact_sql(cur, *, alias: str, cashflow: bool) -> str:
    """Keep unresolved linked facts visible for explicit source verification."""
    active = f"LOWER(COALESCE(to_jsonb({alias})->>'review_status','')) NOT IN ('ignored','rejected','cancelled','canceled','void','duplicate')"
    if not _table_exists(cur, 'source_items'):
        return f"({active})"
    item = f"to_jsonb({alias})->>'source_item_id'"
    return f"({active} AND NOT EXISTS (SELECT 1 FROM source_items i WHERE i.source_item_id::text={item} AND i.review_status='excluded'))"


def _source_fact_verifier(cur):
    from domain.imports.source_fact_evidence import NativeFactVerifier
    native=NativeFactVerifier(cur)
    from domain.brokers.ibkr.ibkr_fact_source_verification import EvidenceCache, verify_ibkr_fact

    class LedgerSourceVerifier:
        def __init__(self):
            self.cache=EvidenceCache()
            cur.execute("""SELECT to_regclass('public.source_files') IS NOT NULL AND
                to_regclass('public.source_batches') IS NOT NULL AND to_regclass('public.source_items') IS NOT NULL AND
                EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.source_items')
                    AND attname='origin_file_id' AND NOT attisdropped) AND
                EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.pams_investment_trade_fact')
                    AND attname='source_item_id' AND NOT attisdropped) AS available""")
            self.available=bool(cur.fetchone()['available'])

        def verify_many(self, *, table, fact_ids):
            if table == 'pams_cashflow_fact':
                return native.verify_many(table=table, fact_ids=fact_ids)
            return {fact_id:self.verify(table=table,fact_id=fact_id) for fact_id in fact_ids}

        def verify(self, *, table, fact_id):
            if table!='pams_investment_trade_fact':
                return native.verify(table=table,fact_id=fact_id)
            cur.execute('SELECT source_table FROM pams_investment_trade_fact WHERE trade_id=%s',(fact_id,))
            row=cur.fetchone()
            if not row or row['source_table']!='ibkr_flex_cash_transaction':
                return native.verify(table=table,fact_id=fact_id)
            if not self.available:
                return {'status':'unknown','reason':'ibkr_source_schema_unavailable',
                        'source_role':'ibkr_report_cash','external_authority_verified':False}
            result=verify_ibkr_fact(cur,trade_id=fact_id,cache=self.cache)
            # Cash ledger needs account, date, currency, direction, native amount
            # and dated conversion. Other absent fields remain explicitly unknown.
            required={'trade_date','account_code','currency','side','gross_amount','net_cash',
                      'source_key','source_id','fx_rate_to_cny','net_cash_cny','fx_evidence','fx_source','cashflow_association'}
            known=result.get('status') in {'consistent','partial'} and not required.intersection(result.get('unknown_fields',[]))
            return {'status':'verified' if known else 'unknown',
                    'reason':'ibkr_report_cash_consistent' if known else result.get('reason','ibkr_cash_evidence_incomplete'),
                    'source_role':'ibkr_report_cash','external_authority_verified':False,
                    'unknown_fields':result.get('unknown_fields',[])}
    return LedgerSourceVerifier()


def _mark_fact_source(cur, rows, *, table: str, verifier=None):
    verifier = verifier or _source_fact_verifier(cur)
    ids = [int(row['fact_id']) for row in rows if row.get('source_item_id')]
    bulk = (verifier.verify_many(table=table,fact_ids=ids)
            if table == 'pams_cashflow_fact' and hasattr(verifier,'verify_many') else {})
    for row in rows:
        if not row.get('source_item_id'):
            if row.get('source_table') in {'source_items','ibkr_flex_cash_transaction'}:
                proof = {'status':'unknown','reason':'source_link_missing'}
            else:
                continue
        else:
            fact_id = int(row['fact_id'])
            proof = bulk.get(fact_id) or verifier.verify(table=table, fact_id=fact_id)
        row['source_evidence_status'] = proof.get('status', 'unknown')
        row['source_evidence_reason'] = proof.get('reason')
        if proof.get('source_role')=='ibkr_report_cash':
            row['source_evidence_role']='ibkr_report_cash'
            row['source_authority_verified']=False
            # Cash report evidence does not establish a separate bank leg.
            row['linked_cashflow_id']=None
        if row['source_evidence_status'] != 'verified':
            row['amount_cny'] = None
            row['source_issue'] = 'source_evidence_' + row['source_evidence_status']
            row['needs_review'] = True
    return rows


def _load_cash_rows(cur, *, start: date, end: date, base_currency: str, verifier=None) -> list[dict[str, Any]]:
    source_rows = source_row_relation(cur, verifier=verifier)
    fx_join = _daily_fx_join(cur, currency_sql='cf.currency', date_sql='cf.event_date')
    visibility = _visible_fact_sql(cur, alias='cf', cashflow=True)
    cur.execute(
        f"""
        SELECT
            cf.cashflow_id AS id,
            cf.cashflow_id AS fact_id,
            to_jsonb(cf)->>'source_item_id' AS source_item_id,
            cf.account_code,
            cf.counter_account_code,
            NULL::text AS account_kind,
            COALESCE(NULLIF(cf.source_channel, ''), NULLIF(cf.source_table, ''), '') AS bank,
            cf.event_date AS transaction_date,
            cf.amount,
            cf.amount * CASE WHEN NULLIF(cf.currency, '') = %(base)s THEN 1 ELSE fx.rate END AS amount_cny,
            cf.amount_cny AS recorded_amount_cny,
            cf.fx_rate_to_cny AS recorded_fx_rate_to_cny,
            CASE WHEN NULLIF(cf.currency, '') = %(base)s THEN 'native_base'
                 WHEN fx.rate IS NULL THEN 'unavailable' ELSE 'daily_market_projection' END AS fx_basis,
            NULLIF(cf.currency, '') AS currency,
            cf.category,
            cf.subcategory,
            cf.summary,
            cf.description,
            cf.counterparty,
            cf.cashflow_type,
            cf.is_internal_transfer,
            cf.transfer_group_id,
            cf.review_status,
            cf.classification_source,
            cf.source_table,
            cf.source_id,
            COALESCE(sr.imported_at, cf.updated_at, cf.created_at) AS imported_at
        FROM pams_cashflow_fact cf
        {fx_join}
        LEFT JOIN {source_rows} sr
          ON sr.source_table = cf.source_table
         AND sr.source_id = cf.source_id
        WHERE cf.event_date >= %(start)s
          AND cf.event_date <= %(end)s
          AND {visibility}
        ORDER BY cf.event_date DESC, cf.cashflow_id DESC
        """,
        {"start": start, "end": end, "base": base_currency},
    )
    return _mark_fact_source(cur, [dict(row) for row in cur.fetchall()], table='pams_cashflow_fact', verifier=verifier)


def _merge_investment_account_rows(
    cash_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer bank cashflow funding rows over broker/importer cash events."""
    cashflow_ids = {
        str(row.get("linked_cashflow_id"))
        for row in cash_rows
        if row.get("linked_cashflow_id") not in (None, "")
    }
    filtered_trade_rows = [
        row
        for row in trade_rows
        if str(row.get("linked_cashflow_id") or "") not in cashflow_ids
    ]
    return sorted(
        cash_rows + filtered_trade_rows,
        key=lambda item: (item.get("transaction_date_obj") or date.min, str(item.get("id") or "")),
        reverse=True,
    )


def _load_investment_trade_account_rows(cur, *, start: date, end: date, base_currency: str, verifier=None) -> list[dict[str, Any]]:
    if not _table_exists(cur, "pams_investment_trade_fact"):
        return []
    fx_join = _daily_fx_join(cur, currency_sql='f.currency', date_sql='f.trade_date')
    visibility = _visible_fact_sql(cur, alias='f', cashflow=False)
    cur.execute(
        f"""
        WITH cash_events AS (
            SELECT
                f.trade_id AS id,
                to_jsonb(f)->>'source_item_id' AS source_item_id,
                f.account_code,
                f.trade_date AS transaction_date,
                LOWER(COALESCE(NULLIF(f.side, ''), f.trade_type)) AS side_norm,
                NULLIF(f.currency, '') AS currency,
                f.net_cash AS native_signed_cash,
                (f.net_cash IS NOT NULL AND (
                  (LOWER(COALESCE(NULLIF(f.side, ''), f.trade_type)) IN ('deposit','cash_deposit','transfer_in') AND f.net_cash < 0)
                  OR (LOWER(COALESCE(NULLIF(f.side, ''), f.trade_type)) IN ('withdraw','withdrawal','cash_withdrawal','transfer_out') AND f.net_cash > 0))) AS direction_conflict,
                ABS(COALESCE(f.net_cash, f.gross_amount, 0)) AS native_amount,
                ABS(COALESCE(f.net_cash, f.gross_amount) * CASE
                    WHEN NULLIF(f.currency, '') = %(base)s THEN 1
                    ELSE fx.rate END) AS amount_cny,
                COALESCE(NULLIF(f.name, ''), NULLIF(f.symbol, ''), '投资现金') AS name,
                COALESCE(NULLIF(f.broker, ''), NULLIF(f.source_table, ''), 'investment') AS source_label,
                f.source_table,
                f.source_id,
                CASE WHEN EXISTS (
                    SELECT 1 FROM pams_cashflow_fact linked
                    WHERE linked.cashflow_id::text = COALESCE(to_jsonb(f)->>'cashflow_id',
                          CASE WHEN f.source_table='pams_cashflow_fact' THEN f.source_id END)
                      AND linked.counter_account_code=f.account_code AND linked.event_date=f.trade_date
                      AND linked.currency=f.currency AND linked.amount=-f.net_cash
                      AND LOWER(COALESCE(to_jsonb(linked)->>'review_status','')) NOT IN ('ignored','rejected')
                ) THEN COALESCE(to_jsonb(f)->>'cashflow_id',
                                CASE WHEN f.source_table='pams_cashflow_fact' THEN f.source_id END)
                  ELSE NULL END AS verified_cashflow_id,
                f.created_at
            FROM pams_investment_trade_fact f
            {fx_join}
            WHERE f.trade_date >= %(start)s
              AND f.trade_date <= %(end)s
              AND {visibility}
              AND f.account_code = ANY(%(accounts)s)
              AND LOWER(COALESCE(NULLIF(f.side, ''), f.trade_type)) IN (
                  'deposit', 'cash_deposit', 'transfer_in',
                  'withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out'
              )
        )
        SELECT
            ('investment-trade-' || id::text) AS id,
            id AS fact_id,
            source_item_id,
            account_code,
            'investment'::text AS account_kind,
            source_label AS bank,
            transaction_date,
            CASE
                WHEN direction_conflict THEN native_signed_cash
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN -native_amount
                ELSE native_amount
            END AS amount,
            CASE
                WHEN direction_conflict THEN NULL
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN -amount_cny
                ELSE amount_cny
            END AS amount_cny,
            currency,
            '投资账户'::text AS category,
            CASE
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN '出金'
                ELSE '入金'
            END AS subcategory,
            CASE
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN '投资出金'
                ELSE '投资入金'
            END AS summary,
            CONCAT_WS(' / ', source_label, name) AS description,
            source_label AS counterparty,
            CASE
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN 'investment_withdrawal'
                ELSE 'investment_funding'
            END AS cashflow_type,
            TRUE AS is_internal_transfer,
            ('investment-trade-' || id::text) AS transfer_group_id,
            verified_cashflow_id AS linked_cashflow_id,
            'auto'::text AS review_status,
            source_table,
            source_id,
            created_at AS imported_at,
            CASE
                WHEN side_norm IN ('withdraw', 'withdrawal', 'cash_withdrawal', 'transfer_out') THEN 'investment_outflow'
                ELSE 'investment_inflow'
            END AS flow_type,
            direction_conflict AS needs_review,
            CASE WHEN direction_conflict THEN 'direction_conflict' ELSE NULL END AS source_issue,
            FALSE AS is_operating
        FROM cash_events
        WHERE native_amount > 0.0001
        ORDER BY transaction_date DESC, id DESC
        """,
        {
            "start": start,
            "end": end,
            "base": base_currency,
            "accounts": sorted(INVESTMENT_ACCOUNT_CODES),
        },
    )
    rows = []
    for raw in cur.fetchall():
        row = dict(raw)
        row["transaction_date_obj"] = _as_date(row.get("transaction_date"))
        row["amount_cny"] = _to_float(row["amount_cny"]) if row.get("amount_cny") is not None else None
        if row["amount_cny"] is None:
            if row.get('source_issue') != 'direction_conflict':
                row["fx_status"] = "unavailable"
            row["needs_review"] = True
        row["coarse_category"] = row.get("subcategory") or "投资现金流"
        rows.append(row)
    return _mark_fact_source(cur, rows, table='pams_investment_trade_fact', verifier=verifier)


def _load_investment_account_rows(cur, *, start: date, end: date, base_currency: str, verifier=None) -> list[dict[str, Any]]:
    source_rows = source_row_relation(cur, verifier=verifier)
    fx_join = _daily_fx_join(cur, currency_sql='cf.currency', date_sql='cf.event_date')
    visibility = _visible_fact_sql(cur, alias='cf', cashflow=True)
    cur.execute(
        f"""
        WITH funding AS (
            SELECT
                cf.cashflow_id AS id,
                to_jsonb(cf)->>'source_item_id' AS source_item_id,
                CASE
                    WHEN cf.counter_account_code = 'direct_investment'
                     AND cf.account_code = 'ccb_7332'
                     AND (
                        COALESCE(cf.summary, '') ILIKE '%%基金%%'
                        OR COALESCE(cf.description, '') ILIKE '%%基金%%'
                     )
                    THEN 'ccb_yhs300_fund'
                    ELSE cf.counter_account_code
                END AS investment_account_code,
                cf.account_code AS funding_account_code,
                cf.event_date AS transaction_date,
                NULLIF(cf.currency, '') AS currency,
                ABS(cf.amount) AS amount,
                ABS(cf.amount * CASE WHEN NULLIF(cf.currency, '') = %(base)s THEN 1 ELSE fx.rate END) AS amount_cny,
                cf.summary,
                cf.description,
                cf.source_table,
                cf.source_id,
                COALESCE(sr.imported_at, cf.updated_at, cf.created_at) AS imported_at
            FROM pams_cashflow_fact cf
            {fx_join}
            LEFT JOIN {source_rows} sr
              ON sr.source_table = cf.source_table
             AND sr.source_id = cf.source_id
            WHERE cf.event_date >= %(start)s
              AND cf.event_date <= %(end)s
              AND {visibility}
              AND (
                cf.cashflow_type = 'investment_funding'
                OR cf.transfer_group_id LIKE 'investment_funding:%%'
              )
              AND cf.amount < 0
        )
        SELECT
            ('investment-inflow-' || id::text) AS id,
            id AS fact_id,
            source_item_id,
            investment_account_code AS account_code,
            'investment'::text AS account_kind,
            ''::text AS bank,
            transaction_date,
            amount,
            amount_cny,
            currency,
            '投资账户'::text AS category,
            '入金'::text AS subcategory,
            COALESCE(NULLIF(summary, ''), '投资入金') AS summary,
            CONCAT_WS(' / ', '来自 ' || funding_account_code, NULLIF(description, '')) AS description,
            funding_account_code AS counterparty,
            'investment_funding'::text AS cashflow_type,
            TRUE AS is_internal_transfer,
            ('investment-funding-' || id::text) AS transfer_group_id,
            id::text AS linked_cashflow_id,
            'auto'::text AS review_status,
            source_table,
            source_id,
            imported_at,
            'investment_inflow'::text AS flow_type,
            FALSE AS needs_review,
            FALSE AS is_operating
        FROM funding
        WHERE investment_account_code = ANY(%(accounts)s)
          AND amount > 0.0001
        ORDER BY transaction_date DESC, id DESC
        """,
        {
            "start": start,
            "end": end,
            "base": base_currency,
            "accounts": sorted(INVESTMENT_ACCOUNT_CODES),
        },
    )
    rows = []
    for raw in cur.fetchall():
        row = dict(raw)
        row["transaction_date_obj"] = _as_date(row.get("transaction_date"))
        row["amount_cny"] = _to_float(row["amount_cny"]) if row.get("amount_cny") is not None else None
        if row["amount_cny"] is None and not row.get('source_issue'):
            row["fx_status"] = "unavailable"
            row["needs_review"] = True
        row["coarse_category"] = row.get("subcategory") or "投资现金流"
        rows.append(row)
    rows = _mark_fact_source(cur, rows, table='pams_cashflow_fact', verifier=verifier)
    trade_rows = _load_investment_trade_account_rows(cur, start=start, end=end, base_currency=base_currency, verifier=verifier)
    return _merge_investment_account_rows(rows, trade_rows)


def _confirmed_transfer_records(cur, *, start: date, end: date):
    """Read immutable decisions and recheck both source-backed facts."""
    from domain.ledger import confirmed_transfers
    result = []
    for item in confirmed_transfers.read(cur, limit=None):
        left, right = item['from_evidence'], item['to_evidence']
        left_day = _as_date(left.get('day'))
        right_day = _as_date(right.get('day'))
        if not any(day and start <= day <= end for day in (left_day, right_day)):
            continue
        kind = ('investment_funding' if right.get('kind') == 'trade' else
                'investment_withdrawal' if left.get('kind') == 'trade' else 'internal_transfer')
        result.append((item, left, right, left_day, right_day, kind))
    return result


def _load_transfer_matches(cur, *, start: date, end: date, base_currency: str) -> list[dict[str, Any]]:
    result = []
    for item, left, right, left_day, right_day, kind in _confirmed_transfer_records(cur, start=start, end=end):
        if item['evidence_status'] != 'confirmed' or left_day != right_day or not left_day:
            continue
        fx_join = _daily_fx_join(cur, currency_sql='matches.currency_norm', date_sql='matches.match_date', alias='fx_hist')
        cur.execute(f"""WITH matches AS (SELECT %(currency)s::text AS currency_norm, %(day)s::date AS match_date)
            SELECT CASE WHEN matches.currency_norm = %(base)s THEN 1 ELSE fx_hist.rate END AS fx_rate
            FROM matches {fx_join}""",
            {'currency': item['currency'], 'day': left_day, 'base': base_currency})
        fx = cur.fetchone()['fx_rate']
        result.append({'id': str(item['transfer_id']), 'relation_code': None, 'match_type': kind,
            'match_date': left_day, 'from_account_code': item['from_account_code'],
            'to_account_code': item['to_account_code'],
            'source_row_id': left['id'] if left['kind'] == 'cashflow' else None,
            'target_row_id': right['id'] if right['kind'] == 'cashflow' else None,
            'amount': item['amount'], 'currency_norm': item['currency'], 'status': 'confirmed',
            'fx_rate': fx, 'amount_cny': item['amount'] * fx if fx is not None else None})
    return result


def _load_pending_transfer_cases(cur, *, start: date, end: date) -> list[dict[str, Any]]:
    """Expose known open bank facts and invalidated decisions without confirming them."""
    from domain.ledger import bank_source_read

    decisions = _confirmed_transfer_records(cur, start=start, end=end)
    pending = []
    linked: set[int] = set()
    for item, left, right, left_day, right_day, kind in decisions:
        if item['evidence_status'] == 'confirmed':
            linked.update(leg['id'] for leg in (left, right) if leg.get('kind') == 'cashflow')
        if item['evidence_status'] == 'revoked' or (item['evidence_status'] == 'confirmed' and left_day == right_day and left_day):
            continue
        pending.append({'case_id': str(item['transfer_id']), 'case_type': kind, 'relation_code': None,
            'from_account_code': item['from_account_code'], 'to_account_code': item['to_account_code'],
            'case_date': _serialize_date(left_day), 'amount': None, 'currency': item['currency'],
            'status': item['evidence_status'] if item['evidence_status'] != 'confirmed' else 'date_unknown'})
    # A bounded scan makes the response usable even for large ledgers.  The
    # count is explicitly the known subset, never a claim of full coverage.
    rows_by_id: dict[int, dict[str, Any]] = {}
    for row in bank_source_read.rows(cur, since=start, limit=10000):
        if (row['event_date'] > end or row['cashflow_id'] in linked or
                row['cashflow_type'] not in {'internal_transfer', 'investment_funding', 'investment_withdrawal'}):
            continue
        previous = rows_by_id.get(row['cashflow_id'])
        if previous is None or row.get('source_evidence_status') == 'verified':
            rows_by_id[row['cashflow_id']] = row
    known = list(rows_by_id.values())
    groups = defaultdict(list)
    for row in known:
        amount = row.get('amount')
        if row.get('source_evidence_status') == 'verified' and amount is not None and amount != 0:
            groups[(row.get('event_date'), row.get('currency'), abs(amount), amount > 0)].append(row)
    candidate_cache: dict[int, list[int]] = {}

    def candidates_for(row):
        ident = row['cashflow_id']
        if ident in candidate_cache:
            return candidate_cache[ident]
        amount = row.get('amount')
        if row.get('source_evidence_status') != 'verified' or amount is None or amount == 0:
            return []
        candidates = []
        for other in groups.get((row.get('event_date'), row.get('currency'), abs(amount), amount < 0), []):
            if (other.get('account_code') == row.get('account_code')
                    or (row.get('counter_account_code') and row['counter_account_code'] != other.get('account_code'))
                    or (other.get('counter_account_code') and other['counter_account_code'] != row.get('account_code'))):
                continue
            candidates.append(other['cashflow_id'])
        candidate_cache[ident] = candidates
        return candidates

    total_count = len(pending) + len(known)
    for row in known[:max(0, 200 - len(pending))]:
        verified = row.get('source_evidence_status') == 'verified'
        amount = row.get('amount')
        day = row.get('event_date')
        currency = row.get('currency')
        candidates = candidates_for(row) if currency and day else []
        status = ('source_unknown' if not verified or amount is None or not currency or not day else
                  'unmatched' if not candidates else
                  'unique_candidate' if len(candidates) == 1 and len(candidates_for(rows_by_id[candidates[0]])) == 1
                  else 'ambiguous')
        pending.append({'case_id': 'cashflow:' + str(row['cashflow_id']), 'case_type': row['cashflow_type'],
            'relation_code': None, 'from_account_code': row['account_code'] if amount is not None and amount < 0 else None,
            'to_account_code': row['account_code'] if amount is not None and amount > 0 else None,
            'case_date': _serialize_date(day), 'amount': _to_float(amount) if verified and amount is not None else None,
            'currency': currency, 'status': status, 'source_evidence_status': row.get('source_evidence_status', 'unknown'),
            'candidate_count': len(candidates) if status != 'source_unknown' else None,
            'candidate_ids': sorted(candidates)[:20]})
    for item in pending:
        item['total_count'] = total_count
    return pending[:200]


def _serialize_row(row: Mapping[str, Any], *, base_currency: str = 'CNY') -> dict[str, Any]:
    keys = [
        "id",
        "account_code",
        "counter_account_code",
        "account_kind",
        "bank",
        "transaction_date",
        "amount",
        "amount_cny",
        "recorded_amount_cny",
        "recorded_fx_rate_to_cny",
        "fx_basis",
        "source_issue",
        "source_evidence_status",
        "source_evidence_reason",
        "source_evidence_role",
        "source_authority_verified",
        "currency",
        "category",
        "subcategory",
        "summary",
        "description",
        "counterparty",
        "flow_type",
        "coarse_category",
        "needs_review",
        "cashflow_type",
        "is_internal_transfer",
        "transfer_group_id",
        "review_status",
        "classification_source",
    ]
    out = {key: row.get(key) for key in keys}
    transaction_date = out.get("transaction_date")
    if isinstance(transaction_date, (date, datetime)):
        out["transaction_date"] = transaction_date.isoformat()[:10]
    out["amount"] = round(_to_float(out.get("amount")), 2)
    out["amount_cny"] = round(_to_float(out["amount_cny"]), 2) if out.get("amount_cny") is not None else None
    out['amount_base'] = out['amount_cny']
    out['base_currency'] = base_currency
    if base_currency != 'CNY':
        out['amount_cny'] = None
    if out.get("recorded_amount_cny") is not None:
        out["recorded_amount_cny"] = round(_to_float(out["recorded_amount_cny"]), 2)
        if out.get("fx_basis") != 'native_base':
            out["recorded_fx_status"] = 'unverified'
    if out.get("recorded_fx_rate_to_cny") is not None:
        out["recorded_fx_rate_to_cny"] = _to_float(out["recorded_fx_rate_to_cny"])
    if row.get("fx_status"):
        out["fx_status"] = row["fx_status"]
    return out


def _transfer_summary(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for match in matches:
        key = (match.get("match_type") or "internal_transfer", match.get("relation_code") or "未命名关系")
        item = grouped.setdefault(
            key,
            {
                "match_type": key[0],
                "relation_code": key[1],
                "amount": 0.0,
                "count": 0,
                "fx_missing_count": 0,
            },
        )
        if match.get("amount_cny") is None:
            item["fx_missing_count"] += 1
        else:
            item["amount"] += abs(_to_float(match["amount_cny"]))
        item["count"] += 1
    return sorted(
        ({**item, "amount": None if item["fx_missing_count"] else round(item["amount"], 2),
          **({"fx_status": "incomplete"} if item["fx_missing_count"] else {})} for item in grouped.values()),
        key=lambda item: item["amount"] if item["amount"] is not None else -1,
        reverse=True,
    )


def build_ledger_workbench_payload(cur, *, month: str | None = None, base_currency: str = "CNY", recent_days: int = 30) -> dict[str, Any]:
    """Build the ledger workbench payload from imported bank/payment rows."""

    verifier = _source_fact_verifier(cur)
    latest_date = _load_latest_statement_date(cur, verifier=verifier)
    month_start, month_end = _month_bounds(month, latest_date)
    ytd_start = date(month_start.year, 1, 1)
    account_config = _load_accounts_config()
    configured_accounts = _load_configured_account_codes(account_config)
    cash_rows = _load_cash_rows(cur, start=ytd_start, end=month_end, base_currency=base_currency, verifier=verifier)
    all_transfer_matches = _load_transfer_matches(cur, start=ytd_start, end=month_end, base_currency=base_currency)
    pending_transfer_cases = _load_pending_transfer_cases(cur, start=ytd_start, end=month_end)
    payment_proxy_source_row_ids = {
        row_id
        for match in all_transfer_matches
        if str(match.get("match_type") or "").lower() == "payment_proxy"
        for row_id in (match.get("source_row_id"),)
        if isinstance(row_id, int)
    }
    transfer_matches = [
        match for match in all_transfer_matches if _is_configured_transfer_match(match, configured_accounts)
    ]
    transfer_row_ids = {
        row_id
        for match in transfer_matches
        for row_id in (match.get("source_row_id"), match.get("target_row_id"))
        if isinstance(row_id, int)
    }

    confirmed_proxy_roles = {}
    if _table_exists(cur, 'evidence_confirmation'):
        from domain.ledger.payment_proxy_confirmation import decisions
        for decision in decisions(cur):
            if decision['evidence_status'] == 'confirmed':
                confirmed_proxy_roles[decision['from_cashflow_id']] = 'source'
                confirmed_proxy_roles[decision['to_cashflow_id']] = 'target'
    enriched_rows: list[dict[str, Any]] = []
    for raw in cash_rows:
        row = dict(raw)
        if row.get("id") in confirmed_proxy_roles:
            row["confirmed_payment_proxy_role"] = confirmed_proxy_roles[row["id"]]
        row["transaction_date_obj"] = _as_date(row.get("transaction_date"))
        row["amount_cny"] = _to_float(row["amount_cny"]) if row.get("amount_cny") is not None else None
        if row["amount_cny"] is None and not row.get('source_issue'):
            row["fx_status"] = "unavailable"
        row.update(classify_ledger_row(row, transfer_row_ids, payment_proxy_source_row_ids))
        if row.get("fx_status") == "unavailable":
            row["needs_review"] = True
        if str(row.get('source_issue') or '').startswith('source_evidence_'):
            row["needs_review"] = True
        enriched_rows.append(row)
    display_rows = [row for row in enriched_rows if row.get("flow_type") != "payment_proxy_dedup"]

    for match in transfer_matches:
        match["match_date_obj"] = _as_date(match.get("match_date"))
        match["amount_cny"] = _to_float(match["amount_cny"]) if match.get("amount_cny") is not None else None

    investment_rows = _load_investment_account_rows(cur, start=ytd_start, end=month_end, base_currency=base_currency, verifier=verifier)
    account_rows = display_rows + investment_rows
    month_rows = [r for r in display_rows if r.get("transaction_date_obj") and month_start <= r["transaction_date_obj"] <= month_end]
    ytd_rows = [r for r in display_rows if r.get("transaction_date_obj") and ytd_start <= r["transaction_date_obj"] <= month_end]
    month_account_rows = [
        r for r in account_rows if r.get("transaction_date_obj") and month_start <= r["transaction_date_obj"] <= month_end
    ]
    ytd_account_rows = [
        row
        for row in account_rows
        if row.get("transaction_date_obj") and ytd_start <= row["transaction_date_obj"] <= month_end
    ]
    recent_start = _recent_start_for_account_window(month_start, month_end, recent_days)
    recent_rows = [
        _serialize_row(r,base_currency=base_currency)
        for r in account_rows
        if r.get("transaction_date_obj") and recent_start <= r["transaction_date_obj"] <= month_end
    ][:300]
    recent_rows_month = [_serialize_row(r,base_currency=base_currency) for r in month_account_rows]
    recent_rows_ytd = [_serialize_row(r,base_currency=base_currency) for r in ytd_account_rows]
    review_rows = [_serialize_row(r,base_currency=base_currency) for r in display_rows if r.get("needs_review")][:100]
    month_transfer_matches = [
        match for match in transfer_matches if match.get("match_date_obj") and month_start <= match["match_date_obj"] <= month_end
    ]
    ytd_transfer_matches = [
        match for match in transfer_matches if match.get("match_date_obj") and ytd_start <= match["match_date_obj"] <= month_end
    ]

    return {
        "as_of": latest_date.isoformat() if latest_date else None,
        "month": month_start.strftime("%Y-%m"),
        "base_currency": base_currency,
        "pending_transfer_count": pending_transfer_cases[0]['total_count'] if pending_transfer_cases else 0,
        "known_pending_transfer_count": pending_transfer_cases[0]['total_count'] if pending_transfer_cases else 0,
        "pending_transfer_coverage": "partial",
        "pending_transfer_count_status": "known_subset",
        "pending_transfer_cases": pending_transfer_cases,
        "periods": {
            "month": summarize_period(display_rows, transfer_matches, month_start, month_end),
            "ytd": summarize_period(display_rows, transfer_matches, ytd_start, month_end),
        },
        "expense_categories": _group_amounts(month_rows, "expense"),
        "income_categories": _group_amounts(month_rows, "income"),
        "expense_categories_month": _group_amounts(month_rows, "expense"),
        "income_categories_month": _group_amounts(month_rows, "income"),
        "expense_categories_ytd": _group_amounts(ytd_rows, "expense"),
        "income_categories_ytd": _group_amounts(ytd_rows, "income"),
        "account_summary": _account_summary(month_account_rows, configured_accounts,base_currency=base_currency),
        "account_summary_ytd": _account_summary(ytd_account_rows, configured_accounts, account_config,base_currency=base_currency),
        "monthly_trend": _trend(display_rows, ytd_start, month_end, "monthly"),
        "weekly_trend": _trend(display_rows, ytd_start, month_end, "weekly"),
        "recent_rows": recent_rows,
        "recent_rows_month": recent_rows_month,
        "recent_rows_ytd": recent_rows_ytd,
        "review_rows": review_rows,
        "transfer_summary": _transfer_summary(ytd_transfer_matches),
        "transfer_summary_month": _transfer_summary(month_transfer_matches),
        "transfer_summary_ytd": _transfer_summary(ytd_transfer_matches),
        "classification_rules": CLASSIFICATION_RULES,
    }
