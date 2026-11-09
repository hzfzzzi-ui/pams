"""Suggest pairs between independent, source-verified cash facts."""
from collections import Counter, defaultdict
from datetime import date
import re

from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.ledger.confirmed_transfers_v2 import is_independent_equal_pair, occupied_cashflow_ids


TRANSFER_HINTS = {'internal_transfer', 'investment_funding', 'investment_withdrawal', 'debt_repayment'}
TRANSFER_TEXT = re.compile(r'信用卡.{0,10}还款|银转证|证转银|银证转账|内部划转|同行转账|'
                           r'electronic fund transfers?|transferencia a interactive brokers', re.IGNORECASE)
INVESTMENT_TYPES = {'brokerage', 'fund_platform'}
MAX_DAYS = 3


def cash_pair_type(from_account, to_account, profiles):
    source = profiles.get(from_account, {}).get('account_type')
    target = profiles.get(to_account, {}).get('account_type')
    if target == 'liability' and source != 'liability':
        return 'credit_card_payment'
    if target in INVESTMENT_TYPES and source not in INVESTMENT_TYPES:
        return 'investment_funding'
    if source in INVESTMENT_TYPES and target not in INVESTMENT_TYPES:
        return 'investment_withdrawal'
    return 'internal_transfer'


def has_transfer_hint(row):
    return ((row.get('classification_status') == 'confirmed' and row.get('cashflow_type') in TRANSFER_HINTS)
            or bool(TRANSFER_TEXT.search(' '.join(row.get(key) or '' for key in ('summary', 'description')))))


def suggest(rows, profiles, occupied, *, limit=None):
    available = [row for row in rows if row['id'] not in occupied and row['source_evidence_status'] == 'verified'
                 and row['amount'] and row['currency'] and row['transaction_date']]
    deposits = defaultdict(list)
    for row in available:
        if row['amount'] > 0:
            deposits[row['currency'], row['amount']].append(row)

    matches = []
    for source in available:
        if source['amount'] >= 0:
            continue
        for target in deposits[source['currency'], -source['amount']]:
            delta = abs((source['transaction_date'] - target['transaction_date']).days)
            if delta > MAX_DAYS or not is_independent_equal_pair(source['pair_evidence'], target['pair_evidence']):
                continue
            hints = int(has_transfer_hint(source)) + int(has_transfer_hint(target))
            if hints:
                matches.append((source, target, delta, hints))

    usage = Counter(row['id'] for source, target, _, _ in matches for row in (source, target))
    matches.sort(key=lambda match: (any(usage[row['id']] > 1 for row in match[:2]), -match[3], match[2],
                                    -match[0]['transaction_date'].toordinal(), match[0]['id'], match[1]['id']))
    result = []
    for source, target, delta, hints in matches[:limit]:
        ambiguous = usage[source['id']] > 1 or usage[target['id']] > 1
        result.append({
            'match_key': f"candidate:{source['id']}:{target['id']}",
            'match_type': cash_pair_type(source['account_code'], target['account_code'], profiles),
            'from_account_code': source['account_code'], 'to_account_code': target['account_code'],
            'source_row_id': source['id'], 'target_row_id': target['id'],
            'match_date': source['transaction_date'], 'source_date': source['transaction_date'],
            'target_date': target['transaction_date'], 'source_summary': source.get('summary'),
            'target_summary': target.get('summary'), 'date_delta_days': delta, 'transfer_hint_count': hints,
            'amount': -source['amount'], 'currency': source['currency'], 'status': 'review_required',
            'ambiguity_status': 'ambiguous' if ambiguous else 'unique_candidate', 'confirmation_supported': True,
            'note': '同币种等额，' + ('两边有划转线索' if hints == 2 else '单边有划转线索'),
        })
    return result


def list_candidates(cur, profiles, *, limit):
    rows = load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY', include_pair_evidence=True)
    return suggest(rows, profiles, occupied_cashflow_ids(cur), limit=limit)
