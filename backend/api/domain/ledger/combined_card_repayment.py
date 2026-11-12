"""Find and confirm one bank debit covering multiple cards at the same issuer."""
from collections import defaultdict
from decimal import Decimal
from itertools import combinations
from uuid import uuid4

from psycopg2.extras import Json

from domain.ledger import confirmed_transfers_v2 as transfers
from domain.ledger.cash_transfer_pairs import CARD_PAYMENT, FX_REVIEW_TOLERANCE, _cash_text
from domain.ledger.cashflow_classification import personal_loan_principal
from domain.ledger.cashflow_read_v2 import load_cash_rows


def _eligible(row):
    return (row['source_evidence_status'] == 'verified' and row.get('amount_cny') is not None
            and not personal_loan_principal(row))


def _issuer(row, profiles):
    profile = profiles.get(row['account_code'], {})
    if (profile.get('account_type') == 'liability' and row['amount'] > 0
            and _eligible(row) and '合并' in _cash_text(row) and '还款' in _cash_text(row)):
        return profile.get('institution')
    return None


def _totals(source, targets, issuer, profiles):
    if (not _eligible(source) or source['amount'] >= 0
            or profiles.get(source['account_code'], {}).get('account_type') != 'funding'
            or not CARD_PAYMENT.search(_cash_text(source)) or issuer not in _cash_text(source)
            or len({row['account_code'] for row in targets}) != len(targets)
            or any(row['transaction_date'] != source['transaction_date']
                   or not transfers.is_independent_opposite_pair(source['pair_evidence'], row['pair_evidence'])
                   for row in targets)
            or len({row['pair_evidence']['source_item_id'] for row in targets}) != len(targets)
            or any(row['amount_cny'] <= 0 for row in targets)):
        return None
    received = sum(row['amount_cny'] for row in targets)
    difference = -source['amount_cny'] - received
    if all(row['currency'] == source['currency'] for row in targets):
        if -source['amount'] != sum(row['amount'] for row in targets):
            return None
    elif abs(difference) > -source['amount_cny'] * FX_REVIEW_TOLERANCE:
        return None
    return received, difference


def candidates(rows, profiles, occupied, *, anchor_id=None):
    available = [row for row in rows if row['id'] not in occupied]
    receipts = defaultdict(list)
    for row in available:
        issuer = _issuer(row, profiles)
        if issuer:
            receipts[row['transaction_date'], issuer].append(row)
    for source in available:
        if source['amount'] >= 0 or not CARD_PAYMENT.search(_cash_text(source)):
            continue
        for (day, issuer), pool in receipts.items():
            if day != source['transaction_date'] or issuer not in _cash_text(source):
                continue
            # Each card can contribute at most one leg in a group. Allow more
            # than two cards while bounding the candidate search per issuer/day.
            if len(pool) > 12:
                continue
            for size in range(2, min(4, len(pool)) + 1):
                for targets in combinations(pool, size):
                    ids = [row['id'] for row in targets]
                    if anchor_id is not None and anchor_id not in [source['id'], *ids]:
                        continue
                    totals = _totals(source, targets, issuer, profiles)
                    if totals is not None:
                        yield {
                            'match_key': 'combined:' + ':'.join(map(str, [source['id'], *sorted(ids)])),
                            'match_type': 'credit_card_payment', 'confirmation_supported': True,
                            'from_account_code': source['account_code'], 'to_account_code': targets[0]['account_code'],
                            'source_row_id': source['id'], 'target_row_id': targets[0]['id'],
                            'source_date': day, 'target_date': day, 'match_date': day,
                            'amount': -source['amount'], 'currency': source['currency'],
                            'amount_cny': -source['amount_cny'], 'received_cny': totals[0],
                            'reference_difference_cny': totals[1], 'source': _display_leg(source),
                            'targets': [_display_leg(row) for row in sorted(targets, key=lambda row: row['account_code'])],
                        }


def _display_leg(row):
    return {key: row.get(key) for key in ('id', 'account_code', 'amount', 'currency', 'summary',
                                         'description', 'transaction_date', 'amount_cny')}


def confirm(cur, *, source_id, target_ids, note, confirmed, profiles):
    ids = [source_id, *target_ids]
    if (cur.connection.autocommit or confirmed is not True or not note.strip() or len(note) > 1000
            or not 2 <= len(target_ids) <= 4 or len(set(ids)) != len(ids)
            or not all(transfers._valid_id(ident) for ident in ids)):
        raise ValueError('请明确确认整组还款，并选择不同的流水。')
    cur.execute('SELECT cashflow_id, account_code FROM public.pams_cashflow_fact WHERE cashflow_id=ANY(%s)', (ids,))
    accounts = {row['cashflow_id']: row['account_code'] for row in cur.fetchall()}
    if set(accounts) != set(ids):
        raise ValueError('还款流水不存在。')
    for account in sorted(set(accounts.values())):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account,))
    facts = {row['id']: row for row in transfers._leg_rows(cur, 'cashflow', sorted(ids), lock=True)}
    if {ident: row['account_code'] for ident, row in facts.items()} != accounts:
        raise ValueError('还款账户已变更，请重新查找。')
    originals = transfers.SourceOriginals(cur)
    legs = {ident: transfers._verified_leg(row, 'cashflow', originals) for ident, row in facts.items()}
    first, *others = sorted(target_ids)
    right = {**legs[first], 'additional_targets': [legs[ident] for ident in others]}
    cur.execute("""SELECT confirmation_id, from_evidence, to_evidence, note FROM public.evidence_confirmation
        WHERE confirmation_type='transfer' AND revoked_at IS NULL AND from_cashflow_id=%s""", (source_id,))
    existing = cur.fetchall()
    if len(existing) == 1 and (existing[0]['from_evidence'], existing[0]['to_evidence'], existing[0]['note']) == (
        legs[source_id], right, note.strip(),
    ):
        return {'transfer_id': str(existing[0]['confirmation_id']), 'replayed': True}
    if set(ids) & transfers.occupied_cashflow_ids(cur):
        raise ValueError('有流水已经参加其他配对，请刷新后重试。')
    day = facts[source_id]['day']
    rows = load_cash_rows(cur, start=day, end=day, base_currency='CNY',
                          account_codes=sorted(set(accounts.values())), originals=originals, include_pair_evidence=True)
    selected = [row for row in rows if row['id'] in ids]
    if not any(item['source_row_id'] == source_id and {leg['id'] for leg in item['targets']} == set(target_ids)
               for item in candidates(selected, profiles, set())):
        raise ValueError('合并还款凭证或折算金额不符合条件，请重新核对。')
    transfer_id = str(uuid4())
    cur.execute("""INSERT INTO public.evidence_confirmation
        (confirmation_id, confirmation_type, from_cashflow_id, to_cashflow_id, from_account_code,
         to_account_code, amount, currency, from_evidence, to_evidence, note)
        VALUES (%s,'transfer',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (transfer_id, source_id, first, accounts[source_id], accounts[first], -Decimal(legs[source_id]['amount']),
         legs[source_id]['currency'], Json(legs[source_id]), Json(right), note.strip()))
    return {'transfer_id': transfer_id, 'replayed': False}
