"""Read original-backed statement balances for the account center."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation


def statement_balances(cur, accounts, today, *, originals):
    """Return the latest unambiguous statement balance in each account's currency."""
    if not accounts:
        return {}
    cur.execute("""SELECT r.account_code AS source_account, r.source_id AS original_source_id,
               r.sha256, r.content IS NOT NULL AS inline_original,
               i.parsed_fields->>'business_date' AS as_of,
               i.parsed_fields->>'net_cash' AS amount, i.parsed_fields->>'currency' AS currency
        FROM public.source_records r JOIN public.source_items i USING(source_id)
        WHERE r.account_code=ANY(%s::text[]) AND r.status='published' AND r.document_type='cash'
          AND r.acquisition_method='file' AND r.file_format='pdf' AND r.sha256 IS NOT NULL
          AND i.parse_method='template' AND i.parsed_fields->>'action'='balance'
          AND i.source_position->>'row'='statement_balance'
          AND i.raw_fields#>>'{source,balance_candidate}'='true'
          AND i.raw_fields->'initial_fields'=i.parsed_fields""", (sorted(accounts),))
    by_account = defaultdict(list)
    for row in cur.fetchall():
        code = row['source_account']
        if row['currency'] != accounts[code][0]:
            continue
        try:
            day, amount = date.fromisoformat(row['as_of']), Decimal(row['amount'])
        except (TypeError, ValueError, InvalidOperation):
            continue
        if day <= today and amount.is_finite():
            by_account[code].append((day, amount, row))

    results = {}
    for code, balances in by_account.items():
        latest = max(day for day, _, _ in balances)
        current = [(amount, row) for day, amount, row in balances if day == latest]
        amounts = {amount for amount, _ in current}
        if len(amounts) != 1 or not all(originals.read(row)[1] for _, row in current):
            continue
        amount = amounts.pop()
        liability = accounts[code][1]
        results[code] = {'amount': str(-amount if liability else amount), 'as_of': latest,
                         'basis': 'credit_card_statement' if liability else 'bank_statement'}
    return results
