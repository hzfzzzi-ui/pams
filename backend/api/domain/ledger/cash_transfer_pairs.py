"""Suggest pairs between independent, source-verified cash facts."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import re

from domain.ledger.cashflow_read_v2 import load_cash_rows
from domain.ledger import confirmed_transfers_v2 as confirmation
from domain.ledger.confirmed_transfers_v2 import is_independent_equal_pair, is_independent_opposite_pair, occupied_cashflow_ids
from domain.imports.source_original_read import SourceOriginals


TRANSFER_HINTS = {'internal_transfer', 'investment_funding', 'investment_withdrawal', 'debt_repayment'}
TRANSFER_TEXT = re.compile(r'信用卡.{0,10}还款|银转证|证转银|银证转账|内部划转|同行转账|'
                           r'electronic fund transfers?|transferencia a interactive brokers', re.IGNORECASE)
INVESTMENT_TYPES = {'brokerage', 'fund_platform'}
MAX_DAYS = 3
CARD_PAYMENT = re.compile(r'信用卡.{0,10}还款')
FX_REPAYMENT = re.compile(r'购汇还')
FX_REVIEW_TOLERANCE = Decimal('0.01')


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


def _cash_text(row):
    return ' '.join(row.get(key) or '' for key in ('summary', 'description', 'counterparty'))


def _fx_repayment_target(row, profiles):
    text = _cash_text(row)
    return (profiles.get(row['account_code'], {}).get('account_type') == 'liability'
            and FX_REPAYMENT.search(text) and '合并' not in text)


def is_cross_currency_card_repayment(source, target, profiles):
    """Narrow suggestions to same-day, independently evidenced card repayments."""
    source_profile = profiles.get(source['account_code'], {})
    target_institution = profiles.get(target['account_code'], {}).get('institution')
    source_text = _cash_text(source)
    if (source['currency'] == target['currency'] or source['transaction_date'] != target['transaction_date']
            or source_profile.get('account_type') != 'funding' or not target_institution
            or target_institution not in source_text or not CARD_PAYMENT.search(source_text)
            or '合并' in source_text or not _fx_repayment_target(target, profiles)
            or any(row.get('source_evidence_status') != 'verified' for row in (source, target))
            or not is_independent_opposite_pair(source['pair_evidence'], target['pair_evidence'])):
        return False
    outgoing, incoming = source.get('amount_cny'), target.get('amount_cny')
    return (outgoing is not None and incoming is not None and outgoing < 0 < incoming
            and abs(outgoing + incoming) <= -outgoing * FX_REVIEW_TOLERANCE)


def suggest(rows, profiles, occupied, *, limit=None):
    available = [row for row in rows if row['id'] not in occupied and row['source_evidence_status'] == 'verified'
                 and row['amount'] and row['currency'] and row['transaction_date']]
    deposits = defaultdict(list)
    fx_repayments = defaultdict(list)
    for row in available:
        if row['amount'] > 0:
            deposits[row['currency'], row['amount']].append(row)
            if _fx_repayment_target(row, profiles):
                fx_repayments[row['transaction_date']].append(row)

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
        for target in fx_repayments[source['transaction_date']]:
            if is_cross_currency_card_repayment(source, target, profiles):
                matches.append((source, target, 0, 2))

    usage = Counter(row['id'] for source, target, _, _ in matches for row in (source, target))
    matches.sort(key=lambda match: (any(usage[row['id']] > 1 for row in match[:2]), -match[3], match[2],
                                    -match[0]['transaction_date'].toordinal(), match[0]['id'], match[1]['id']))
    result = []
    for source, target, delta, hints in matches[:limit]:
        ambiguous = usage[source['id']] > 1 or usage[target['id']] > 1
        different_currency = source['currency'] != target['currency']
        result.append({
            'match_key': f"candidate:{source['id']}:{target['id']}",
            'match_type': cash_pair_type(source['account_code'], target['account_code'], profiles),
            'from_account_code': source['account_code'], 'to_account_code': target['account_code'],
            'source_row_id': source['id'], 'target_row_id': target['id'],
            'match_date': source['transaction_date'], 'source_date': source['transaction_date'],
            'target_date': target['transaction_date'], 'source_summary': source.get('summary'),
            'target_summary': target.get('summary'), 'date_delta_days': delta, 'transfer_hint_count': hints,
            'source': {key: source.get(key) for key in ('summary', 'description', 'source_item_id')},
            'target': {key: target.get(key) for key in ('summary', 'description', 'source_item_id')},
            'amount': -source['amount'], 'currency': source['currency'], 'status': 'review_required',
            **({'target_amount': target['amount'], 'target_currency': target['currency'],
                'settlement_rate': -source['amount'] / target['amount']} if different_currency else {}),
            'ambiguity_status': 'ambiguous' if ambiguous else 'unique_candidate', 'confirmation_supported': True,
            'note': ('请按银行还款回单核对两边实扣和到账金额。' if different_currency
                     else '同币种等额，' + ('两边有划转线索' if hints == 2 else '单边有划转线索')),
        })
    return result


def _redemption_candidates(cur, rows, occupied):
    receipts = [row for row in rows if row['id'] not in occupied
                and row['source_evidence_status'] == 'verified'
                and row.get('summary') == '理财通活期+转入'
                and row['account_code'] == confirmation.LICAITONG_CURRENT_PLUS]
    if not receipts:
        return []
    cur.execute("""SELECT trade_id FROM public.pams_investment_trade_fact f
        WHERE f.account_code=%s AND f.trade_type='fund_redemption'
          AND NOT EXISTS (SELECT 1 FROM public.evidence_confirmation e
                          WHERE e.revoked_at IS NULL AND f.trade_id IN (e.from_trade_id,e.to_trade_id))
        ORDER BY trade_date DESC,trade_id DESC LIMIT 500""", (confirmation.REDEMPTION_ACCOUNT,))
    ids = [row['trade_id'] for row in cur.fetchall()]
    if not ids:
        return []
    originals = SourceOriginals(cur)
    receipt_facts = {fact['id']: fact for fact in
                     confirmation._leg_rows(cur, 'cashflow', [row['id'] for row in receipts], lock=False)}
    candidates = []
    for fact in confirmation._leg_rows(cur, 'trade', ids, lock=False):
        try:
            left = confirmation._verified_leg(fact, 'trade', originals)
        except ValueError:
            continue
        for row in receipts:
            right = {**row['pair_evidence'], 'kind': 'cashflow', 'id': row['id'],
                     'day': str(row['transaction_date'])}
            if not is_independent_equal_pair(left, right):
                continue
            receipt = receipt_facts.get(right['id'])
            target_time = confirmation.redemption_receipt_time(left, receipt) if receipt else None
            if target_time is None:
                continue
            candidates.append({
                'match_key': f"candidate:redemption:{left['id']}:{right['id']}",
                'match_type': 'internal_transfer', 'status': 'review_required',
                'from_account_code': left['account_code'], 'to_account_code': right['account_code'],
                'source_kind': 'trade', 'target_kind': 'cashflow',
                'source_row_id': left['id'], 'target_row_id': right['id'],
                'match_date': right['day'], 'source_date': left['day'], 'target_date': right['day'],
                'amount': -Decimal(left['amount']), 'currency': left['currency'],
                'source_summary': (fact['parsed_fields'].get('name') or '') + ' · 基金赎回',
                'target_summary': row['summary'], 'confirmation_supported': True,
                'source': {'description': f"截图到账 {left['receipt_day']} {left['receipt_time']}",
                           'source_item_id': left['source_item_id']},
                'target': {'description': f"截图转入 {right['day']} {target_time}",
                           'source_item_id': str(right['source_item_id'])},
                'ambiguity_status': 'unique_candidate',
                'note': '基金赎回原件显示到账活期+，与转入流水的日期、时间和金额一致。',
            })
    return candidates


def list_candidates(cur, profiles, *, limit):
    rows = load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY', include_pair_evidence=True)
    occupied = occupied_cashflow_ids(cur)
    candidates = _redemption_candidates(cur, rows, occupied) + suggest(rows, profiles, occupied)
    receipt_use = Counter(item['target_row_id'] for item in candidates)
    redemption_use = Counter(item['source_row_id'] for item in candidates if item.get('source_kind') == 'trade')
    for item in candidates:
        if receipt_use[item['target_row_id']] > 1 or (
            item.get('source_kind') == 'trade' and redemption_use[item['source_row_id']] > 1
        ):
            item['ambiguity_status'] = 'ambiguous'
    return candidates[:limit]
