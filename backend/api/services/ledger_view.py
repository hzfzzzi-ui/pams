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


ACCOUNTS_CONFIG_PATH = Path(os.environ.get("PAMS_ACCOUNTS_CONFIG", "/app/config/accounts.yaml"))
LOCAL_ACCOUNTS_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "accounts.yaml"

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
    ("餐饮", ("餐饮", "吃饭", "饭", "咖啡", "外卖", "美团", "饿了么", "restaurant", "rest ", "uber eats", "pickup coffee", "oxxo")),
    ("交通", ("交通", "打车", "滴滴", "uber ride", "taxi", "地铁", "公交", "铁路", "机票", "航旅")),
    ("居住通信", ("房租", "租金", "物业", "水电", "宽带", "网络", "通信", "移动", "联通", "电信", "mobile reload")),
    ("工具订阅", ("工具软件", "订阅", "软件", "openai", "chatgpt", "claude", "cursor", "icloud", "apple.com/bill")),
    ("购物消费", ("购物", "淘宝", "天猫", "京东", "拼多多", "抖音", "商户消费", "pos", "消费")),
    ("医疗健康", ("医院", "药", "医保", "医疗", "诊所")),
    ("人情往来", ("红包", "转账", "群收款")),
)

EXPENSE_CATEGORY_MAP = {
    "餐饮": "餐饮",
    "交通": "交通",
    "居住": "居住通信",
    "工具软件": "工具订阅",
    "日常消费": "购物消费",
    "医疗": "医疗健康",
}

