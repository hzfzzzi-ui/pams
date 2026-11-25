"""Unbilled installment principal between a verified disbursement and a dated holding."""
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
from zipfile import BadZipFile, ZipFile

from domain.imports.sources.source_fact_evidence import _digest
from domain.imports.sources.source_original_read import SourceOriginals
from domain.investments.valuation.nav_period_comparison import position_member
from domain.investments.holdings.snapshot_coverage import load_account_snapshot_coverage
from domain.ledger.cashflow_read_v2 import load_cash_rows


def _matches_order(flow, order):
    # Link by the reviewed fact ID first. Independently require its exact order
    # reference in the verified transaction fields, never a merchant/amount match.
    fields = (flow.get(key) or '' for key in ('summary', 'description', 'counterparty'))
    return bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(order) + r'(?![A-Za-z0-9])', ' '.join(fields)))


def _principal_steps(row, facts):
    raw, fields = row['raw_fields'], row['parsed_fields']
    try:
        order = raw['order_number']
        if (not isinstance(order, str) or not order.strip() or fields['order_number'] != order
                or fields['asset_type'] != 'liability' or fields['currency'] != row['currency']
                or fields['symbol'] != row['symbol'] or not row['symbol']
                or date.fromisoformat(fields['business_date']) != row['as_of']):
            return None
        principal = Decimal(raw['contract_principal'])
        residual = Decimal(raw['unbilled_principal'])
        if (not principal.is_finite() or not residual.is_finite() or not 0 <= residual <= principal
                or principal <= 0 or row['market_value'] != residual or Decimal(fields['market_value']) != residual):
            return None
        funding_id = raw['bank_funding_fact']
        funding = facts[funding_id]
        if (funding['account_code'] == row['account_code'] or funding['amount'] != principal
                or funding['currency'] != row['currency'] or not _matches_order(funding, order)):
            return None
        start = funding['posting_date'] or funding['transaction_date']
        if start > row['as_of']:
            return None
        used = {funding_id}
        balance = principal
        steps = {start: balance}
        previous = start
        postings = raw['verified_postings']
        if (not isinstance(postings, list) or len(postings) > raw['contract_periods']
                or raw['paid_periods_user_confirmed'] != len(postings)):
            return None
        for number, posting in enumerate(postings, 1):
            day = date.fromisoformat(posting['date'])
            ids, principal_id = posting['fact_ids'], posting['principal_fact']
            part, interest = Decimal(posting['principal']), Decimal(posting['net_interest'])
            if (posting['period'] != number or not previous <= day <= row['as_of']
                    or not isinstance(ids, list) or principal_id not in ids
                    or len(set(ids)) != len(ids) or used.intersection(ids)
                    or not part.is_finite() or not 0 < part <= balance or not interest.is_finite()):
                return None
            flows = [facts[ident] for ident in ids]
            if (any(f['account_code'] != row['account_code'] or f['currency'] != row['currency']
                    or (f['posting_date'] or f['transaction_date']) != day or not _matches_order(f, order)
                    for f in flows) or facts[principal_id]['amount'] != -part
                    or sum((facts[i]['amount'] for i in ids if i != principal_id), Decimal(0)) != -interest):
                return None
            used.update(ids)
            balance -= part
            steps[day] = balance
            previous = day
        if balance == residual:
            return steps
    except (KeyError, TypeError, ValueError, InvalidOperation):
        pass
    return None


def _verify_attachments(originals, row):
    content, verified = originals.read(row)
    if not content or not verified or _digest(row['raw_fields']) != row['record_hash']:
        return False
    expected = row['raw_fields'].get('original_sha256')
    if not isinstance(expected, list) or not expected:
        return False
    try:
        with ZipFile(BytesIO(content)) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            return (len(files) == len(expected) and
                    sorted(sha256(archive.read(entry)).hexdigest() for entry in files) == sorted(expected))
    except (BadZipFile, TypeError, RuntimeError, OSError):
        return False


