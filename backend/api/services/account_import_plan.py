"""Exact financial write intents shared by preflight and publication."""
from services.cashflow_fact import _cashflow_flags, make_source_hash


def bank_leg(batch, row, fact):
    identity = row["key"] + ":bank"
    typ = "investment_funding" if fact["side"] == "deposit" else "investment_withdrawal"
    return {"event_date": fact["trade_date"], "posting_date": None,
        "account_code": row["fields"]["counter_account_code"], "counter_account_code": batch["account_code"],
        "amount": -fact["net_cash"], "currency": "CNY", "fx_rate_to_cny": 1, "amount_cny": -fact["net_cash"],
        "cashflow_type": typ, "category": "投资", "subcategory": "入金" if fact["side"] == "deposit" else "出金",
        "counterparty": batch["account_code"], "summary": row["fields"].get("note"), "description": "用户核对的账户导入",
        **_cashflow_flags(typ), "transfer_group_id": row["key"],
        "source_table": "pams_account_import_batch", "source_id": identity, "source_uid": identity,
        "source_hash": make_source_hash("pams_account_import_batch", {"source_id": identity}),
        "raw_json": {"batch_id": batch["batch_id"], "row_id": row["row_id"], "source": row["source"], "fields": row["fields"]},
        "classification_source": "user_confirmed_import", "confidence": 1, "review_status": "confirmed"}


def entries(batch, rows):
    result = []
    for row in rows:
        if row["status"] != "ready":
            continue
        def append(table, value, before=None, role="primary"):
            result.append({"row_id": row["row_id"], "role": role, "table": table,
                "operation": "update" if before else "insert", "before": before, "after": value,
                "source": row["source"]})
        if row.get("snapshot"):
            append("pams_holdings_snapshot", row["snapshot"], row["before"])
        elif row.get("cashflow"):
            append("pams_cashflow_fact", row["cashflow"])
        elif row.get("fact"):
            fact = row["fact"]
            if fact["side"] in {"deposit", "withdraw"}:
                append("pams_cashflow_fact", bank_leg(batch, row, fact), role="bank_leg")
            append("pams_investment_trade_fact", fact)
            if fact["side"] in {"deposit", "withdraw"}:
                result[-1]["links"] = [{"field": "cashflow_id", "row_id": row["row_id"], "role": "bank_leg"}]
    return result