CLASSIFICATION_RULES = [
    {
        "name": "资金流转优先",
        "description": "已匹配的账户划转、信用卡还款、支付通道映射、投资入金不进入收入或消费统计。",
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
        anchor = latest_date or date.today()
        year, month_no = anchor.year, anchor.month
    end_day = calendar.monthrange(year, month_no)[1]
    return date(year, month_no, 1), date(year, month_no, end_day)


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


def _is_transfer_like(row: Mapping[str, Any], transfer_row_ids: set[int]) -> bool:
    row_id = row.get("id")
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


def classify_ledger_row(row: Mapping[str, Any], transfer_row_ids: set[int] | None = None) -> dict[str, Any]:
    """Classify one imported statement row for the cashflow workbench.

    The classifier intentionally avoids amount thresholds. It first removes
    recognized internal fund movement, then buckets positive rows as income and
    negative rows as coarse consumption categories.
    """

    transfer_row_ids = transfer_row_ids or set()
    amount = _to_float(row.get("amount_cny", row.get("amount")))
    category = str(row.get("category") or "")
    subcategory = str(row.get("subcategory") or "")
    cashflow_type = str(row.get("cashflow_type") or "")
    text = _row_text(row)

    if _is_transfer_like(row, transfer_row_ids):
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
            "flow_type": "transfer",
            "coarse_category": subcategory or "福利资产入账",
            "needs_review": False,
            "is_operating": False,
        }

    if category == "投资收益" and subcategory != "ESOP分红":
        return {
            "flow_type": "investment_return",
            "coarse_category": subcategory or "投资收益",
            "needs_review": False,
            "is_operating": False,
        }

    if amount >= 0:
        if "退款" in text or category == "消费退款":
            label = "退款"
        elif subcategory == "ESOP分红":
            label = "ESOP分红"
        elif subcategory == "年终奖" or "年终" in text:
            label = "年终奖"
        elif "差旅" in text or "报销" in text or subcategory == "差旅报销补助":
            label = "差旅报销补助"
        elif "工资" in text or subcategory == "工资":
            label = "工资"
        elif "利息" in text or subcategory == "利息":
            label = "利息"
        elif "借款归还" in text:
            label = "借款归还"
        elif "红包" in text:
            label = "零散收入/红包"
        elif category == "收入" and subcategory:
            label = subcategory
        else:
            label = "零散收入"
        return {
            "flow_type": "income",
            "coarse_category": label,
            "needs_review": False,
            "is_operating": True,
        }

    keyword_label = _expense_keyword_category(text)
    if keyword_label:
        label = keyword_label
    elif category in EXPENSE_CATEGORY_MAP:
        label = EXPENSE_CATEGORY_MAP[category]
    elif category in {"未分类", ""}:
        label = "待确认支出"
    else:
        label = category or "其他消费"

    return {
        "flow_type": "expense",
        "coarse_category": label,
        "needs_review": label == "待确认支出",
        "is_operating": True,
    }


def summarize_period(rows: list[dict[str, Any]], transfers: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    """Summarize operating cashflow and one-way internal fund movement for a period."""

    period_rows = [r for r in rows if (r.get("transaction_date_obj") and start <= r["transaction_date_obj"] <= end)]
    period_transfers = [t for t in transfers if (t.get("match_date_obj") and start <= t["match_date_obj"] <= end)]
    income = sum(r["amount_cny"] for r in period_rows if r["flow_type"] == "income" and r["amount_cny"] > 0)
    expense = sum(abs(r["amount_cny"]) for r in period_rows if r["flow_type"] == "expense" and r["amount_cny"] < 0)
    transfer = sum(abs(t["amount_cny"]) for t in period_transfers)
    return {
        "income": round(income, 2),
        "expense": round(expense, 2),
        "net": round(income - expense, 2),
        "internal_transfer": round(transfer, 2),
        "pending_count": sum(1 for r in period_rows if r.get("needs_review")),
        "row_count": len(period_rows),
        "income_count": sum(1 for r in period_rows if r["flow_type"] == "income"),
        "expense_count": sum(1 for r in period_rows if r["flow_type"] == "expense"),
    }


def _group_amounts(rows: list[dict[str, Any]], flow_type: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["flow_type"] != flow_type:
            continue
        label = row["coarse_category"]
        item = grouped.setdefault(label, {"category": label, "amount": 0.0, "count": 0})
        item["amount"] += abs(row["amount_cny"])
        item["count"] += 1
    total = sum(item["amount"] for item in grouped.values())
    result = []
    for item in grouped.values():
        amount = round(item["amount"], 2)
        result.append(
            {
                "category": item["category"],
                "amount": amount,
                "count": item["count"],
                "share": round(amount / total, 4) if total else 0,
            }
        )
    return sorted(result, key=lambda item: item["amount"], reverse=True)


def _load_configured_account_codes() -> set[str]:
    path = ACCOUNTS_CONFIG_PATH if ACCOUNTS_CONFIG_PATH.exists() else LOCAL_ACCOUNTS_CONFIG_PATH
    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:
        return set()
    accounts = cfg.get("accounts") or {}
    return {str(code) for code in accounts.keys() if code}


def _account_summary(rows: list[dict[str, Any]], configured_accounts: set[str] | None = None) -> list[dict[str, Any]]:
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
            },
        )
        if row.get("account_kind") == "investment":
            item["account_kind"] = "investment"
        sync_at = row.get("latest_sync_at") or row.get("imported_at") or row.get("updated_at") or row.get("created_at")
        if sync_at and (item["latest_sync_at"] is None or str(sync_at) > str(item["latest_sync_at"])):
            item["latest_sync_at"] = sync_at
        amount = _to_float(row.get("amount_cny"))
        flow_type = row.get("flow_type")
        item["row_count"] += 1
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
        activity = (
            item["income"]
            + item["expense"]
            + item["transfer"]
            + item["investment_inflow"]
            + item["investment_outflow"]
            + item["investment_return"]
            + item["investment_fee"]
        )
        result.append(
            {
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
            }
        )
    return sorted(result, key=lambda item: item["activity"], reverse=True)


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
        if row["flow_type"] == "income" and row["amount_cny"] > 0:
            buckets[key]["income"] += row["amount_cny"]
        elif row["flow_type"] == "expense" and row["amount_cny"] < 0:
            buckets[key]["expense"] += abs(row["amount_cny"])

    result = []
    for key in sorted(buckets):
        income = round(buckets[key]["income"], 2)
        expense = round(buckets[key]["expense"], 2)
        result.append({"period": key, "income": income, "expense": expense, "net": round(income - expense, 2)})
    return result


def _load_latest_statement_date(cur) -> date | None:
    cur.execute("SELECT max(event_date) AS latest_date FROM pams_cashflow_fact")
    row = cur.fetchone()
    return _as_date(row.get("latest_date") if row else None)


def _load_cash_rows(cur, *, start: date, end: date, base_currency: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            cf.cashflow_id AS id,
            cf.account_code,
            NULL::text AS account_kind,
            COALESCE(NULLIF(cf.source_channel, ''), NULLIF(cf.source_table, ''), '') AS bank,
            cf.event_date AS transaction_date,
            cf.amount,
            COALESCE(cf.amount_cny, cf.amount * COALESCE(cf.fx_rate_to_cny, 1), cf.amount) AS amount_cny,
            COALESCE(NULLIF(cf.currency, ''), %(base)s) AS currency,
            cf.category,
            cf.subcategory,
            cf.summary,
            cf.description,
            cf.counterparty,
            cf.cashflow_type,
            cf.is_internal_transfer,
            cf.transfer_group_id,
            cf.review_status,
            cf.source_table,
            cf.source_id,
            COALESCE(sr.imported_at, cf.updated_at, cf.created_at) AS imported_at
        FROM pams_cashflow_fact cf
        LEFT JOIN pams_source_row sr
          ON sr.source_table = cf.source_table
         AND sr.source_id = cf.source_id
        WHERE cf.event_date >= %(start)s
          AND cf.event_date <= %(end)s
        ORDER BY cf.event_date DESC, cf.cashflow_id DESC
        """,
        {"start": start, "end": end, "base": base_currency},
    )
    return [dict(row) for row in cur.fetchall()]


def _load_investment_account_rows(cur, *, start: date, end: date, base_currency: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        WITH funding AS (
            SELECT
                cf.cashflow_id AS id,
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
                COALESCE(NULLIF(cf.currency, ''), %(base)s) AS currency,
                ABS(COALESCE(cf.amount_cny, cf.amount * COALESCE(cf.fx_rate_to_cny, 1), cf.amount)) AS amount_cny,
                cf.summary,
                cf.description,
                cf.source_table,
                cf.source_id,
                COALESCE(sr.imported_at, cf.updated_at, cf.created_at) AS imported_at
            FROM pams_cashflow_fact cf
            LEFT JOIN pams_source_row sr
              ON sr.source_table = cf.source_table
             AND sr.source_id = cf.source_id
            WHERE cf.event_date >= %(start)s
              AND cf.event_date <= %(end)s
              AND cf.cashflow_type = 'investment_funding'
              AND COALESCE(cf.amount_cny, cf.amount * COALESCE(cf.fx_rate_to_cny, 1), cf.amount) < 0
        )
        SELECT
            ('investment-inflow-' || id::text) AS id,
            investment_account_code AS account_code,
            'investment'::text AS account_kind,
            ''::text AS bank,
            transaction_date,
            amount_cny AS amount,
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
            'auto'::text AS review_status,
            source_table,
            source_id,
            imported_at,
            'investment_inflow'::text AS flow_type,
            FALSE AS needs_review,
            FALSE AS is_operating
        FROM funding
        WHERE investment_account_code = ANY(%(accounts)s)
          AND amount_cny > 0.0001
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
        row["amount_cny"] = _to_float(row.get("amount_cny"))
        row["coarse_category"] = row.get("subcategory") or "投资现金流"
        rows.append(row)
    return rows


def _load_transfer_matches(cur, *, start: date, end: date, base_currency: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        WITH matches AS (
            SELECT
                m.case_id AS id,
                m.relation_code,
                m.case_type AS match_type,
                m.case_date AS match_date,
                m.from_account_code,
                m.to_account_code,
                CASE WHEN m.source_ref_id ~ '^[0-9]+$' THEN m.source_ref_id::bigint ELSE NULL END AS source_row_id,
                CASE WHEN m.target_ref_id ~ '^[0-9]+$' THEN m.target_ref_id::bigint ELSE NULL END AS target_row_id,
                m.amount,
                COALESCE(NULLIF(m.currency, ''), %(base)s) AS currency_norm,
                m.status
            FROM pams_reconciliation_case m
            WHERE m.case_date >= %(start)s
              AND m.case_date <= %(end)s
              AND m.case_type = ANY(%(match_types)s)
              AND COALESCE(m.status, 'confirmed') <> 'rejected'
        )
        SELECT
            matches.*,
            CASE
                WHEN matches.currency_norm = %(base)s THEN 1
                ELSE COALESCE(fx_hist.rate, fx_latest.rate, 1)
            END AS fx_rate,
            matches.amount * CASE
                WHEN matches.currency_norm = %(base)s THEN 1
                ELSE COALESCE(fx_hist.rate, fx_latest.rate, 1)
            END AS amount_cny
        FROM matches
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = matches.currency_norm
              AND fx.as_of <= matches.match_date
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_hist ON TRUE
        LEFT JOIN LATERAL (
            SELECT rate
            FROM pams_fx_rates_daily fx
            WHERE fx.base_currency = %(base)s
              AND fx.quote_currency = matches.currency_norm
            ORDER BY fx.as_of DESC
            LIMIT 1
        ) fx_latest ON TRUE
        ORDER BY matches.match_date DESC, matches.id DESC
        """,
        {"start": start, "end": end, "base": base_currency, "match_types": list(TRANSFER_MATCH_TYPES)},
    )
    return [dict(row) for row in cur.fetchall()]


def _serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "account_code",
        "account_kind",
        "bank",
        "transaction_date",
        "amount",
        "amount_cny",
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
    ]
    out = {key: row.get(key) for key in keys}
    if isinstance(out.get("transaction_date"), (date, datetime)):
        out["transaction_date"] = out["transaction_date"].isoformat()[:10]
    out["amount"] = round(_to_float(out.get("amount")), 2)
    out["amount_cny"] = round(_to_float(out.get("amount_cny")), 2)
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
            },
        )
        item["amount"] += abs(_to_float(match.get("amount_cny")))
        item["count"] += 1
    return sorted(
        ({**item, "amount": round(item["amount"], 2)} for item in grouped.values()),
        key=lambda item: item["amount"],
        reverse=True,
    )


