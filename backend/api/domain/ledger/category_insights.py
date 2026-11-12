"""Read-only category aggregates for the ledger overview."""
from collections import defaultdict
from dataclasses import dataclass


# Older records used the flow direction as the parent category.
_LEGACY_PARENTS = {
    "income": {
        "工资": "工资薪酬", "其他雇主收入": "工资薪酬",
        "投资分红": "投资收益", "替代分红": "投资收益",
        "存款利息": "利息收入", "券商利息": "利息收入",
        "消费退款": "退款返现", "利息返还": "退款返现",
    },
    "expense": {
        "餐饮": "餐饮", "外卖": "餐饮", "咖啡饮品": "餐饮",
        "打车": "交通出行", "交通": "交通出行",
        "旅行住宿": "旅行住宿", "购物日用": "购物", "超市便利": "购物",
        "数码与数字服务": "购物", "物流": "购物", "通信费": "通讯网络",
    },
}
_GENERIC = {None, "", "收入", "消费", "待分类", "未分类"}


def _labels(row, redesigned):
    category, child = row.get("category"), row.get("subcategory")
    if redesigned and row.get("classification_status") != "confirmed":
        return "分类待补", "子类待补"
    if not redesigned:
        category = row["coarse_category"]
    if category in {"内部划转", "支付通道", "融资", "换汇"}:
        category = "资金往来（未配对）"
    elif category in _GENERIC:
        category = _LEGACY_PARENTS[row["flow_type"]].get(child, "分类待补")
    return category, child or "子类待补"


@dataclass
class _Amount:
    total: float = 0.0
    count: int = 0
    missing: bool = False

    def add(self, value):
        self.count += 1
        if value is None:
            self.missing = True
        else:
            self.total += abs(value)

    def value(self):
        return None if self.missing else round(self.total, 2)


def _ranking(buckets):
    total = sum(bucket.total for bucket in buckets.values())
    missing = any(bucket.missing for bucket in buckets.values())
    rows = []
    for category, bucket in buckets.items():
        rows.append({
            "category": category,
            "amount": bucket.value(),
            "count": bucket.count,
            "share": None if missing else round(bucket.total / total, 4) if total else 0,
        })
    return sorted(rows, key=lambda row: (row["amount"] is None, -(row["amount"] or 0), row["category"]))


def category_insights(rows, month_start, *, redesigned):
    """Return two rankings, their children, and one series per parent for the year."""
    periods = [f"{month_start.year}-{month:02d}" for month in range(1, month_start.month + 1)]
    flow_types = ("income", "expense")
    parents = {scope: {flow: defaultdict(_Amount) for flow in flow_types} for scope in ("month", "ytd")}
    children = {scope: defaultdict(lambda: defaultdict(_Amount)) for scope in parents}
    monthly = defaultdict(lambda: defaultdict(_Amount))

    for row in rows:
        flow = row["flow_type"]
        if flow not in flow_types:
            continue
        category, child = _labels(row, redesigned)
        amount = row["amount_cny"]
        day = row["transaction_date_obj"]
        monthly[(flow, category)][day.month].add(amount)
        parents["ytd"][flow][category].add(amount)
        children["ytd"][(flow, category)][child].add(amount)
        if day >= month_start:
            parents["month"][flow][category].add(amount)
            children["month"][(flow, category)][child].add(amount)

    return {
        "periods": periods,
        "monthly": {
            flow: {
                category: [monthly[(flow, category)][month].value()
                           for month in range(1, len(periods) + 1)]
                for category in parents["ytd"][flow]
            } for flow in flow_types
        },
        **{
            scope: {
                flow: [{**row, "children": _ranking(children[scope][(flow, row["category"])])}
                       for row in _ranking(rank)]
                for flow, rank in flows.items()
            }
            for scope, flows in parents.items()
        },
    }
