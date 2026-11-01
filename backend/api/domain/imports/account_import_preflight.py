"""Read-only evidence validation. Unknown is never turned into a financial zero."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import re
import json
from zoneinfo import ZoneInfo

from domain.assets.alias_policy import is_identity_alias
from domain.imports import account_import_store as store
from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE
from domain.investments.investment_trade_fact import build_investment_trade_fact
from domain.investments.portfolio_state import _is_cash_like_holding
from domain.imports.account_import_types import asset_type
from domain.imports import account_import_ths as ths
from domain.imports import account_import_statement as statement
from domain.imports.account_import_plan import entries as write_entries
from domain.imports.account_import_review import image_reviewed


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


def ths_display_price_matches(quantity: Decimal, price: Decimal, gross: Decimal) -> bool:
    """Accept a source amount compatible with a displayed three-decimal average.

    This is a bounded compatibility check, not a claim about the broker's
    calculation or permission to replace its reported amounts.
    """
    if quantity <= 0 or price <= 0 or gross < 0 or price % Decimal("0.001"):
        return False
    midpoint = quantity * price
    half_step = quantity * Decimal("0.0005")
    return midpoint - half_step <= gross < midpoint + half_step


def created_asset_first_rows(batch: dict, rows: list[dict], assets: list[dict]) -> set[str]:
    """Identify one source-backed row per asset created by this import batch.

    This records the first eligible row in this batch, not a first lifetime trade.
    """
    created = {asset['asset_id']: asset for asset in assets
               if asset.get('source') == 'ths_import'
               and str((asset.get('raw_json') or {}).get('source_batch_id') or '') == str(batch['batch_id'])}
    first: dict[str, tuple] = {}
    for row in rows:
        fields, source = row.get('fields') or {}, row.get('source') or {}
        asset_id = fields.get('asset_id')
        asset = created.get(asset_id)
        if not asset or row.get('status') == 'excluded' or not ths.native_row(batch, row):
            continue
        evidence = asset.get('raw_json') or {}
        if (not source.get('file_id') or not evidence.get('source_file_sha256')
                or str(source.get('file_id') or '') != str(evidence.get('source_file_id') or '')
                or source.get('file_sha256') != evidence.get('source_file_sha256')):
            continue
        day, position = fields.get('business_date'), source.get('row')
        if not any(record.get('date') == day and record.get('row') == position
                   for record in evidence.get('source_records') or []):
            continue
        if not isinstance(day, str) or not isinstance(position, int):
            continue
        key = (day, fields.get('trade_time') or '', position, str(row['row_id']))
        if asset_id not in first or key < first[asset_id]:
            first[asset_id] = key
    return {key[-1] for key in first.values()}


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
    cur.execute("SELECT asset_id,primary_symbol,canonical_name,asset_type,currency,exchange,source,raw_json FROM pams_asset_master WHERE status='active' AND currency='CNY' ORDER BY asset_id")
    assets = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT asset_id,alias_type,alias_value FROM pams_asset_alias ORDER BY asset_id,alias_value")
    alias_rows = [dict(r) for r in cur.fetchall()]
    aliases_for_fingerprint = [{"asset_id": r["asset_id"], "alias_value": r["alias_value"]} for r in alias_rows]
    aliases = [{"asset_id": r["asset_id"], "alias_value": r["alias_value"]} for r in alias_rows
               if is_identity_alias(r["asset_id"], r["alias_type"], r["alias_value"])]
    cur.execute("SELECT account_code,base_currency,active FROM pams_accounts ORDER BY account_code")
    accounts = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM pams_cashflow_fact WHERE account_code=%s OR counter_account_code=%s ORDER BY cashflow_id LIMIT 10001", (account, account))
    cashflows = [dict(r) for r in cur.fetchall()]
    if len(cashflows) > 10000:
        raise store.ImportProblem("history_limit", "账户资金历史超过预检容量，不能截断后发布")
    # Poll timestamps are metadata; quantities/cost/price and the complete fact set are not.
    version = fingerprint({"holdings": [{k: v for k, v in r.items() if k not in {"updated_at", "created_at"}} for r in holdings],
                           "latest_rows": latest_rows, "members": members, "facts": facts, "assets": assets, "aliases": aliases_for_fingerprint, "accounts": accounts, "cashflows": cashflows})
    return {"holdings": holdings, "facts": facts, "assets": assets, "aliases": aliases,
            "latest_rows": latest_rows, "members": members, "accounts": accounts, "cashflows": cashflows, "version": version}


def source_key(batch: dict, row: dict) -> str:
    fields, source = row["fields"], row.get("source", {})
    if ths.native_row(batch, row) and source.get("identity"):
        return fingerprint([ths.PROFILE, batch["account_code"], batch["kind"], source["identity"]])
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


def _asset(fields: dict, state: dict, errors: list, *, infer_type=False, source=None):
    symbol, identifier = fields.get("symbol"), fields.get("asset_id")
    aliases = {a["asset_id"] for a in state["aliases"] if symbol and a["alias_value"] == symbol}
    matches = [a for a in state["assets"] if (a["asset_id"] == identifier if identifier else
               a["primary_symbol"] == symbol or a["asset_id"] in aliases or not symbol and a["canonical_name"] == fields.get("name"))]
    if len(matches) != 1:
        errors.append(issue("symbol", "asset_unresolved", "标的未唯一匹配，请先核对代码或完成资产底表映射"))
        return None
    asset = matches[0]
    if infer_type and source:
        source_market = {'上海Ａ股': 'SSE', '深圳Ａ股': 'SZSE'}.get(source.get('market'))
        evidence = asset.get('raw_json') or {}
        recorded_market = {'上海Ａ股': 'SSE', '深圳Ａ股': 'SZSE'}.get(evidence.get('source_market'))
        exchange = asset.get('exchange') or recorded_market
        if not exchange:
            qualified = str(asset.get('primary_symbol') or '').upper()
            if qualified.endswith(('.SH', '.SS')):
                exchange = 'SSE'
            elif qualified.endswith('.SZ'):
                exchange = 'SZSE'
        exchange = {'SH': 'SSE', 'SS': 'SSE', '上海Ａ股': 'SSE', '上海证券交易所': 'SSE',
                    'SZ': 'SZSE', '深圳Ａ股': 'SZSE', '深圳证券交易所': 'SZSE'}.get(exchange, exchange)
        if source_market and not exchange:
            errors.append(issue('symbol', 'asset_market_unverified', '资产底表缺少市场证据，请先核对标的身份'))
            return None
        if source_market and exchange and exchange != source_market:
            errors.append(issue('symbol', 'asset_market_mismatch', '原件市场与资产底表不一致，请先核对标的身份'))
            return None
    if infer_type and not fields.get("asset_type") and asset_type(asset["asset_type"]) in {"stock", "fund"}:
        fields["asset_type"] = asset_type(asset["asset_type"])
    if symbol and identifier and asset["primary_symbol"] != symbol and asset["asset_id"] not in aliases:
        errors.append(issue("symbol", "asset_mismatch", "代码与所选标的不一致"))
    if asset_type(asset["asset_type"]) not in {"stock", "fund"} or asset_type(fields.get("asset_type")) != asset_type(asset["asset_type"]):
        errors.append(issue("asset_type", "asset_type_mismatch", "标的类型不一致或不在本轮范围"))
    return asset


def deferred_source_verified(cur, batch: dict, row: dict, cache: dict) -> bool:
    """Verify the frozen parent, original row and unchanged file-backed fields."""
    source = row.get('source') or {}
    origin = source.get('deferred_from') or {}
    parent_id, row_id, file_id = (origin.get(key) for key in ('batch_id', 'row_id', 'file_id'))
    if not all(isinstance(value, str) and value for value in (parent_id, row_id, file_id)):
        return False
    try:
        from uuid import UUID
        parent_id, row_id, file_id = (str(UUID(value)) for value in (parent_id, row_id, file_id))
    except ValueError:
        return False
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s', (parent_id,))
    parent = cur.fetchone()
    if not parent or parent['status'] != 'published' or parent['account_code'] != batch['account_code']:
        return False
    partial = (parent.get('published_result') or {}).get('partial') or {}
    if partial.get('deferred_batch_id') != str(batch['batch_id']):
        return False
    from domain.imports.source_fact_evidence import _reviewed_snapshot
    if not _reviewed_snapshot(cur, parent, parent_id, locking=False, snapshot_cache=cache):
        return False
    cur.execute('SELECT raw_fields,parsed_fields,review_status,file_id FROM source_items WHERE batch_id=%s AND source_item_id=%s',
                (parent_id, row_id))
    original = cur.fetchone()
    if not original or original['review_status'] != 'excluded' or str(original['file_id']) != file_id:
        return False
    original_source = (original['raw_fields'] or {}).get('source') or {}
    if ({key: value for key, value in source.items() if key not in {'file_id', 'deferred_from'}} !=
            {key: value for key, value in original_source.items() if key != 'file_id'}):
        return False
    from domain.imports.account_import_review import DECISIONS
    old_fields = (original['parsed_fields'] or {}).get('fields') or {}
    return all((row.get('fields') or {}).get(key) == old_fields.get(key)
               for key in (set(row.get('fields') or {}) | set(old_fields)) - DECISIONS)


def prepare_row(cur, batch: dict, row: dict, state: dict) -> dict:
    fields = dict(row["fields"])
    errors: list = []
    native = ths.native_row(batch, row)
    if native and row.get('source', {}).get('deferred_from') and not deferred_source_verified(
            cur, batch, row, state.setdefault('_deferred_snapshot_cache', {})):
        errors.append(issue(None, 'deferred_source_unverified', '待关联记录的原始批次或来源证据无法核验，请人工检查'))
    if native:
        source_errors = {"broker_mapping_conflict", "missing_fee_component", "invalid_fee_component", "negative_fee_component",
                         "standalone_tax_components", "unsupported_broker_action", "invalid_value", "scope_conflict", "row_shape"}
        errors.extend(i for i in row.get("issues", []) if i.get("code") in source_errors)
    result = {"row_id": row["row_id"], "fields": fields, "source": row.get("source", {}), "issues": errors,
              "status": "ready", "before": None, "after": fields, "key": source_key(batch, row)}
    if row.get("excluded") or fields.get("duplicate_decision") == "skip":
        result["status"] = "excluded"
        return result
    if fields.get("kind") != batch["kind"]:
        errors.append(issue("kind", "kind_mismatch", "记录类型与批次不一致"))
    bank_cash = (batch['kind'] == 'cash' and batch['account_code'] in state.get('_statement_sources', {})
                 and fields.get('action') in {'income','expense','transfer'})
    currency = fields.get('currency')
    account = next((a for a in state['accounts'] if a['account_code'] == batch['account_code'] and a['active']), None)
    if not account or account.get('base_currency') != currency:
        errors.append(issue('account_code', 'account_inactive', '目标账户已停用或币种不匹配'))
    if currency != 'CNY' and (not bank_cash or not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency)):
        errors.append(issue('currency', 'currency_unverified', '此资料类型暂不支持该币种，不推断换汇'))
    try:
        day = date.fromisoformat(fields.get("business_date") or "")
        if day > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError
    except ValueError:
        errors.append(issue("business_date", "invalid_date", "请填写原始资料中的有效业务日期，不能使用上传时间"))
        day = None
    if row.get("source", {}).get("origin") == "ocr" and not (
            row.get("acknowledged") and image_reviewed(batch, row.get("source", {}).get("file_id"))):
        errors.append(issue(None, "ocr_review_required", "请核对截图中的代码、日期与金额"))
    if fields.get("status") == "pending":
        result["status"] = "pending"
        errors.append(issue("status", "pending_confirmation", "申请中资料仅保存草稿，不生成确认持仓或交易"))
    kind = batch["kind"]
    if kind == "cash" and batch["account_code"] in state.get("_statement_sources", {}) and fields.get("action") != "balance":
        if row.get("source", {}).get("origin") != "ocr":
            errors.extend(i for i in row.get("issues", []) if i.get("severity") == "error")
        return statement.prepare(cur, result, batch, state, decimal, issue, row)
    cash_balance = kind == "cash" and fields.get("action") == "balance" or kind == "holding" and fields.get("asset_type") == "cash"
    if cash_balance and any(p.get("account_kind") == "credit_card" or (p.get("pams_target") or {}).get("is_asset") is False
                            for p in state.get("_statement_sources", {}).get(batch["account_code"], [])):
        errors.append(issue("net_cash", "liability_balance_unsupported", "此余额属于负债账户，不能按普通现金资产快照写入；本轮保留对账证据"))
    cash_event = native and fields.get("asset_type") == "cash" and fields.get("action") in ths.CASH_ACTIONS
    asset = _asset(fields, state, errors, infer_type=native, source=row.get('source')) if kind != "cash" and not cash_balance and not cash_event else None
    if asset:
        fields.update(asset_id=asset["asset_id"], symbol=asset["primary_symbol"], name=asset["canonical_name"])
        profile_actions = ths.TRADE_ACTIONS if native else store.PROFILES[batch["account_code"]]["trade_actions"]
        if asset_type(asset["asset_type"]) not in profile_actions:
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
        if native:
            allowed |= {"deposit", "withdraw", "interest", "tax"}
        if action not in allowed:
            errors.append(issue("action", "invalid_action", "方向不在此资料类型的范围内"))
        if kind == "trade" and fields.get("status") != "confirmed":
            errors.append(issue("status", "confirmation_required", "请核对成交或基金确认状态"))
        if asset and action not in profile_actions.get(asset_type(fields.get("asset_type")), []):
            errors.append(issue("action", "action_type", "方向与账户交易场所不一致：场内买卖和场外申赎不能混用"))
        fees = decimal(fields.get("fee_amount"), "fee_amount", errors) if kind == "trade" else Decimal(0)
        taxes = (decimal(fields.get("tax_amount"), "tax_amount", errors)
                 if kind == "trade" and fields.get("fee_scope") == "separate"
                 else None if kind == "trade" and fields.get("fee_scope") == "total_charges" else Decimal(0))
        if kind == "trade" and fields.get("fee_scope") not in {"separate", "total_charges"}:
            errors.append(issue("fee_scope", "fee_scope_required", "请明确税费合计或分列，不推断缺失税费为零"))
        if kind == "trade" and fields.get("fee_scope") == "total_charges" and fields.get("tax_amount") not in (None, ""):
            errors.append(issue("tax_amount", "double_charge", "税费合计不能再重复列税款"))
        quantity = decimal(fields.get("quantity"), "quantity", errors) if action in {"buy", "sell", "subscribe", "redeem"} else None
        price = decimal(fields.get("price"), "price", errors) if quantity is not None else None
        gross: Decimal | None
        funding = kind == "cash" or native and action in {"deposit", "withdraw"}
        if kind == "cash":
            cash = decimal(fields.get("net_cash"), "net_cash", errors)
            gross = abs(cash) if cash is not None else None
        else:
            gross = decimal(fields.get("gross_amount"), "gross_amount", errors) if action != "fee" else (fees + taxes if taxes is not None else fees) if fees is not None else None
        net = decimal(fields.get("net_cash"), "net_cash", errors, scale=4 if kind == "cash" else 10)
        if any(v is not None and v < 0 for v in (quantity, price, gross, fees, taxes)) or quantity == 0 or price == 0:
            errors.append(issue(None, "invalid_sign", "数量、成交价、发生额与税费需为正数或明确的零费用"))
        if quantity is not None and price is not None and gross is not None and abs(quantity * price - gross) > Decimal("0.01"):
            if not (native and ths_display_price_matches(quantity, price, gross)):
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
        if funding:
            counterpart = next((a for a in state["accounts"] if a["account_code"] == counter and a["active"]), None)
            if not counterpart or counter == batch["account_code"] or counterpart["base_currency"] != "CNY":
                if native and not counter:
                    result["status"] = "pending"
                    errors.append({**issue("counter_account_code", "counter_required", "券商入出金已识别；原报表未证明银行对手账户，保留待关联，不自动补银行腿"), "severity": "warning"})
                else:
                    errors.append(issue("counter_account_code", "counter_required", "资金划转须关联不同的真实对方账户"))
            if counter in store.PROFILES:
                errors.append(issue("counter_account_code", "investment_counter", "投资账户间划转需要双账户资料，本轮不自动补另一投资账户流水"))
            if native and row.get('source', {}).get('deferred_from') and not fields.get('bank_cashflow_id'):
                result['status'] = 'pending'
                errors.append({**issue('bank_cashflow_id', 'bank_confirmation_required',
                    '请导入并核对独立银行流水后选择匹配记录；不会自动生成银行流水'), 'severity': 'warning'})
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
            fact = build_investment_trade_fact({**fields, "source_table": "source_items",
                "source_id": result["key"], "source_key": batch["account_code"] + ":" + result["key"],
                "account_code": batch["account_code"], "broker": batch["account_code"], "trade_date": confirmed or day,
                "trade_time": stamp, "settle_date": confirmed, "side": action, "quantity": quantity, "price": price,
                "gross_amount": gross, "fee_amount": fees, "tax_amount": taxes, "fee_scope": fields.get("fee_scope"), "net_cash": None, "fx_rate_to_cny": 1,
                "raw_json": {"batch_id": batch["batch_id"], "row_id": row["row_id"], "source": row.get("source", {})}})
            if fact["net_cash"] != net:
                errors.append(issue("net_cash", "net_mismatch", "净收付与方向、成交额及税费不闭合"))
            fact["review_status"] = "confirmed"
            fact["raw_json"]["source_row"]["reported_net_cash"] = str(net)
            result["fact"] = fact
    if errors and result["status"] != "pending":
        result["status"] = "blocked"
    if native and result["status"] == "pending" and any(i["severity"] == "error" and i["code"] != "pending_confirmation" for i in errors):
        result["status"] = "blocked"
    return result


def deferred_bank_candidates(cur, batch: dict, row: dict) -> list[dict]:
    """Suggest independently verified same-day bank facts; never auto-confirm."""
    fields, source = row.get('fields') or {}, row.get('source') or {}
    if not source.get('deferred_from') or fields.get('action') not in {'deposit', 'withdraw'}:
        return []
    try:
        day = date.fromisoformat(fields.get('business_date') or '')
        amount = Decimal(fields.get('net_cash') or '')
        if not amount.is_finite() or not amount:
            return []
    except (ValueError, InvalidOperation):
        return []
    cur.execute("""SELECT cashflow_id FROM pams_cashflow_fact
        WHERE event_date=%s AND currency='CNY' AND amount=%s AND account_code<>%s
        ORDER BY cashflow_id LIMIT 101""", (day, -amount, batch['account_code']))
    matches = [record['cashflow_id'] for record in cur.fetchall()]
    if len(matches) > 100:
        return []
    from domain.ledger.confirmed_transfers import _leg
    candidates = []
    for identifier in matches:
        try:
            leg = _leg(cur, 'cashflow', identifier)
        except ValueError:
            continue
        if leg['counter_account_code'] not in (None, batch['account_code']):
            continue
        candidates.append({'cashflow_id': str(identifier), 'account_code': leg['account_code'],
                           'business_date': leg['day'], 'amount': leg['signed_amount'], 'currency': leg['currency']})
    return candidates


def preview(cur, batch: dict) -> dict:
    state = current_state(cur, batch["account_code"])
    if batch.get("unified_source"):
        counterparts = sorted({r.get("fields", {}).get("counter_account_code") for r in batch["rows"]
                               if r.get("fields", {}).get("counter_account_code")})
        if counterparts:
            cur.execute("SELECT * FROM pams_cashflow_fact WHERE account_code=ANY(%s::text[]) ORDER BY cashflow_id LIMIT 10001", (counterparts,))
            additional = [dict(row) for row in cur.fetchall()]
            if len(additional) > 10000:
                raise store.ImportProblem("history_limit", "对方账户资金历史过多，不能截断确认候选")
            combined = {row['cashflow_id']: row for row in [*state['cashflows'], *additional]}
            state['cashflows'] = [combined[key] for key in sorted(combined)]
            state['version'] = fingerprint([state['version'], state['cashflows']])
    state["_statement_config"] = statement.config()
    state["_statement_sources"] = statement.profiles(state["_statement_config"])
    rows = [prepare_row(cur, batch, row, state) for row in batch["rows"]]
    for row in rows:
        if row['status'] != 'excluded' and row.get('source', {}).get('deferred_from') and not any(
                item['code'] == 'deferred_source_unverified' for item in row['issues']):
            row['bank_candidates'] = deferred_bank_candidates(cur, batch, row)
    seen_keys: dict = {}
    seen_economic: list = []
    seen_cashflows: list = []
    for row, original in zip(rows, batch["rows"]):
        if row["status"] != "ready":
            continue
        fact = row.get("fact")
        cashflow = row.get("cashflow")
        if cashflow:
            if any(statement.same_cashflow(old, cashflow, economic=True) for old in seen_cashflows) and not (
                    original.get("acknowledged") and row["fields"].get("duplicate_decision") == "distinct"):
                row["status"] = "blocked"
                row["issues"].append(issue("duplicate_decision", "possible_duplicate", "本批存在同日同额同类账单，请核准或排除"))
            seen_cashflows.append(cashflow)
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
            existing = [f for f in state["facts"] if f.get("source_table") == "source_items" and f.get("source_id") == row["key"]]
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
            if fact["side"] in {"deposit", "withdraw"}:
                candidates = [f for f in state["cashflows"] if str(f["event_date"]) == str(fact["trade_date"]) and
                    f["account_code"] == row["fields"].get("counter_account_code") and f["currency"] == "CNY" and
                    f["amount"] == -fact["net_cash"]]
                if not batch.get("unified_source"):
                    if candidates:
                        row["status"] = "blocked"
                        row["issues"].append(issue("source_ref", "existing_bank_leg", "对方账户已有同日同额现金流水，需要核对关联，不重复新增银行腿"))
                elif candidates:
                    selected = row["fields"].get("bank_cashflow_id")
                    if (len(candidates) != 1 or not original.get('acknowledged') or not str(selected or '').isdigit()
                            or int(selected) != candidates[0]['cashflow_id']):
                        row["status"] = "blocked"
                        row["issues"].append(issue("bank_cashflow_id", "bank_confirmation_required", "同额银行流水须唯一且由用户明确核准关联；歧义保持待处理"))
                    else:
                        from domain.ledger.confirmed_transfers import _leg
                        try:
                            source = _leg(cur, 'cashflow', int(selected))
                            if source['counter_account_code'] not in (None, batch['account_code']):
                                raise ValueError('counter account differs')
                        except ValueError:
                            row["status"] = "blocked"
                            row["issues"].append(issue("bank_cashflow_id", "bank_source_unverified", "银行流水须有独立且已核准的来源，不能使用派生银行腿"))
                        else:
                            fact['cashflow_id'] = int(selected)
                            row['existing_bank_cashflow_id'] = int(selected)
                elif row["fields"].get("bank_cashflow_id"):
                    row["status"] = "blocked"
                    row["issues"].append(issue("bank_cashflow_id", "bank_source_missing", "所选银行流水不存在或内容已变化"))
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
    plan = write_entries(batch, rows)
    token = fingerprint([batch["batch_id"], batch["revision"], batch["rows"], batch["completeness"], state["version"], plan])
    first_created = created_asset_first_rows(batch, rows, state['assets'])
    public_rows = [{**{k: v for k, v in row.items() if k not in {"fact", "snapshot", "key"}},
                    'asset_created_first_row': str(row['row_id']) in first_created} for row in rows]
    unresolved: dict[str, int] = {}
    for row in rows:
        if any(i["code"] == "asset_unresolved" for i in row["issues"]):
            symbol = row["fields"].get("symbol") or row["fields"].get("name") or "未知标的"
            unresolved[symbol] = unresolved.get(symbol, 0) + 1
    warnings = ["持仓、交易、现金独立核对；未上传部分不视为零。"]
    pending_ids = {a['asset_id'] for a in state['assets']
                   if a.get('source') in {'ths_import', 'broker_statement'}
                   and not (a.get('raw_json') or {}).get('import_identity_reviewed')}
    provisional = {r['fields'].get('symbol') for r in rows
                   if r['fields'].get('asset_id') in pending_ids and r['status'] != 'excluded'}
    if provisional:
        warnings.append('以下标的类型及币种使用待核对默认值，请在更多页资产管理补充：' + '、'.join(sorted(provisional)))
    if batch.get("unified_source") and any(row.get("fact") and row["fact"].get("side") in {"deposit", "withdraw"} for row in rows):
        warnings.append("未上传独立银行资料时生成派生银行腿，并标记与券商侧同源；已有真实流水须唯一且人工确认后关联。")
    if unresolved:
        warnings.append("资产底表待补充/核准：" + "、".join(sorted(unresolved)) + "；按标的统一处理，不要求逐笔修改交易。")
    if any(any(i["code"] == "counter_required" for i in row["issues"]) and row["status"] == "pending" for row in rows):
        warnings.append("已识别的券商入出金缺少银行对手账户证据，统一保留待关联，不猜测或补造银行腿。")
    grouped: dict = {}
    for row in rows:
        if row["status"] not in {"blocked", "pending"}:
            continue
        for problem in row["issues"]:
            key = (problem["code"], problem.get("field"))
            group = grouped.setdefault(key, {"code": problem["code"], "message": problem["message"], "row_ids": []})
            if row["row_id"] not in group["row_ids"]:
                group["row_ids"].append(row["row_id"])
    return {"batch_id": batch["batch_id"], "revision": batch["revision"], "preview_token": token,
            "can_publish": can_publish, "counts": counts, "rows": public_rows, "_prepared": rows,
            "write_plan": json_ready(plan), "source_row_count": len(rows), "write_count": len(plan),
            "issue_groups": [{**group, "count": len(group["row_ids"])} for group in grouped.values()],
            "impact": {"nav_change": None, "nav_estimate_status": "unavailable",
                       "message": "仅发布有证据的记录；交易流水不自动补造完整持仓或现金，NAV影响无法完整预估"},
            "warnings": warnings}
