"""Read redesigned cash facts with verified origin and explicit classification state."""
from __future__ import annotations

from hashlib import sha256
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from domain.imports.source_original_read import SourceOriginals


def load_cash_rows(cur, *, start, end, base_currency: str, fact_id: int | None = None,
                   account_codes: list[str] | None = None, originals: SourceOriginals | None = None) -> list[dict[str, Any]]:
    if base_currency != 'CNY':
        raise ValueError('only evidenced CNY projection is supported')
    filters, params = [], [base_currency, start, end]
    if fact_id is not None:
        filters.append(' AND f.cashflow_id=%s')
        params.append(fact_id)
    if account_codes is not None:
        filters.append(' AND f.account_code=ANY(%s::text[])')
        params.append(account_codes)
    cur.execute('''SELECT f.cashflow_id AS id,f.cashflow_id AS fact_id,f.source_item_id,f.account_code,
        f.event_date AS transaction_date,f.amount,f.currency,f.summary,f.description,f.counterparty,
        f.posting_date,f.created_at,f.updated_at,i.raw_fields,i.parsed_fields,i.record_hash,i.review_status AS item_review,
        r.status AS source_status,r.account_code AS source_account,r.source_id AS original_source_id,
        r.content IS NOT NULL AS inline_original,r.sha256,
        c.cashflow_type,c.category,c.subcategory,c.status AS classification_status,c.classification_source,
        fx.rate AS fx_rate
        FROM public.pams_cashflow_fact f
        JOIN public.source_items i ON i.source_item_id=f.source_item_id
        JOIN public.source_records r ON r.source_id=i.source_id
        LEFT JOIN public.cashflow_classification c ON c.cashflow_id=f.cashflow_id
        LEFT JOIN LATERAL (SELECT rate FROM public.pams_fx_rates_daily x
          WHERE x.base_currency=%s AND x.quote_currency=f.currency AND x.as_of=f.event_date
            AND x.rate>0 AND x.rate::text NOT IN ('NaN','Infinity','-Infinity')
            AND NULLIF(btrim(x.source),'') IS NOT NULL LIMIT 1) fx ON TRUE
        WHERE f.event_date >= %s AND f.event_date <= %s''' + ''.join(filters) +
        ' ORDER BY f.event_date DESC,f.cashflow_id DESC', params)
    names=[x[0] for x in cur.description]
    out=[]
    if originals is None:
        originals = SourceOriginals(cur)
    for record in cur.fetchall():
        row=dict(record) if hasattr(record,'keys') else dict(zip(names,record))
        raw=json.dumps(row['raw_fields'],ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')
        fields=row['parsed_fields'] or {}
        try:
            fact_matches=(date.fromisoformat(fields['business_date'])==row['transaction_date'] and
                          Decimal(str(fields['net_cash']))==row['amount'] and fields['currency']==row['currency'] and
                          (date.fromisoformat(fields['posting_date']) if fields.get('posting_date') else None)==row['posting_date'] and
                          all(fields.get(key)==row[key] for key in ('summary','description','counterparty')))
        except (ValueError,TypeError,KeyError,InvalidOperation):
            fact_matches=False
        verified=(row['source_status']=='published' and row['item_review']=='confirmed' and fact_matches and
                  row['source_account']==row['account_code'] and sha256(raw).hexdigest()==row['record_hash'] and
                  originals.read(row)[1])
        rate=1 if row['currency']==base_currency else row['fx_rate']
        row['amount_cny']=row['amount']*rate if verified and rate is not None else None
        row['fx_basis']='native_base' if row['currency']==base_currency else ('daily_market_projection' if rate is not None else 'unavailable')
        row['source_evidence_status']='verified' if verified else 'unknown'
        if not verified: row['source_issue']='source_evidence_unknown'
        row['classification_status']=row['classification_status'] or 'pending'
        row['transaction_date_obj']=row['transaction_date']
        row['source_table']='source_items';row['source_id']=str(row['source_item_id'])
        row['counter_account_code']=None;row['is_internal_transfer']=False;row['transfer_group_id']=None
        for key in ('raw_fields','parsed_fields','record_hash','original_source_id','inline_original','sha256',
                    'source_account','item_review','source_status','fx_rate'):
            row.pop(key,None)
        out.append(row)
    return out
