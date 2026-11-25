"""Resolve the cash receipt underlying an already counted investment income fact."""
from collections import Counter


def counted_receipt_ids(cur, rows, counted_trade_sources):
    if not counted_trade_sources:
        return set()
    cash = {row['id']: row for row in rows if row.get('source_evidence_status') == 'verified'}
    cur.execute("""SELECT f.trade_date,f.currency,f.net_cash,i.raw_fields->'bank_receipt' AS receipt
        FROM pams_investment_trade_fact f JOIN source_items i USING(source_item_id)
        WHERE f.source_item_id=ANY(%s::uuid[]) AND f.trade_type IN ('dividend','coupon','interest')
          AND jsonb_typeof(i.raw_fields->'bank_receipt')='object'""", (list(counted_trade_sources),))
    claimed = Counter()
    for trade in cur.fetchall():
        ref = trade['receipt']
        ident = ref.get('cashflow_id')
        row = cash.get(ident) if type(ident) is int else None
        if not row or ref.get('source_table') != 'pams_cashflow_fact':
            continue
        proof = row.get('pair_evidence') or {}
        if (proof.get('source_hash') and proof.get('source_id') and row.get('source_item_id')
                and ref.get('source_hash') == proof['source_hash']
                and str(ref.get('source_key')) == str(row['source_item_id'])
                and str(ref.get('source_id')) == str(proof['source_id'])
                and ref.get('account_code') == row['account_code']
                and trade['trade_date'] == row['transaction_date_obj']
                and trade['currency'] == row['currency'] and trade['net_cash'] == row['amount'] > 0):
            claimed[ident] += 1
    return {ident for ident, count in claimed.items() if count == 1}