def _verified_contracts(cur, accounts, since):
    if not accounts:
        return
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS supported")
    if not cur.fetchone()['supported']:
        return
    cur.execute("""SELECT h.id,h.as_of,h.account_code,h.currency,h.asset_liability,h.asset_id,h.symbol,h.name,h.market_value,
               i.raw_fields,i.parsed_fields,i.record_hash,r.source_id original_source_id,r.account_code source_account,
               r.content IS NOT NULL inline_original,r.sha256
        FROM pams_holdings_snapshot h JOIN source_items i USING (source_item_id) JOIN source_records r USING(source_id)
        WHERE h.account_code=ANY(%s) AND h.as_of>=%s AND h.asset_liability='负债' AND r.account_code=h.account_code
          AND i.review_status='confirmed' AND r.status='published' AND r.acquisition_method='file'
          AND r.document_type='holding' AND r.file_format='zip'
          AND i.raw_fields ?& ARRAY['contract_principal','verified_postings','unbilled_principal','bank_funding_fact']""",
                (sorted(accounts), since))
    rows = cur.fetchall()
    if not rows:
        return
    coverage = load_account_snapshot_coverage(cur, {(r['account_code'], r['as_of']) for r in rows})
    originals = SourceOriginals(cur)
    verified = []
    order_members = defaultdict(set)
    member_orders = defaultdict(set)
    for row in rows:
        if row['id'] not in coverage[row['account_code'], row['as_of']]['member_ids'] or not _verify_attachments(originals, row):
            continue
        raw = row['raw_fields']
        try:
            ids = {raw['bank_funding_fact'], *(i for p in raw['verified_postings'] for i in p['fact_ids'])}
            if not all(type(i) is int and i > 0 for i in ids):
                continue
        except (KeyError, TypeError):
            continue
        facts = load_cash_rows(cur, start=date.min, end=row['as_of'], base_currency='CNY', fact_ids=sorted(ids), originals=originals,
                               include_pair_evidence=True)
        facts = {f['id']:f for f in facts if f['source_evidence_status']=='verified' and f['pair_evidence']['source_hash']}
        steps = _principal_steps(row, facts)
        if not steps:
            continue
        member = position_member(row)
        contract = (row['account_code'], raw['order_number'])
        order_members[contract].add(member)
        member_orders[row['account_code'], member].add(contract)
        verified.append((row, member, contract, steps))
    for row, member, contract, steps in verified:
        if len(order_members[contract]) == 1 and len(member_orders[row['account_code'], member]) == 1:
            yield row, member, contract, steps


def load_principal_cash_ids(cur, accounts, since):
    """Cash funding and separately verified principal postings for recognized liabilities."""
    claims = defaultdict(set)
    for row, _, contract, _ in _verified_contracts(cur, accounts, since):
        raw = row['raw_fields']
        claims[raw['bank_funding_fact']].add((contract, 'principal'))
        for posting in raw['verified_postings']:
            for ident in posting['fact_ids']:
                role = 'principal' if ident == posting['principal_fact'] else 'interest'
                claims[ident].add((contract, role))
    return {ident for ident, roles in claims.items() if len(roles) == 1 and next(iter(roles))[1] == 'principal'}


def load_unbilled_principal(cur, accounts, dates):
    """Return contract members only on covered dates, separately from the card statement balance."""
    if not dates:
        return {}
    result = defaultdict(lambda: defaultdict(set))
    for row, member, _, steps in _verified_contracts(cur, accounts, min(dates)):
        transitions = sorted(steps)
        for day in dates:
            if transitions[0] <= day <= row['as_of']:
                changed = transitions[bisect_right(transitions, day) - 1]
                result[day, row['account_code']][member].add((steps[changed], changed, transitions[0]))
    points = defaultdict(dict)
    for (day, code), members in result.items():
        for member, values in members.items():
            if len(values) == 1:
                points[day, code][member] = next(iter(values))
    return points
