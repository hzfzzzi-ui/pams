"""Exact financial write intents shared by preflight and publication."""
from domain.ledger.cashflow_fact import _cashflow_flags, make_source_hash


def bank_leg(batch, row, fact):
    identity = row["key"] + ":bank"
    typ = "investment_funding" if fact["side"] == "deposit" else "investment_withdrawal"
    value = {"event_date": fact["trade_date"], "posting_date": None,
        "account_code": row["fields"]["counter_account_code"], "counter_account_code": batch["account_code"],
        "amount": -fact["net_cash"], "currency": "CNY", "fx_rate_to_cny": 1, "amount_cny": -fact["net_cash"],
        "cashflow_type": typ, "category": "投资", "subcategory": "入金" if fact["side"] == "deposit" else "出金",
        "counterparty": batch["account_code"], "summary": row["fields"].get("note"), "description": "用户核对的账户导入",
        **_cashflow_flags(typ), "transfer_group_id": row["key"],
        "source_table": "source_items", "source_id": identity, "source_uid": identity,
        "source_hash": make_source_hash("source_items", {"source_id": identity}),
        "raw_json": {"batch_id": batch["batch_id"], "row_id": row["row_id"], "source": row["source"], "fields": row["fields"]},
        "classification_source": "user_confirmed_import", "confidence": 1, "review_status": "confirmed"}
    if batch.get("unified_source"):
        value["raw_json"].update(evidence_role="derived_bank_leg", independent_bank_evidence=False)
    return value


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
        elif row.get("cashflow_confirmation"):
            result.append({"row_id":row["row_id"], "role":"bank_source_confirmation", "table":"pams_cashflow_fact",
                           "operation":"confirm_source", "id":row["cashflow_confirmation"], "source":row["source"]})
        elif row.get("cashflow"):
            append("pams_cashflow_fact", row["cashflow"])
        elif row.get("fact"):
            fact = row["fact"]
            if fact["side"] in {"deposit", "withdraw"} and not row.get("existing_bank_cashflow_id"):
                append("pams_cashflow_fact", bank_leg(batch, row, fact), role="bank_leg")
            append("pams_investment_trade_fact", fact)
            if fact["side"] in {"deposit", "withdraw"} and not row.get("existing_bank_cashflow_id"):
                result[-1]["links"] = [{"field": "cashflow_id", "row_id": row["row_id"], "role": "bank_leg"}]
            elif row.get("existing_bank_cashflow_id"):
                result[-1]["links"] = [{"field": "cashflow_id", "existing_id": row["existing_bank_cashflow_id"], "role": "confirmed_bank_source"}]
    return result
