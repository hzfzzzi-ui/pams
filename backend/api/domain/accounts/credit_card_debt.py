"""Read-only credit-card amounts for the account center."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation


def statement_debts(cur, currencies, today):
    """Use the latest archived statement, including its separate balance candidate."""
    accounts = sorted(currencies)
    if not accounts:
        return {}
    cur.execute("""SELECT r.account_code,i.parsed_fields->>'business_date' AS as_of,
               i.parsed_fields->>'net_cash' AS due,i.parsed_fields->>'currency' AS currency
        FROM public.source_records r JOIN public.source_items i USING(source_id)
        WHERE r.account_code=ANY(%s::text[]) AND r.status='published' AND r.document_type='cash'
          AND r.acquisition_method='file' AND r.file_format='pdf' AND r.sha256 IS NOT NULL
          AND i.parse_method='template' AND i.parsed_fields->>'action'='balance'
          AND i.source_position->>'row'='statement_balance'
          AND i.raw_fields#>>'{source,balance_candidate}'='true'
          AND i.raw_fields->'initial_fields'=i.parsed_fields""", (accounts,))
    by_account = defaultdict(list)
    for row in cur.fetchall():
        code = row['account_code']
        if row['currency'] != currencies[code]:
            continue
        try:
            as_of, due = date.fromisoformat(row['as_of']), Decimal(row['due'])
        except (TypeError, ValueError, InvalidOperation):
            continue
        if as_of <= today and due.is_finite():
            by_account[code].append((as_of, due))

    results = {}
    for code, balances in by_account.items():
        latest = max(as_of for as_of, _ in balances)
        amounts = {due for as_of, due in balances if as_of == latest}
        if len(amounts) == 1:
            results[code] = {'amount': str(Decimal(0) - amounts.pop()),
                             'as_of': latest, 'basis': 'credit_card_statement'}
    return results


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
