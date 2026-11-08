"""Confirm bank evidence independently of the cash ledger."""

from decimal import InvalidOperation

from domain.imports.cash_preview_v2 import _fields


SOURCE_ONLY_ACCOUNTS = frozenset({'ccb_7332', 'ccb_credit_0453', 'gf_7443', 'gf_9711', 'santander'})


def confirm(cur, source_id, revision):
    cur.execute("""SELECT account_code,status,revision FROM public.source_records
        WHERE source_id=%s AND document_type='cash' AND
        (acquisition_method='manual' OR (acquisition_method IN ('file','screenshot') AND sha256 IS NOT NULL))
        FOR UPDATE""", (str(source_id),))
    source = cur.fetchone()
    if not source or source['account_code'] not in SOURCE_ONLY_ACCOUNTS:
        raise ValueError('此资料不支持银行来源确认')
    if source['status'] == 'published' and source['revision'] == revision + 1:
        return {'source_id': str(source_id), 'revision': source['revision']}
    if source['status'] != 'draft' or source['revision'] != revision:
        from domain.imports.source_records_v2 import SourceRevisionConflict
        raise SourceRevisionConflict('来源资料已变化')
    cur.execute("""SELECT parsed_fields,raw_fields,review_status FROM public.source_items
        WHERE source_id=%s FOR UPDATE""", (str(source_id),))
    items = cur.fetchall()
    if not items:
        raise ValueError('来源资料没有可确认的明细')
    for item in items:
        if item['review_status'] != 'excluded':
            try:
                _fields(item)
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValueError('请先核对资料中的日期、金额和币种') from exc
    cur.execute("""UPDATE public.source_items SET review_status='confirmed',updated_at=now()
        WHERE source_id=%s AND review_status='pending'""", (str(source_id),))
    cur.execute("""UPDATE public.source_records SET status='published',revision=revision+1,updated_at=now()
        WHERE source_id=%s""", (str(source_id),))
    return {'source_id': str(source_id), 'revision': revision + 1}
