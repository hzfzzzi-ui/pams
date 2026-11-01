"""Confirmed TradingView source publication for disposable databases only."""
from __future__ import annotations

import base64
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID

from collectors.tradingview_portfolio import _parse_time, _to_decimal, normalize_symbol
from domain.imports import isolation
from domain.imports.tradingview_source import _prepare
from domain.imports.unified_source import lock_source_review, review_item
from domain.imports.fact_associations import check_all_source_links
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts


def _values(raw):
    side = raw['Side'].strip()
    quantity = _to_decimal(raw['Qty'], default=None)
    price = _to_decimal(raw['Fill Price'], default=None)
    fee = _to_decimal(raw['Commission'], default=Decimal('0'))
    if (quantity is None or not quantity.is_finite() or quantity <= 0 or fee is None or not fee.is_finite() or fee < 0 or
        any(value is not None and value != value.quantize(Decimal('0.000001')) for value in (quantity, price, fee)) or
        (side in {'Buy', 'Sell'} and (not normalize_symbol(raw['Symbol']) or price is None or price <= 0))):
        raise ValueError('TradingView amounts or symbol need explicit correction')
    return side, quantity, price, fee, _parse_time(raw['Closing Time'])


def source_row_from_item(batch, item, file, raw):
    """Normalize one reviewed source row to the investment fact contract."""
    side, quantity, price, fee, closing = _values(raw)
    fee_text = str(raw.get('Commission') or '').strip()
    if not fee_text:
        raise ValueError('TradingView commission is unknown')
    tax_text = str(raw.get('Tax') or '').strip()
    tax = _to_decimal(tax_text, default=None) if tax_text else None
    if tax_text and (tax is None or not tax.is_finite() or tax < 0 or
                     abs(tax) >= Decimal('1e18') or tax != tax.quantize(Decimal('0.0000000001'))):
        raise ValueError('TradingView tax is invalid or unrepresentable')
    if side == 'Deposit' and (fee != 0 or tax not in (None, Decimal('0'))):
        raise ValueError('TradingView deposit charge scope is unknown')
    return {'source_table': 'tradingview_csv', 'source_id': str(item['source_item_id']),
            'source_key': item['source_uid'], 'trade_date': closing.date(), 'trade_time': closing,
            'settle_date': None, 'account_code': batch['account_code'], 'broker': 'tradingview',
            'portfolio_code': 'A股', 'asset_id': None, 'symbol': normalize_symbol(raw['Symbol']) or raw['Symbol'].strip(),
            'name': raw['Symbol'].strip(), 'market': 'CN_A', 'currency': 'CNY', 'side': side,
            'quantity': quantity, 'price': price,
            'gross_amount': abs(quantity) if side in {'Deposit', 'Withdraw', 'Dividend'} else abs(quantity * price),
            'fee_amount': abs(fee), 'tax_amount': tax, 'net_cash': None, 'fx_rate_to_cny': Decimal('1'),
            'raw_json': raw, 'source_file': file['filename']}


