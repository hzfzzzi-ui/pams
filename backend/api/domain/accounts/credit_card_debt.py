"""Read-only credit-card amounts for the account center."""

from collections import defaultdict
from datetime import date
from hashlib import sha256
import json
from decimal import Decimal, InvalidOperation


def uses_table_net_reference(account_code, config):
    """Retain the previously approved basis when a private config predates this capability."""
    basis = config.get('credit_balance_basis')
    if basis is None:
        return account_code == 'ccb_credit_0453'
    return basis == 'posted_flow_net'


def confirmed_table_net(cur, account_code, *, currency, today, has_duplicate_alerts):
    """Sum the confirmed table entries, including newer source-only imports."""
    history = confirmed_table_net_history(cur, account_code, currency=currency, today=today,
                                          has_duplicate_alerts=has_duplicate_alerts)
    return history[-1] if history else {}


def confirmed_table_net_history(cur, account_code, *, currency, today, has_duplicate_alerts):
    """Return end-of-posting-day cumulative amounts under the same approved table-net basis."""
    if has_duplicate_alerts:
        return []
    cur.execute("""SELECT COALESCE(NULLIF(i.parsed_fields->>'posting_date',''),i.parsed_fields->>'business_date') AS as_of,
               i.parsed_fields->>'net_cash' AS amount,i.parsed_fields->>'currency' AS currency
        FROM public.source_records r JOIN public.source_items i USING(source_id)
        WHERE r.account_code=%s AND r.status='published' AND r.document_type='cash'
          AND i.review_status='confirmed' AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'""", (account_code,))
    posted = {}
    for row in cur.fetchall():
        if row['currency'] != currency:
            return []
        try:
            day, amount = date.fromisoformat(row['as_of']), Decimal(row['amount'])
        except (TypeError, ValueError, InvalidOperation):
            return []
        if not amount.is_finite():
            return []
        if day <= today:
            posted[day] = posted.get(day, Decimal(0)) + amount
    total = Decimal(0)
    history = []
    for day, amount in sorted(posted.items()):
        total += amount
        history.append({'amount': str(total), 'as_of': day, 'basis': 'credit_card_flow_net'})
    return history

def verified_table_postings(cur, account, currency, originals):
    """Read one signed movement per reviewed statement-table item, on its posting date."""
    cur.execute("""SELECT i.raw_fields,i.record_hash,i.parsed_fields,r.account_code source_account,
                   r.source_id original_source_id,r.sha256,r.content IS NOT NULL inline_original
            FROM source_records r JOIN source_items i USING(source_id)
            WHERE r.account_code=%s AND r.status='published' AND r.document_type='cash'
            AND i.review_status='confirmed' AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'""", (account,))
    rows = cur.fetchall()
    originals.verify_many(rows)
    totals = defaultdict(Decimal)
    for row in rows:
        fields, raw = row['parsed_fields'], row['raw_fields']
        statement = raw.get('source', {}).get('statement_row') or {}
        try:
            day = date.fromisoformat(fields['posting_date'])
            amount = Decimal(fields['net_cash'])
            digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if (not row['sha256'] or not originals.verify(row) or row['record_hash'] != digest
                    or statement.get('posting_date') != day.isoformat() or statement.get('currency') != currency
                    or fields['currency'] != currency or not amount.is_finite() or Decimal(statement['amount']) != amount):
                return {}
        except (ValueError, TypeError, KeyError, InvalidOperation):
            return {}
        totals[day] += amount
    return dict(totals)
