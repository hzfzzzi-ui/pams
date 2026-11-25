"""Resolve an explicitly reviewed fund disbursement to its posted cash receipt."""
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from domain.imports.sources.source_fact_evidence import _digest
from domain.imports.sources.source_original_read import SourceOriginals
from domain.imports.publication.trade_supplemental_evidence import read as read_trade_evidence


def load(cur, trade):
    proofs = [p for p in read_trade_evidence(cur, trade_id=trade['id'])
              if p['status'] == 'verified' and (p['fields'] or {}).get('settlement_receipt')]
    if len(proofs) != 1:
        return None
    proof = proofs[0]
    receipt = proof['fields']['settlement_receipt']
    try:
        cash_id = receipt['cashflow_id']
        if type(cash_id) is not int or cash_id <= 0:
            return None
        observation_id = str(UUID(receipt['observation_item_id']))
        day = date.fromisoformat(receipt['posting_date'])
    except (KeyError, ValueError, AttributeError, TypeError):
        return None
    cur.execute("""SELECT f.cashflow_id,f.source_item_id,f.account_code,f.amount,f.currency,f.event_date,
        COALESCE(f.posting_date,f.event_date) AS posted,i.raw_fields,i.parsed_fields,i.record_hash,i.review_status,
        r.source_id AS original_source_id,r.sha256,r.content IS NOT NULL AS inline_original,
        r.account_code AS source_account
        FROM public.pams_cashflow_fact f JOIN public.source_items i ON i.source_item_id=%s
        JOIN public.source_records r ON r.source_id=i.source_id AND r.account_code=f.account_code
        WHERE f.cashflow_id=%s AND r.status='published' AND r.document_type='cash'
          AND r.acquisition_method IN ('screenshot','file')""", (observation_id, cash_id))
    row = cur.fetchone()
    if not row:
        return None
    observed = row['parsed_fields']
    # A duplicate row can corroborate an existing fact only when explicitly
    # cross-referenced. It never creates a second cashflow.
    try:
        valid = ((row['review_status'] == 'confirmed' and observation_id == str(row['source_item_id'])
                  or row['review_status'] == 'excluded' and row['raw_fields'].get('existing_same_movement_id') == cash_id)
                 and row['record_hash'] == _digest(row['raw_fields'])
                 and row['record_hash'] == receipt['observation_record_hash']
                 and row['sha256'] and row['sha256'] == receipt['observation_sha256'] != trade['sha256']
                 and str(row['source_item_id']) == receipt['cash_source_item_id']
                 and row['account_code'] == receipt['account_code'] != trade['account_code']
                 and row['currency'] == trade['currency'] == observed['currency']
                 and row['amount'] == trade['amount'] == Decimal(str(observed['net_cash']))
                 and row['amount'] > 0 and trade['day'] <= day == row['posted']
                 and day.isoformat() == (observed.get('posting_date') or observed['business_date'])
                 and SourceOriginals(cur).verify(row))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if valid:
        return dict(settlement_confirmation_id=proof['confirmation_id'], bank_cashflow_id=cash_id,
                    bank_source_item_id=str(row['source_item_id']), target_account=row['account_code'],
                    cash_event_day=str(row['event_date']), receipt_day=str(day))
    return None
