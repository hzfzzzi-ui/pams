"""Read-only evidence validation. Unknown is never turned into a financial zero."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from zoneinfo import ZoneInfo

from services import account_import_store as store
from services.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from services.investment_trade_fact import build_investment_trade_fact
from services.portfolio_state import _is_cash_like_holding
from services.account_import_types import asset_type


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def json_ready(value):
    return json.loads(json.dumps(store.norm(value), default=str))


def issue(field, code, message):
    return {"field": field, "code": code, "message": message, "severity": "error"}


def cash_member(row):
    # The portfolio carry-forward helper intentionally includes loans. An import
    # cash balance must not replace those liabilities or a different currency.
    return row.get("currency") == "CNY" and row.get("asset_liability") != "负债" and not any(
        "借款" in str(row.get(k) or "") for k in ("major_category", "minor_category")) and _is_cash_like_holding(row)


def decimal(value, field: str, errors: list, *, required=True, scale=10):
    if value in (None, ""):
        if required:
            errors.append(issue(field, "missing", "请补充" + field))
        return None
    try:
        result = Decimal(value)
        if not result.is_finite() or abs(result) >= Decimal("1e14") or result != result.quantize(Decimal(1).scaleb(-scale)):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        errors.append(issue(field, "invalid_decimal", "数值无效、超出存储精度或范围：" + field))
        return None


def current_state(cur, account: str) -> dict:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    cur.execute(f"""WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT h.* FROM pams_holdings_snapshot h JOIN latest_account a
        ON a.account_code=h.account_code AND a.account_as_of=h.as_of
        WHERE h.account_code=%s ORDER BY h.id""", (today, account))
    holdings = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=(SELECT max(as_of) FROM pams_holdings_snapshot WHERE account_code=%s) ORDER BY id", (account, account))
    latest_rows = [dict(r) for r in cur.fetchall()]
    # Display completeness uses a row-count heuristic. Import safety instead requires
    # explicit last-known member evidence; omission alone never proves a disposal.
    cur.execute("""SELECT DISTINCT ON (COALESCE(NULLIF(symbol,''),name),currency) *
        FROM pams_holdings_snapshot WHERE account_code=%s
        ORDER BY COALESCE(NULLIF(symbol,''),name),currency,as_of DESC,id DESC LIMIT 2001""", (account,))
    members = [dict(r) for r in cur.fetchall()]
    if len(members) > 2000:
        raise store.ImportProblem("membership_limit", "历史成员过多，不能截断完整性检查")
    cur.execute("SELECT * FROM pams_investment_trade_fact WHERE account_code=%s ORDER BY trade_id LIMIT 10001", (account,))
    facts = [dict(r) for r in cur.fetchall()]
    if len(facts) > 10000:
        raise store.ImportProblem("history_limit", "账户历史超过本轮预检容量，不能截断后发布")
    cur.execute("SELECT asset_id,primary_symbol,canonical_name,asset_type,currency FROM pams_asset_master WHERE status='active' AND currency='CNY' ORDER BY asset_id")
    assets = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT asset_id,alias_value FROM pams_asset_alias ORDER BY asset_id,alias_value")
    aliases = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT account_code,base_currency,active FROM pams_accounts ORDER BY account_code")
    accounts = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM pams_cashflow_fact WHERE account_code=%s OR counter_account_code=%s ORDER BY cashflow_id LIMIT 10001", (account, account))
    cashflows = [dict(r) for r in cur.fetchall()]
    if len(cashflows) > 10000:
        raise store.ImportProblem("history_limit", "账户资金历史超过预检容量，不能截断后发布")
    # Poll timestamps are metadata; quantities/cost/price and the complete fact set are not.
    version = fingerprint({"holdings": [{k: v for k, v in r.items() if k not in {"updated_at", "created_at"}} for r in holdings],
                           "latest_rows": latest_rows, "members": members, "facts": facts, "assets": assets, "aliases": aliases, "accounts": accounts, "cashflows": cashflows})
    return {"holdings": holdings, "facts": facts, "assets": assets, "aliases": aliases,
            "latest_rows": latest_rows, "members": members, "accounts": accounts, "cashflows": cashflows, "version": version}


def source_key(batch: dict, row: dict) -> str:
    fields, source = row["fields"], row.get("source", {})
    reference = fields.get("source_ref")
    if reference:
        identity = ["reference", batch["account_code"], batch["kind"], reference]
    elif source.get("file_sha256") and source.get("row") is not None:
        identity = ["file", batch["account_code"], batch["kind"], source["file_sha256"], source.get("sheet"), source["row"]]
    else:
        identity = ["manual", batch["batch_id"], row["row_id"]]
    return fingerprint(identity)


def economic(fact: dict):
    keys = ("account_code", "symbol", "currency", "side", "quantity", "price", "gross_amount", "fee_amount", "tax_amount", "net_cash")
    value = {k: fact.get(k) for k in keys}
    for key in ("quantity", "price", "gross_amount", "fee_amount", "tax_amount", "net_cash"):
        if value[key] is not None:
            value[key] = str(Decimal(str(value[key])).normalize())
    value["trade_date"] = str(fact.get("trade_date"))[:10]
    return value


def same_fact(left, right):
    keys = ("asset_id", "trade_time", "settle_date", "trade_type")
    counter = lambda f: f.get("raw_json", {}).get("source_row", {}).get("counter_account_code")
    return economic(left) == economic(right) and counter(left) == counter(right) and all(str(left.get(k) or "") == str(right.get(k) or "") for k in keys)


def same_snapshot(before, after):
    if not before:
        return False
    numbers = {"quantity", "current_price", "cost", "market_value", "buy_price"}
    return all((Decimal(str(before[k])) == Decimal(str(v)) if k in numbers and before.get(k) is not None and v is not None
                else str(before.get(k)) == str(v)) for k, v in after.items())


def _asset(fields: dict, state: dict, errors: list):
    symbol, identifier = fields.get("symbol"), fields.get("asset_id")
    aliases = {a["asset_id"] for a in state["aliases"] if symbol and a["alias_value"] == symbol}
    matches = [a for a in state["assets"] if (a["asset_id"] == identifier if identifier else
               a["primary_symbol"] == symbol or a["asset_id"] in aliases or not symbol and a["canonical_name"] == fields.get("name"))]
    if len(matches) != 1:
        errors.append(issue("symbol", "asset_unresolved", "标的未唯一匹配，请先核对代码或完成资产底表映射"))
        return None
    asset = matches[0]
    if symbol and identifier and asset["primary_symbol"] != symbol and asset["asset_id"] not in aliases:
        errors.append(issue("symbol", "asset_mismatch", "代码与所选标的不一致"))
    if asset_type(asset["asset_type"]) not in {"stock", "fund"} or asset_type(fields.get("asset_type")) != asset_type(asset["asset_type"]):
        errors.append(issue("asset_type", "asset_type_mismatch", "标的类型不一致或不在本轮范围"))
    return asset


def prepare_row(cur, batch: dict, row: dict, state: dict) -> dict:
    fields = dict(row["fields"])
    errors: list = []
    result = {"row_id": row["row_id"], "fields": fields, "source": row.get("source", {}), "issues": errors,
              "status": "ready", "before": None, "after": fields, "key": source_key(batch, row)}
    if row.get("excluded") or fields.get("duplicate_decision") == "skip":
        result["status"] = "excluded"
        return result
    if fields.get("kind") != batch["kind"]:
        errors.append(issue("kind", "kind_mismatch", "记录类型与批次不一致"))
    if not any(a["account_code"] == batch["account_code"] and a["active"] and a["base_currency"] == "CNY" for a in state["accounts"]):
        errors.append(issue("account_code", "account_inactive", "目标账户已停用或币种不匹配"))
    if fields.get("currency") != "CNY":
        errors.append(issue("currency", "currency_unverified", "本轮仅接收人民币原币资料，不推断换汇"))
    try:
        day = date.fromisoformat(fields.get("business_date") or "")
        if day > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError
    except ValueError:
        errors.append(issue("business_date", "invalid_date", "请填写原始资料中的有效业务日期，不能使用上传时间"))
        day = None
    if row.get("source", {}).get("origin") == "ocr" and not row.get("acknowledged"):
        errors.append(issue(None, "ocr_review_required", "请核对截图中的代码、日期与金额"))
    if fields.get("status") == "pending":
        result["status"] = "pending"
        errors.append(issue("status", "pending_confirmation", "申请中资料仅保存草稿，不生成确认持仓或交易"))
    kind = batch["kind"]
    cash_balance = kind == "cash" and fields.get("action") == "balance" or kind == "holding" and fields.get("asset_type") == "cash"
    asset = _asset(fields, state, errors) if kind != "cash" and not cash_balance else None
    if asset:
        fields.update(asset_id=asset["asset_id"], symbol=asset["primary_symbol"], name=asset["canonical_name"])
        if asset_type(asset["asset_type"]) not in store.PROFILES[batch["account_code"]]["trade_actions"]:
            errors.append(issue("asset_type", "profile_asset_type", "资产类型不属于所选账户支持的资料范围"))
    price: Decimal | None
    if kind == "holding" or kind == "cash" and fields.get("action") == "balance":
        if cash_balance:
            amount = decimal(fields.get("net_cash"), "net_cash", errors, scale=4)
            if amount is not None and amount < 0:
                errors.append(issue("net_cash", "negative_cash_unsupported", "负现金涉及融资负债口径，本轮不自动生成负债快照"))
            fields.update(symbol="CASH_CNY", name="Cash CNY", asset_type="cash")
            quantity, price, cost, value = amount, Decimal(1), amount, amount
        else:
            quantity = decimal(fields.get("quantity"), "quantity", errors, scale=6)
            price = decimal(fields.get("price"), "price", errors, scale=6)
            cost = decimal(fields.get("cost"), "cost", errors, scale=4)
            value = decimal(fields.get("market_value"), "market_value", errors, scale=4)
            if fields.get("cost_basis_type") != "holding_cost":
                errors.append(issue("cost_basis_type", "unverified_cost", "需要核准的持仓成本，不能直接使用摊薄成本或未知报表成本"))
            if any(v is not None and v < 0 for v in (quantity, price, cost, value)):
                errors.append(issue(None, "negative_holding", "非融资持仓数量、价格、市值和成本不能为负"))
            if quantity is not None and quantity == 0 and (cost != 0 or value != 0):
                errors.append(issue("quantity", "zero_position", "零持仓证据的市值与成本必须同时为零"))
            if quantity is not None and price is not None and value is not None and abs(quantity * price - value) > Decimal("0.02"):
                errors.append(issue("market_value", "value_mismatch", "数量乘净值/价格与报表市值不一致，请核对资料日期和精度"))
        matches = [h for h in state["holdings"] if (cash_member(h) if cash_balance else h.get("symbol") == fields.get("symbol") and h.get("currency") == "CNY")]
        if len(matches) > 1:
            errors.append(issue("symbol", "snapshot_ambiguous", "现有快照中有多条同标的记录，不能选择性覆盖"))
        old = matches[0] if len(matches) == 1 else None
        latest = max((h["as_of"] for h in state["latest_rows"]), default=None)
        if day and latest and day < latest:
            errors.append(issue("business_date", "stale_snapshot", "较旧持仓资料不能覆盖更新的账户状态"))
        snapshot = {"account_code": batch["account_code"], "symbol": fields.get("symbol"),
                    "name": old["name"] if old else fields.get("name"), "currency": "CNY", "as_of": day,
                    "asset_id": asset["asset_id"] if asset else None, "quantity": quantity, "current_price": price,
                    "cost": cost, "market_value": value,
                    "buy_price": cost / quantity if cost is not None and quantity else Decimal(0),
                    "major_category": "现金" if cash_balance else "A股",
                    "minor_category": "现金/活期" if cash_balance else "A股基金" if asset_type(fields.get("asset_type")) == "fund" else "A股",
                    "asset_liability": "负债" if cash_balance and value is not None and value < 0 else "资产"}
        # Only the derived average uses schema precision; reported total cost is preserved.
        if snapshot["buy_price"] is not None:
            snapshot["buy_price"] = snapshot["buy_price"].quantize(Decimal("0.000001"))
        result.update(snapshot=snapshot, before=json_ready(old), after=json_ready(snapshot))
    else:
        action = fields.get("action")
        allowed = {"deposit", "withdraw", "transfer"} if kind == "cash" else {"buy", "sell", "subscribe", "redeem", "dividend", "fee"}
        if action not in allowed:
            errors.append(issue("action", "invalid_action", "方向不在此资料类型的范围内"))
        if kind == "trade" and fields.get("status") != "confirmed":
            errors.append(issue("status", "confirmation_required", "请核对成交或基金确认状态"))
        if asset and action not in store.PROFILES[batch["account_code"]]["trade_actions"].get(asset_type(fields.get("asset_type")), []):
            errors.append(issue("action", "action_type", "方向与账户交易场所不一致：场内买卖和场外申赎不能混用"))
        fees = decimal(fields.get("fee_amount"), "fee_amount", errors) if kind == "trade" else Decimal(0)
        taxes = decimal(fields.get("tax_amount"), "tax_amount", errors) if kind == "trade" and fields.get("fee_scope") == "separate" else Decimal(0)
        if kind == "trade" and fields.get("fee_scope") not in {"separate", "total_charges"}:
            errors.append(issue("fee_scope", "fee_scope_required", "请明确税费合计或分列，不推断缺失税费为零"))
        if kind == "trade" and fields.get("fee_scope") == "total_charges" and fields.get("tax_amount") not in (None, ""):
            errors.append(issue("tax_amount", "double_charge", "税费合计不能再重复列税款"))
        quantity = decimal(fields.get("quantity"), "quantity", errors) if action in {"buy", "sell", "subscribe", "redeem"} else None
        price = decimal(fields.get("price"), "price", errors) if quantity is not None else None
        gross: Decimal | None
        if kind == "cash":
            cash = decimal(fields.get("net_cash"), "net_cash", errors)
            gross = abs(cash) if cash is not None else None
        else:
            gross = decimal(fields.get("gross_amount"), "gross_amount", errors) if action != "fee" else fees + taxes if fees is not None and taxes is not None else None
        net = decimal(fields.get("net_cash"), "net_cash", errors, scale=4 if kind == "cash" else 10)
        if any(v is not None and v < 0 for v in (quantity, price, gross, fees, taxes)) or quantity == 0 or price == 0:
            errors.append(issue(None, "invalid_sign", "数量、成交价、发生额与税费需为正数或明确的零费用"))
        if quantity is not None and price is not None and gross is not None and abs(quantity * price - gross) > Decimal("0.01"):
            errors.append(issue("gross_amount", "gross_mismatch", "份额×成交价与成交额不一致，请核对净额/总额口径"))
        confirmed = None
        if action in {"subscribe", "redeem"}:
            try:
                confirmed = date.fromisoformat(fields.get("confirmed_date") or "")
                if not day or confirmed < day or confirmed > datetime.now(ZoneInfo("Asia/Shanghai")).date():
                    raise ValueError
            except ValueError:
                errors.append(issue("confirmed_date", "invalid_confirmation_date", "基金须提供有效确认日期，且不早于申请日期"))
                confirmed = None
        counter = fields.get("counter_account_code")
        if kind == "cash":
            counterpart = next((a for a in state["accounts"] if a["account_code"] == counter and a["active"]), None)
            if not counterpart or counter == batch["account_code"] or counterpart["base_currency"] != "CNY":
                errors.append(issue("counter_account_code", "counter_required", "资金划转须关联不同的真实对方账户"))
            if counter in store.PROFILES:
                errors.append(issue("counter_account_code", "investment_counter", "投资账户间划转需要双账户资料，本轮不自动补另一投资账户流水"))
            if net is not None and (net == 0 or action == "deposit" and net < 0 or action == "withdraw" and net > 0):
                errors.append(issue("net_cash", "cash_sign", "入金为正、出金为负；划转金额不能为零"))
            action = "deposit" if net is not None and net > 0 else "withdraw"
        stamp = None
        if fields.get("trade_time") and day:
            try:
                clock = time.fromisoformat(fields["trade_time"])
                if clock.tzinfo:
                    raise ValueError
                stamp = datetime.combine(confirmed or day, clock)
            except ValueError:
                errors.append(issue("trade_time", "invalid_time", "成交时间须为北京时间，不接受混合时区"))
        if not errors:
            fact = build_investment_trade_fact({**fields, "source_table": "pams_account_import_batch",
                "source_id": result["key"], "source_key": batch["account_code"] + ":" + result["key"],
                "account_code": batch["account_code"], "broker": batch["account_code"], "trade_date": confirmed or day,
                "trade_time": stamp, "settle_date": confirmed, "side": action, "quantity": quantity, "price": price,
                "gross_amount": gross, "fee_amount": fees, "tax_amount": taxes, "net_cash": None, "fx_rate_to_cny": 1,
                "raw_json": {"batch_id": batch["batch_id"], "row_id": row["row_id"], "source": row.get("source", {})}})
            if fact["net_cash"] != net:
                errors.append(issue("net_cash", "net_mismatch", "净收付与方向、成交额及税费不闭合"))
            fact["review_status"] = "confirmed"
            fact["raw_json"]["source_row"]["reported_net_cash"] = str(net)
            result["fact"] = fact
    if errors and result["status"] != "pending":
        result["status"] = "blocked"
    return result


def preview(cur, batch: dict) -> dict:
    state = current_state(cur, batch["account_code"])
    rows = [prepare_row(cur, batch, row, state) for row in batch["rows"]]
    seen_keys: dict = {}
    seen_economic: list = []
    for row, original in zip(rows, batch["rows"]):
        if row["status"] != "ready":
            continue
        fact = row.get("fact")
        if row.get("snapshot") and same_snapshot(row["before"], row["after"]):
            row["status"] = "duplicate"
            continue
        same_key = seen_keys.get(row["key"])
        if same_key:
            row["status"] = "duplicate" if same_fact(fact or {}, same_key.get("fact") or {}) and row["after"] == same_key["after"] else "blocked"
            if row["status"] == "blocked":
                row["issues"].append(issue("source_ref", "source_conflict", "同一来源编号对应不同内容"))
            continue
        seen_keys[row["key"]] = row
        if fact:
            existing = [f for f in state["facts"] if f.get("source_table") == "pams_account_import_batch" and f.get("source_id") == row["key"]]
            if existing:
                row["status"] = "duplicate" if len(existing) == 1 and same_fact(existing[0], fact) else "blocked"
                if row["status"] == "blocked":
                    row["issues"].append(issue("source_ref", "source_conflict", "来源编号已入账但内容不同，不能覆盖"))
                continue
            collisions = [f for f in [*state["facts"], *seen_economic] if economic(f) == economic(fact)]
            reference = row["fields"].get("source_ref")
            ambiguous = [f for f in collisions if not (reference and f.get("raw_json", {}).get("source_row", {}).get("source_ref") and reference != f["raw_json"]["source_row"]["source_ref"])]
            if ambiguous and not (original.get("acknowledged") and row["fields"].get("duplicate_decision") == "distinct"):
                row["status"] = "blocked"
                row["issues"].append(issue("duplicate_decision", "possible_duplicate", "存在相同日期/代码/金额记录，请核对为不同交易或跳过"))
            seen_economic.append(fact)
            if batch["kind"] == "cash" and any(str(f["event_date"]) == str(fact["trade_date"]) and
                    f["account_code"] == row["fields"].get("counter_account_code") and f["currency"] == "CNY" and
                    f["amount"] == -fact["net_cash"] for f in state["cashflows"]):
                row["status"] = "blocked"
                row["issues"].append(issue("source_ref", "existing_bank_leg", "对方账户已有同日同额现金流水，需要核对关联，不重复新增银行腿"))
    snapshots = [r for r in rows if r.get("snapshot") and r["status"] in {"ready", "duplicate"}]
    targets: dict = {}
    for row in snapshots:
        target = (row["snapshot"]["name"], row["snapshot"]["as_of"])
        if target in targets:
            for item in (targets[target], row):
                item["status"] = "blocked"
                item["issues"].append(issue("symbol", "duplicate_snapshot_target", "同一批次不能重复覆盖同一快照成员"))
        targets[target] = row
    dates = {r["snapshot"]["as_of"] for r in snapshots}
    if len(dates) > 1:
        for row in snapshots:
            row["issues"].append(issue("business_date", "mixed_snapshot_dates", "同一持仓批次必须使用同一个资料日期"))
            row["status"] = "blocked"
    def member_key(h):
        return "CASH_CNY" if cash_member(h) else h.get("symbol") or h["name"]
    required_members = [h for h in state["members"] if not (h.get("quantity") == 0 and h.get("cost") == 0 and h.get("market_value") == 0)]
    current_keys = {member_key(h) for h in required_members}
    uploaded_keys = {r["snapshot"].get("symbol") or r["snapshot"]["name"] for r in snapshots}
    latest = max((h["as_of"] for h in state["holdings"]), default=None)
    new_date = dates and (latest is None or max(dates) > latest)
    if snapshots and any(h.get("currency") != "CNY" or h.get("asset_liability") == "负债" for h in required_members):
        for row in snapshots:
            row["issues"].append(issue("currency", "foreign_member_unsupported", "账户有外币或融资负债成员，不能用人民币普通快照替代"))
            row["status"] = "blocked"
    if snapshots and ((new_date and current_keys - uploaded_keys) or batch["completeness"] == "complete" and current_keys - uploaded_keys):
        for row in snapshots:
            row["issues"].append(issue(None, "incomplete_snapshot", "新日期/完整快照缺少既有成员；请补全资料或提供明确零持仓证据，不自动清仓"))
            row["status"] = "blocked"
    counts = {name: sum(r["status"] == status for r in rows) for name, status in
              (("new", "ready"), ("duplicates", "duplicate"), ("blocked", "blocked"), ("pending", "pending"), ("excluded", "excluded"))}
    can_publish = bool(rows) and counts["blocked"] == counts["pending"] == 0 and counts["new"] + counts["duplicates"] > 0
    token = fingerprint([batch["batch_id"], batch["revision"], batch["rows"], batch["completeness"], state["version"]])
    public_rows = [{k: v for k, v in row.items() if k not in {"fact", "snapshot", "key"}} for row in rows]
    return {"batch_id": batch["batch_id"], "revision": batch["revision"], "preview_token": token,
            "can_publish": can_publish, "counts": counts, "rows": public_rows, "_prepared": rows,
            "impact": {"nav_change": None, "nav_estimate_status": "unavailable",
                       "message": "仅发布有证据的记录；交易流水不自动补造完整持仓或现金，NAV影响无法完整预估"},
            "warnings": ["持仓、交易、现金独立核对；未上传部分不视为零。"]}
