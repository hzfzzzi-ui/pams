"""Statement source adapter. No legacy import, balance sync or global backfill."""
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from domain.ledger.cashflow_fact import build_cashflow_fact, make_source_hash
from domain.ledger.bank_statement_rules import _classify_statement_row


def config():
    primary = Path(os.environ.get("PAMS_BANK_RECONCILE_CONFIG", "/app/config/bank_reconcile.yaml"))
    local = Path(__file__).resolve().parents[4] / "config/bank_reconcile.yaml"
    path = primary if primary.exists() else local
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def profiles(source_config=None):
    result: dict[str, list[dict]] = {}
    source_config = config() if source_config is None else source_config
    for key, value in (source_config.get("profiles") or {}).items():
        account = (value.get("pams_target") or {}).get("account_code") or value.get("account_code")
        if account:
            result.setdefault(account, []).append({**value, "profile_code": key})
    return result


def is_statement(account):
    return account in profiles()


def public_profile(account):
    return {"label": account, "profile": "statement", "default_asset_type": "cash", "kinds": ["cash"],
            "trade_actions": {"cash": ["income", "expense", "transfer", "balance"]}}


def _serial(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def parse(content, filename, account, kind, mapping=None):
    from domain.imports.account_import_parse import parse_document, _csv, _xlsx
    from domain.imports.account_import_ocr import parse_image
    if kind != "cash":
        raise ValueError("账单账户仅接受账单资料")
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        result = parse_image(content, "tonghuashun", "cash")
    else:
        # Standard mapped tables share the bounded parser, including formula checks.
        tables = _xlsx(content) if suffix == ".xlsx" else _csv(content) if suffix in {".csv", ".tsv"} else []
        standard = mapping or any(any("net_cash" in row or "净收付" in row or "现金发生额" in row for row in table[:20]) for _, table in tables)
        if standard:
            result = parse_document(content, filename, "tonghuashun", "cash", mapping)
        else:
            from collectors.bank_statement_parser import parse_statement
            candidates = profiles().get(account, [])
            if len(candidates) != 1 or suffix not in {".csv", ".xlsx", ".pdf"}:
                raise ValueError("未识别到唯一账单来源；请核对账户、文件格式或列映射")
            if suffix == ".pdf" and not content.startswith(b"%PDF-"):
                raise ValueError("PDF文件签名无效")
            if suffix == ".pdf":
                from io import BytesIO
                import pdfplumber
                with pdfplumber.open(BytesIO(content), password=str(candidates[0].get("pdf_password") or "")) as pdf:
                    if len(pdf.pages) > 50:
                        raise ValueError("账单PDF超过50页，请分批上传")
            with TemporaryDirectory(prefix="pams-import-") as directory:
                path = Path(directory) / filename
                path.write_bytes(content)
                statement = parse_statement(path, candidates[0], since=None)
            result = {"rows": [], "headers": [], "warnings": [], "suggested_mapping": {}}
            raw = asdict(statement)
            for index, row in enumerate(raw["rows"], 1):
                fields = {"kind": "cash", "asset_type": "cash", "business_date": str(row["transaction_date"]),
                    "currency": row["currency"], "net_cash": str(row["amount"]),
                    "action": "income" if row["amount"] >= 0 else "expense",
                    "note": " ".join(str(row.get(k) or "") for k in ("summary", "description", "counterparty")).strip(),
                    "source_ref": row.get("source_uid")}
                evidence = _serial(row)
                if isinstance(evidence.get("raw"), dict):
                    evidence["raw"]["source_file"] = filename
                result["rows"].append({"fields": fields, "source": {"row": index, "statement_row": evidence}, "issues": []})
            if statement.statement_balance is not None and statement.statement_end:
                result["rows"].append({"fields": {"kind": "cash", "asset_type": "cash", "action": "balance",
                    "business_date": str(statement.statement_end), "currency": candidates[0].get("currency", "CNY"),
                    "net_cash": str(statement.statement_balance)},
                    "source": {"row": "statement_balance", "balance_candidate": True}, "issues": [], "excluded": True})
            result["warnings"].append({"code": "partial_statement", "severity": "warning",
                "message": "按来源模板返回已识别条目；不据此认定完整历史。期末余额独立核对、默认不写入。"})
    for row in result["rows"]:
        row.setdefault("source", {})["statement"] = True
        row["source"]["trade_actions"] = public_profile(account)["trade_actions"]
        if row["fields"].get("action") == "balance":
            row["source"]["balance_candidate"] = True
            row["excluded"] = True
    return result


def prepare(result, batch, state, decimal, issue):
    fields, errors = result["fields"], result["issues"]
    amount = decimal(fields.get("net_cash"), "net_cash", errors, scale=4)
    action = fields.get("action")
    if action not in {"income", "expense", "transfer"}:
        errors.append(issue("action", "invalid_action", "账单方向须明确为收入、支出或转账"))
    if amount is not None and ((action == "income" and amount < 0) or (action == "expense" and amount > 0)):
        errors.append(issue("net_cash", "cash_sign", "账单方向与原始带符号发生额不一致"))
    if action == "transfer":
        errors.append(issue("counter_account_code", "transfer_evidence_required", "内部转账须核准两侧来源关联，本轮不猜测或补造另一侧"))
    if errors:
        result["status"] = "blocked"
        return result
    source = result["source"].get("statement_row", {})
    original = decimal(source.get("original_amount"), "original_amount", errors, required=False)
    if errors:
        result["status"] = "blocked"
        return result
    if amount == 0 and original not in (None, Decimal(0)):
        result["status"] = "blocked"
        errors.append(issue("net_cash", "neutral_statement_evidence", "中性平台记录须先核准资金映射，不将原始金额补零或视为收入"))
        return result
    raw = {**source, "id": result["key"], "account_code": batch["account_code"],
        "transaction_date": date.fromisoformat(fields["business_date"]), "amount": amount,
        "currency": fields["currency"], "fx_rate_to_cny": Decimal(1), "amount_cny": amount,
        "summary": source.get("summary") if source else fields.get("note")}
    rules = state.get("_statement_config", {})
    raw["category"], raw["subcategory"], _ = _classify_statement_row(raw, rules.get("rules") or [], rules.get("amount_rules") or [])
    if raw["category"] in {"内部划转", "支付通道"}:
        result["status"] = "blocked"
        errors.append(issue("counter_account_code", "transfer_evidence_required", "此条账单需要核准关联资金证据，不自动当作生活收支或补造资金腿"))
        return result
    value = build_cashflow_fact(raw)
    value.update(source_table="pams_account_import_batch", source_id=result["key"], source_uid=result["key"],
        source_hash=make_source_hash("pams_account_import_batch", {"source_id": result["key"]}),
        review_status="confirmed", classification_source="user_confirmed_import")
    value["raw_json"] = {"batch_id": batch["batch_id"], "row_id": result["row_id"], "source": result["source"], "fields": fields}
    for old in state["cashflows"]:
        if old.get("source_table") == value["source_table"] and old.get("source_id") == value["source_id"]:
            result["status"] = "duplicate" if same_cashflow(old, value) else "blocked"
            if result["status"] == "blocked":
                errors.append(issue("source_ref", "source_conflict", "来源已入账但内容不同，不能覆盖"))
            break
        if same_cashflow(old, value, economic=True) and not fields.get("duplicate_decision") == "distinct":
            result["status"] = "blocked"
            errors.append(issue("source_ref", "possible_duplicate", "存在同日同额同类流水，请核对来源或明确排除"))
    result.update(cashflow=value, before=None, after=_serial(value))
    return result


def same_cashflow(left, right, *, economic=False):
    keys = {"account_code", "event_date", "currency", "amount"}
    numbers = {"amount", "fx_rate_to_cny", "amount_cny"}
    if not economic:
        keys |= {"posting_date", "counter_account_code", "fx_rate_to_cny", "amount_cny", "cashflow_type", "category", "subcategory",
                 "affects_spending", "affects_nav", "affects_investment_capital", "is_internal_transfer", "summary", "description", "counterparty"}
    return all((Decimal(str(left[k])) == Decimal(str(right[k])) if k in numbers and left.get(k) is not None and right.get(k) is not None
                else str(left.get(k)) == str(right.get(k))) for k in keys)
