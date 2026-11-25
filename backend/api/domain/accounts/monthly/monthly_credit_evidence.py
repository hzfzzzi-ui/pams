"""Project only completed months inside a reviewed recurring-credit balance proof."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from domain.imports.sources.source_original_read import SourceOriginals
from domain.investments.holdings.snapshot_coverage import load_account_snapshot_coverage
from domain.market.month_end_marks import is_month_end


# The stored attestation encodes English month names regardless of process locale.
_MONTHS = 'jan feb mar apr may jun jul aug sep oct nov dec'.split()


def _intact(row):
    digest = sha256(json.dumps(row['raw_fields'], ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')).encode()).hexdigest()
    return digest == row['record_hash']


def confirmed_months(opening_day, opening_amount, proof, contribution, currency, dates):
    try:
        anchor = date.fromisoformat(proof['anchor_date'])
        start = opening_day + timedelta(days=1)
        count = (anchor.year - opening_day.year) * 12 + anchor.month - opening_day.month
        step, end = Decimal(proof['monthly_credit']), Decimal(proof['anchor_amount'])
        if (not is_month_end(opening_day) or not is_month_end(anchor) or count < 1
                or not step.is_finite() or step <= 0 or not end.is_finite()
                or proof['months'] != count or not opening_amount.is_finite()
                or Decimal(proof['result']) != opening_amount or opening_amount + count * step != end
                or start.year != anchor.year or proof['other_movements'] !=
                    f'user_confirmed_none_{start.year}_{_MONTHS[start.month - 1]}_{_MONTHS[anchor.month - 1]}'
                or contribution.get('kind') != 'contribution_evidence' or contribution.get('currency') != currency
                or Decimal(contribution['monthly_personal_amount']) != step
                or contribution.get('period') != f'{start:%Y-%m}/{anchor:%Y-%m}'):
            return {}
        return {d: opening_amount + ((d.year - opening_day.year) * 12 + d.month - opening_day.month) * step
                for d in dates if opening_day < d <= anchor and is_month_end(d)}
    except (InvalidOperation, ValueError, KeyError, TypeError):
        return {}


def recurring_credit_months(cur, accounts, dates):
    if not any(is_month_end(d) for d in dates):
        return {}
    codes = [code for code, (_, card) in accounts.items() if not card]
    if not codes:
        return {}
    cur.execute("""SELECT h.id,h.account_code,h.as_of,h.currency,h.market_value,i.raw_fields,i.record_hash,
                    r.account_code source_account,r.source_id original_source_id,r.sha256,r.content IS NOT NULL inline_original
        FROM pams_holdings_snapshot h JOIN source_items i ON h.source_item_id=i.source_item_id
        JOIN source_records r USING(source_id) WHERE h.account_code=ANY(%s::text[]) AND r.account_code=h.account_code
        AND h.as_of<%s AND r.status='published' AND i.review_status='confirmed' AND r.document_type='holding'
        AND i.raw_fields->'entered_fields'=i.parsed_fields AND h.asset_liability='资产'
        AND i.raw_fields#>>'{derivation,method}'='user_confirmed_reverse_balance'""", (codes, max(dates)))
    rows = cur.fetchall()
    coverage = load_account_snapshot_coverage(cur, {(r['account_code'], r['as_of']) for r in rows}) if rows else {}
    originals = SourceOriginals(cur)
    candidates = {}
    for row in rows:
        code, first, unit = row['account_code'], row['as_of'], row['currency']
        if (accounts[code][0] != unit or not _intact(row) or not row['sha256'] or not originals.verify(row)
                or row['id'] not in coverage[code, first]['member_ids']):
            continue
        fields = row['raw_fields']['entered_fields']
        if fields.get('business_date') != first.isoformat() or fields.get('currency') != unit:
            continue
        try:
            if Decimal(str(fields.get('net_cash'))) != row['market_value']:
                continue
        except InvalidOperation:
            continue
        proof = row['raw_fields']['derivation']
        refs = proof.get('supporting_sources') or []
        if not refs or not all(isinstance(ref,dict) and ref.get('source_item_id') for ref in refs):
            continue
        cur.execute("""SELECT i.source_item_id::text item_id,i.parsed_fields,i.raw_fields,i.record_hash,
                       r.source_id original_source_id,r.account_code source_account,r.sha256,r.content IS NOT NULL inline_original
              FROM source_items i JOIN source_records r USING(source_id) WHERE r.account_code=%s
              AND i.source_item_id::text=ANY(%s::text[]) AND i.review_status='confirmed'
              AND r.document_type='statement' AND r.acquisition_method IN ('screenshot','file')""",
                    (code,[ref['source_item_id'] for ref in refs]))
        linked = {r['item_id']:r for r in cur.fetchall()}
        if any(not (r := linked.get(ref['source_item_id'])) or not r['sha256'] or ref.get('sha256') != r['sha256']
               or str(r['original_source_id']) != ref.get('source_id') or not _intact(r)
               or not originals.verify(r) for ref in refs):
            continue
        for record in linked.values():
            if record['raw_fields'].get('entered_fields') != record['parsed_fields']:
                continue
            for day, amount in confirmed_months(first, row['market_value'], proof, record['parsed_fields'], unit, dates).items():
                candidates.setdefault((code, day), set()).add(amount)
    return {key: next(iter(values)) for key, values in candidates.items() if len(values)==1}
