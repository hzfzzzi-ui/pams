"""Read original-backed statement balances for the account center."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation


def statement_balances(cur, accounts, today, *, originals):
    """Return the latest unambiguous statement balance in each account's currency."""
    return {code: rows[-1] for code, rows in statement_balance_history(
        cur, accounts, today, originals=originals, latest_only=True).items() if rows}


def statement_balance_history(cur, accounts, today, *, originals, latest_only=False, evidence_statements=False, conflicts=None):
    """One signed closing balance per matching statement day; conflicting originals are unusable."""
    if not accounts:
        return {}
    cur.execute("""SELECT r.account_code AS source_account, r.source_id AS original_source_id,
               r.sha256, r.content IS NOT NULL AS inline_original,
               i.parsed_fields->>'business_date' AS as_of,
               i.parsed_fields->>'net_cash' AS amount, i.parsed_fields->>'currency' AS currency
        FROM public.source_records r JOIN public.source_items i USING(source_id)
        WHERE r.account_code=ANY(%s::text[]) AND r.status='published'
          AND (r.document_type='cash' OR (%s AND r.document_type='statement' AND i.review_status='confirmed'))
          AND r.acquisition_method='file' AND r.file_format='pdf' AND r.sha256 IS NOT NULL
          AND i.parse_method='template' AND i.parsed_fields->>'action'='balance'
          AND i.source_position->>'row'='statement_balance'
          AND i.raw_fields#>>'{source,balance_candidate}'='true'
          AND i.raw_fields->'initial_fields'=i.parsed_fields""", (sorted(accounts), evidence_statements))
    by_account = defaultdict(lambda: defaultdict(list))
    for row in cur.fetchall():
        code = row['source_account']
        if row['currency'] != accounts[code][0]:
            continue
        try:
            day, amount = date.fromisoformat(row['as_of']), Decimal(row['amount'])
        except (TypeError, ValueError, InvalidOperation):
            continue
        if day <= today and amount.is_finite():
            by_account[code][day].append((amount, row))

    results = {}
    for code, by_day in by_account.items():
        liability = accounts[code][1]
        days = [max(by_day)] if latest_only else sorted(by_day)
        originals.verify_many(row for day in days for _, row in by_day[day])
        for day in days:
            rows = by_day[day]
            amounts = {amount for amount, _ in rows}
            if len(amounts) != 1 or not all(originals.verify(row) for _, row in rows):
                if conflicts is not None:
                    conflicts.add((code, day))
                continue
            amount = amounts.pop()
            results.setdefault(code, []).append({'amount': str(-amount if liability else amount), 'as_of': day,
                'basis': 'credit_card_statement' if liability else 'bank_statement'})
    return results
