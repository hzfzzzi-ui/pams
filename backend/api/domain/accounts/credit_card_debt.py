"""Read-only credit-card amounts for the account center."""

from datetime import date
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
