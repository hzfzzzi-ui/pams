"""Read-only credit-card amounts for the account center."""

from datetime import date
from decimal import Decimal, InvalidOperation


def confirmed_table_net(cur, account_code, *, currency, today, has_duplicate_alerts):
    """Sum the confirmed table entries, including newer source-only imports."""
    if has_duplicate_alerts:
        return {}
    cur.execute("""SELECT COALESCE(NULLIF(i.parsed_fields->>'posting_date',''),i.parsed_fields->>'business_date') AS as_of,
               i.parsed_fields->>'net_cash' AS amount,i.parsed_fields->>'currency' AS currency
        FROM public.source_records r JOIN public.source_items i USING(source_id)
        WHERE r.account_code=%s AND r.status='published' AND r.document_type='cash'
          AND i.review_status='confirmed' AND i.parsed_fields->>'action' IS DISTINCT FROM 'balance'""", (account_code,))
    posted = []
    for row in cur.fetchall():
        if row['currency'] != currency:
            return {}
        try:
            day, amount = date.fromisoformat(row['as_of']), Decimal(row['amount'])
        except (TypeError, ValueError, InvalidOperation):
            return {}
        if not amount.is_finite():
            return {}
        if day <= today:
            posted.append((day, amount))
    if not posted:
        return {}
    return {'amount': str(sum((amount for _, amount in posted), Decimal(0))),
            'as_of': max(day for day, _ in posted), 'basis': 'credit_card_flow_net'}