def inspect_batch(cur, *, batch_id, file_id=None):
    """Inspect raw source rows and optionally the original CSV in the isolated DB."""
    if not isolation.source_enabled(cur) or cur.connection.autocommit:
        raise isolation.IsolationError('TradingView inspection requires isolated transaction')
    batch_id = str(UUID(str(batch_id)))
    cur.execute("SELECT batch_id,account_code,institution,status,revision FROM source_batches WHERE batch_id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch or batch['institution'] != 'tradingview':
        raise ValueError('TradingView source batch is missing')
    cur.execute("SELECT file_id,filename,sha256 FROM source_files WHERE batch_id=%s ORDER BY filename,file_id", (batch_id,))
    files = [dict(row) for row in cur.fetchall()]
    cur.execute("""SELECT source_item_id,file_id,location,review_status,review_note,raw_fields
        FROM source_items WHERE batch_id=%s ORDER BY source_uid""", (batch_id,))
    items = [dict(row) for row in cur.fetchall()]
    result = {'batch': dict(batch), 'files': files, 'items': items}
    if file_id is not None:
        file_id = str(UUID(str(file_id)))
        cur.execute("SELECT content,sha256 FROM source_files WHERE batch_id=%s AND file_id=%s", (batch_id,file_id))
        file = cur.fetchone()
        if not file:
            raise ValueError('TradingView source file is missing')
        content = bytes(file['content'])
        if sha256(content).hexdigest() != file['sha256']:
            raise ValueError('TradingView source file changed')
        result['original_csv'] = content.decode('utf-8-sig')
        result['original_base64'] = base64.b64encode(content).decode('ascii')
    return result


def review_row(cur, *, batch_id, item_id, expected_revision, note):
    """Confirm one unchanged original CSV row; final publication remains separate."""
    if not isolation.source_enabled(cur) or cur.connection.autocommit:
        raise isolation.IsolationError('TradingView review requires isolated transaction')
    if not isinstance(note, str) or not note.strip() or len(note) > 1000:
        raise ValueError('TradingView row review requires a note')
    batch_id, item_id = UUID(str(batch_id)), UUID(str(item_id))
    cur.execute('SELECT account_code FROM source_batches WHERE batch_id=%s', (str(batch_id),))
    initial = cur.fetchone()
    if not initial:
        raise ValueError('TradingView source batch is missing')
    from domain.investments.holding_evidence_writer import lock_holding_accounts
    lock_holding_accounts(cur, [initial['account_code']])
    cur.execute("""SELECT b.account_code,b.institution,b.revision,b.status,i.raw_fields,i.parsed_fields,
        i.location,i.source_uid,i.record_hash,f.filename,f.sha256,f.content
        FROM source_batches b JOIN source_items i ON i.batch_id=b.batch_id
        JOIN source_files f ON f.file_id=i.file_id AND f.batch_id=b.batch_id
        WHERE b.batch_id=%s AND i.source_item_id=%s FOR UPDATE OF b,i FOR SHARE OF f""",
        (str(batch_id),str(item_id)))
    row = cur.fetchone()
    if (not row or row['account_code'] != initial['account_code'] or row['institution'] != 'tradingview'
            or row['status'] not in {'draft','review'} or row['revision'] != expected_revision):
        raise ValueError('TradingView source row is missing, stale or not editable')
    parsed = _prepare([(row['filename'], bytes(row['content']))])
    matches = [(digest, first, last, key, raw) for _, _, digest, rows in parsed
               for first, last, key, raw in rows if f'{first}-{last}' == row['location']]
    if len(matches) != 1:
        raise ValueError('TradingView original row is missing or ambiguous')
    digest, first, last, key, raw = matches[0]
    evidence = {'file_sha256': digest, 'line_start': first, 'line_end': last,
                'legacy_six_field_hash': key, 'raw_fields': raw}
    encoded = json.dumps(evidence,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    if (row['sha256'] != digest or row['raw_fields'] != evidence or
            row['record_hash'] != sha256(encoded).hexdigest() or row['source_uid'] != f'{digest}:{first}:{last}'):
        raise ValueError('TradingView source evidence changed')
    source_row_from_item({'account_code':row['account_code']},
        {'source_item_id':item_id,'source_uid':row['source_uid']}, {'filename':row['filename']}, raw)
    revision = review_item(cur, batch_id=batch_id, item_id=item_id, expected_revision=expected_revision,
                           parsed_fields=row['parsed_fields'], review_status='confirmed', review_note=note.strip())
    return {'batch_id':str(batch_id), 'item_id':str(item_id), 'revision':revision, 'review_status':'confirmed'}


def publish(cur, *, batch_id, confirm=False):
    """Publish explicitly confirmed records, or verify a published replay."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('TradingView publication requires isolated mode')
    if cur.connection.autocommit or confirm is not True:
        raise ValueError('TradingView publication requires a transaction and explicit confirmation')
    batch_id = str(UUID(str(batch_id)))
    cur.execute('SAVEPOINT tradingview_source_publish')
    try:
        from domain.investments.holding_evidence_writer import lock_holding_accounts
        cur.execute('SELECT account_code FROM public.source_batches WHERE batch_id=%s', (batch_id,))
        initial = cur.fetchone()
        if not initial:
            raise ValueError('TradingView source batch is missing or unavailable')
        lock_holding_accounts(cur, [initial['account_code']])
        cur.execute('SELECT * FROM public.source_batches WHERE batch_id=%s FOR UPDATE', (batch_id,))
        batch = cur.fetchone()
        if not batch or batch['account_code'] != initial['account_code']:
            raise ValueError('TradingView publication account changed')
        if batch['institution'] != 'tradingview' or batch['status'] not in {'draft', 'review', 'published'}:
            raise ValueError('TradingView source batch is missing or unavailable')
        replay = batch['status'] == 'published'
        cur.execute('SELECT * FROM public.source_files WHERE batch_id=%s ORDER BY file_id FOR SHARE', (batch_id,))
        files = cur.fetchall()
        parsed = _prepare([(f['filename'], bytes(f['content'])) for f in files])
        expected = {(digest, f'{first}-{last}'): (first, last, row_hash, raw)
                    for _, _, digest, rows in parsed for first, last, row_hash, raw in rows}
        cur.execute('SELECT * FROM public.source_items WHERE batch_id=%s ORDER BY source_uid FOR UPDATE', (batch_id,))
        items = cur.fetchall()
        if not items or len(items) != len(expected):
            raise ValueError('TradingView source rows are incomplete')
        if any(item['review_status'] != 'confirmed' or not str(item['review_note'] or '').strip() for item in items):
            raise ValueError('TradingView source rows require individual review')
        file_by_id = {str(f['file_id']): f for f in files}
        records = []
        for item in items:
            file = file_by_id.get(str(item['file_id']))
            if file is None:
                raise ValueError('TradingView source file is missing')
            key = (file['sha256'], item['location'])
            value = expected.pop(key, None)
            raw_evidence = item['raw_fields']
            if not value:
                raise ValueError('TradingView source evidence changed')
            first, last, row_hash, raw = value
            evidence = {'file_sha256': file['sha256'], 'line_start': first, 'line_end': last,
                        'legacy_six_field_hash': row_hash, 'raw_fields': raw}
            encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            uid = f"{file['sha256']}:{first}:{last}"
            if raw_evidence != evidence or item['record_hash'] != sha256(encoded).hexdigest() or item['source_uid'] != uid:
                raise ValueError('TradingView source evidence changed')
            fact = build_investment_trade_fact(source_row_from_item(batch, item, file, raw))
            if replay:
                cur.execute('SELECT source_item_id FROM public.pams_investment_trade_fact WHERE source_table=%s AND source_id=%s',
                            (fact['source_table'], fact['source_id']))
                found = cur.fetchone()
                if not found or str(found['source_item_id']) != str(item['source_item_id']):
                    raise ValueError('TradingView published fact link changed')
            insert_investment_trade_facts(cur, [fact], strict_source=True)
            if not replay:
                cur.execute('''UPDATE public.pams_investment_trade_fact SET source_item_id=%s
                    WHERE source_table=%s AND source_id=%s AND source_item_id IS NULL RETURNING trade_id''',
                    (str(item['source_item_id']), fact['source_table'], fact['source_id']))
                linked = cur.fetchall()
                if len(linked) != 1:
                    raise ValueError('TradingView fact has a conflicting source association')
                trade_id = linked[0]['trade_id']
            else:
                cur.execute('SELECT trade_id FROM public.pams_investment_trade_fact WHERE source_table=%s AND source_id=%s',
                            (fact['source_table'], fact['source_id']))
                trade_id = cur.fetchone()['trade_id']
            records.append({'table': 'pams_investment_trade_fact', 'id': trade_id, 'row_id': item['source_item_id']})
        if expected:
            raise ValueError('TradingView source rows are incomplete')
        check_all_source_links(cur, batch_id, records)
        if not replay:
            cur.execute('SELECT revision FROM public.source_batches WHERE batch_id=%s', (batch_id,))
            revision = cur.fetchone()['revision']
            locked = lock_source_review(cur, batch_id=UUID(batch_id), expected_revision=revision)
            if locked['snapshot']['blockers']:
                raise ValueError('TradingView review incomplete')
            cur.execute("UPDATE public.source_batches SET status='published',published_source_hash=%s,updated_at=now() WHERE batch_id=%s",
                        (locked['source_snapshot_sha256'], batch_id))
        cur.execute('RELEASE SAVEPOINT tradingview_source_publish')
        return {'batch_id': batch_id, 'published_count': len(records), 'replayed': replay}
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT tradingview_source_publish')
        cur.execute('RELEASE SAVEPOINT tradingview_source_publish')
        raise