def build_ledger_workbench_payload(cur, *, month: str | None = None, base_currency: str = "CNY", recent_days: int = 30) -> dict[str, Any]:
    """Build the ledger workbench payload from imported bank/payment rows."""

    latest_date = _load_latest_statement_date(cur)
    month_start, month_end = _month_bounds(month, latest_date)
    ytd_start = date(month_start.year, 1, 1)
    cash_rows = _load_cash_rows(cur, start=ytd_start, end=month_end, base_currency=base_currency)
    transfer_matches = _load_transfer_matches(cur, start=ytd_start, end=month_end, base_currency=base_currency)
    transfer_row_ids = {
        row_id
        for match in transfer_matches
        for row_id in (match.get("source_row_id"), match.get("target_row_id"))
        if isinstance(row_id, int)
    }

    enriched_rows: list[dict[str, Any]] = []
    for raw in cash_rows:
        row = dict(raw)
        row["transaction_date_obj"] = _as_date(row.get("transaction_date"))
        row["amount_cny"] = _to_float(row.get("amount_cny"))
        row.update(classify_ledger_row(row, transfer_row_ids))
        enriched_rows.append(row)

    for match in transfer_matches:
        match["match_date_obj"] = _as_date(match.get("match_date"))
        match["amount_cny"] = _to_float(match.get("amount_cny"))

    investment_rows = _load_investment_account_rows(cur, start=ytd_start, end=month_end, base_currency=base_currency)
    account_rows = enriched_rows + investment_rows
    month_rows = [r for r in enriched_rows if r.get("transaction_date_obj") and month_start <= r["transaction_date_obj"] <= month_end]
    month_account_rows = [
        r for r in account_rows if r.get("transaction_date_obj") and month_start <= r["transaction_date_obj"] <= month_end
    ]
    ytd_account_rows = [
        row
        for row in account_rows
        if row.get("transaction_date_obj") and ytd_start <= row["transaction_date_obj"] <= month_end
    ]
    configured_accounts = _load_configured_account_codes()
    recent_start = _recent_start_for_account_window(month_start, month_end, recent_days)
    recent_rows = [
        _serialize_row(r)
        for r in account_rows
        if r.get("transaction_date_obj") and recent_start <= r["transaction_date_obj"] <= month_end
    ][:300]
    recent_rows_month = [_serialize_row(r) for r in month_account_rows]
    recent_rows_ytd = [_serialize_row(r) for r in ytd_account_rows]
    review_rows = [_serialize_row(r) for r in enriched_rows if r.get("needs_review")][:100]

    return {
        "as_of": latest_date.isoformat() if latest_date else None,
        "month": month_start.strftime("%Y-%m"),
        "base_currency": base_currency,
        "periods": {
            "month": summarize_period(enriched_rows, transfer_matches, month_start, month_end),
            "ytd": summarize_period(enriched_rows, transfer_matches, ytd_start, month_end),
        },
        "expense_categories": _group_amounts(month_rows, "expense"),
        "income_categories": _group_amounts(month_rows, "income"),
        "account_summary": _account_summary(month_account_rows, configured_accounts),
        "account_summary_ytd": _account_summary(ytd_account_rows, configured_accounts),
        "monthly_trend": _trend(enriched_rows, ytd_start, month_end, "monthly"),
        "weekly_trend": _trend(enriched_rows, ytd_start, month_end, "weekly"),
        "recent_rows": recent_rows,
        "recent_rows_month": recent_rows_month,
        "recent_rows_ytd": recent_rows_ytd,
        "review_rows": review_rows,
        "transfer_summary": _transfer_summary(transfer_matches),
        "classification_rules": CLASSIFICATION_RULES,
    }
