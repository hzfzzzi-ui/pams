"""Reconcile the reference-currency value of both sides of a settled currency conversion."""
from collections import defaultdict
from decimal import Decimal


def monthly_conversion_settlements(rows, matches):
    cash = {row['id']: row for row in rows}
    months = defaultdict(list)
    for match in matches:
        if match['status'] != 'confirmed' or not match['both_cash_legs']:
            continue
        targets = match.get('targets') or [{
            'id': match['target_row_id'], 'account_code': match['to_account_code'],
            'currency': match.get('target_currency', match['currency_norm']),
        }]
        if all(target['currency'] == match['currency_norm'] for target in targets):
            continue
        ids = [match['source_row_id'], *(target['id'] for target in targets)]
        accounts = [match['from_account_code'], *(target['account_code'] for target in targets)]
        legs = [cash.get(key) for key in ids]
        days = [match['source_date'], match['target_date']]
        for leg in legs:
            if leg:
                days.extend((leg['transaction_date'], leg['posting_date'] or leg['transaction_date']))
        settled = max(days)
        month = settled.replace(day=1)
        # A cross-month difference also requires a verified valuation of the remittance in transit.
        amount = None
        if (all(day.replace(day=1) == month for day in days)
                and all(leg and leg['account_code'] == account and leg['source_evidence_status'] == 'verified'
                        and leg['amount_cny'] is not None for leg, account in zip(legs, accounts))):
            amount = sum((leg['amount_cny'] for leg in legs), Decimal(0))
        months[month].append({'day': settled, 'accounts': sorted(set(accounts)), 'amount': amount})
    return months
