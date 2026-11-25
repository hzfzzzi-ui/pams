"""Project verified ESOP dividends into current holdings."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from domain.investments.trades.trade_read_v2 import load_trade_rows


def enrich_verified_dividends(cur, rows, *, view_as_of):
    positions = [
        row for row in rows
        if row.get("major_category") == "ESOP" and row.get("symbol")
        and row.get("asset_liability") not in {"负债", "liability", "debt"}
        and (row.get("as_of") or row.get("snapshot_as_of"))
    ]
    if not positions:
        return rows
    end = view_as_of or max(row.get("as_of") or row["snapshot_as_of"] for row in positions)
    end = date.fromisoformat(str(end)[:10])
    year_start = date(end.year, 1, 1)
    by_position = defaultdict(list)
    for account in sorted({row.get("account") or row["account_code"] for row in positions}):
        for dividend in load_trade_rows(cur, account_code=account, start=date.min, end=end,
                                        trade_types=("dividend",)):
            if dividend["execution_status"] not in {"cancelled", "canceled", "void"}:
                by_position[account, dividend["symbol"]].append(dividend)

    for row in positions:
        account = row.get("account") or row["account_code"]
        candidates = [
            dividend for dividend in by_position[account, row["symbol"]]
            if not row.get("asset_id") or not dividend["asset_id"] or row["asset_id"] == dividend["asset_id"]
        ]
        if not candidates:
            continue
        verified = [
            dividend for dividend in candidates
            if dividend["source_evidence_status"] == "verified"
            and dividend["execution_status"] in {None, "confirmed"}
            and dividend["currency"] == row.get("currency") == "CNY"
            and dividend["net_cash"] is not None and dividend["net_cash"] > 0
        ]
        lifetime = sum((dividend["net_cash"] for dividend in verified), Decimal(0))
        annual = sum((dividend["net_cash"] for dividend in verified
                      if dividend["trade_date"] >= year_start), Decimal(0))
        row["realized_income_status"] = "confirmed" if len(verified) == len(candidates) else "partial"
        if not verified:
            row["annual_dividend_income_cny"] = None
            continue
        row.update(realized_income=lifetime, realized_income_native=lifetime, realized_income_cny=lifetime,
                   annual_dividend_income_cny=annual)
        for key in ("holding_pnl", "holding_pnl_native", "holding_pnl_cny"):
            if row.get(key) is not None:
                row[key] += lifetime
    return rows


def dividend_yield_on_cost(annual, cost):
    return annual / cost if annual is not None and cost is not None and cost > 0 else None


def summarize_dividends(rows):
    positions = [row for row in rows if not row.get("is_closed")
                 and row.get("asset_liability") not in {"负债", "liability", "debt"}]
    income = [row["annual_dividend_income_cny"] for row in positions
              if row.get("annual_dividend_income_cny") is not None]
    cost = sum((row["cost_cny"] for row in positions if row.get("cost_cny") is not None), Decimal(0))
    annual = sum(income, Decimal(0)) if income else None
    return {"annual_pnl_cny": annual, "annual_basis": "confirmed_dividends",
            "annual_return_rate": dividend_yield_on_cost(annual, cost)}


def reconcile_bank_dividend(cur, *, cashflow_id, event_date, expected_amount, account, symbol, apply=False):
    """Publish one ESOP dividend backed by an explicitly identified bank receipt."""
    from uuid import uuid4

    from domain.imports.sources import source_records_v2 as source
    from domain.ledger.cashflow_read_v2 import load_cash_rows

    if apply:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account-import:" + account,))
        cur.execute("""SELECT 1 FROM pams_cashflow_fact f
            JOIN cashflow_classification c USING (cashflow_id)
            WHERE f.cashflow_id=%s FOR UPDATE OF f, c""", (cashflow_id,))
    cash = load_cash_rows(cur, start=event_date, end=event_date, base_currency="CNY",
                         fact_id=cashflow_id, include_pair_evidence=True)
    if not cash:
        raise ValueError("bank receipt is absent on the specified date")
    receipt = cash[0]
    if (receipt["source_evidence_status"] != "verified" or receipt["classification_status"] != "confirmed"
            or receipt["subcategory"] != "投资分红" or receipt["currency"] != "CNY"
            or receipt["amount"] != expected_amount or expected_amount <= 0
            or receipt["account_code"] == account):
        raise ValueError("bank receipt does not match the confirmed dividend")
    cur.execute("""SELECT asset_id,name FROM pams_holdings_snapshot
        WHERE account_code=%s AND symbol=%s AND major_category='ESOP' AND asset_liability='资产'
          AND currency='CNY' ORDER BY as_of DESC LIMIT 1""", (account, symbol))
    holding = cur.fetchone()
    if not holding:
        raise ValueError("the target ESOP holding is absent")
    source_uid = f"bank-dividend:{receipt['source_item_id']}"
    cur.execute("""SELECT f.trade_id,f.account_code,f.symbol,f.trade_date,f.currency,f.net_cash,i.source_uid
        FROM pams_investment_trade_fact f JOIN source_items i USING (source_item_id)
        WHERE f.trade_type='dividend' AND (i.source_uid=%s OR
            (f.account_code=%s AND f.symbol=%s AND f.trade_date=%s AND f.net_cash=%s
             AND f.currency='CNY'))""", (source_uid, account, symbol, event_date, expected_amount))
    existing = cur.fetchall()
    result = {"cashflow_id": cashflow_id, "account": account, "symbol": symbol,
              "date": event_date.isoformat(), "net_dividend_cny": str(expected_amount)}
    if existing:
        if (len(existing) == 1 and existing[0]["source_uid"] == source_uid
                and all(existing[0][key] == value for key, value in
                        {"account_code": account, "symbol": symbol, "trade_date": event_date,
                         "net_cash": expected_amount, "currency": "CNY"}.items())):
            return dict(result, status="already_linked", trade_id=existing[0]["trade_id"])
        raise ValueError("an existing dividend requires review before linking this receipt")
    if not apply:
        return dict(result, status="ready")

    source_id, item_id = uuid4(), uuid4()
    fields = {"business_date": event_date.isoformat(), "trade_type": "dividend", "currency": "CNY",
              "asset_id": holding["asset_id"], "symbol": symbol, "name": holding["name"],
              "market": "ESOP", "net_cash": str(expected_amount), "execution_status": "confirmed"}
    evidence = receipt["pair_evidence"]
    bank_reference = {"source_table": "pams_cashflow_fact", "cashflow_id": cashflow_id,
                      "source_id": str(evidence["source_id"]), "source_key": str(evidence["source_item_id"]),
                      "source_hash": evidence["source_hash"], "account_code": receipt["account_code"]}
    source.create_source(cur, source_id=source_id, account_code=account, source_name="bank_dividend_reconciliation",
                         acquisition_method="manual", document_type="trade")
    source.add_item(cur, source_id=source_id, item_id=item_id, parse_method="manual",
                    raw_fields={"bank_receipt": bank_reference, "entered_fields": fields},
                    parsed_fields=fields, source_uid=source_uid)
    revision = source.review_item(cur, source_id=source_id, item_id=item_id, expected_revision=2,
                                  parsed_fields=fields, review_status="confirmed",
                                  note="用户确认银行到账归属为 ESOP 税后分红。")
    source.publish_trade_source(cur, source_id=source_id, expected_revision=revision)
    cur.execute("SELECT trade_id FROM pams_investment_trade_fact WHERE source_item_id=%s", (str(item_id),))
    return dict(result, status="linked", trade_id=cur.fetchone()["trade_id"])
