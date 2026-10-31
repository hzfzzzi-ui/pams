"""Confirmed TradingView source publication for disposable databases only."""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID

from psycopg2.extras import Json

from collectors.tradingview_portfolio import _parse_time, _to_decimal, normalize_symbol
from domain.imports import isolation
from domain.imports.tradingview_source_candidate import _prepare
from domain.imports.unified_source import lock_source_review, review_item
from domain.imports.source_import_adapter import _check_all_source_links
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts


def _values(raw):
    side = raw['Side'].strip()
    quantity = _to_decimal(raw['Qty'], default=None)
    price = _to_decimal(raw['Fill Price'], default=None)
    fee = _to_decimal(raw['Commission'], default=Decimal('0'))
    if (quantity is None or not quantity.is_finite() or quantity <= 0 or not fee.is_finite() or fee < 0 or
        any(value is not None and value != value.quantize(Decimal('0.000001')) for value in (quantity, price, fee)) or
        (side in {'Buy', 'Sell'} and (not normalize_symbol(raw['Symbol']) or price is None or price <= 0))):
        raise ValueError('TradingView amounts or symbol need explicit correction')
    return side, quantity, price, fee, _parse_time(raw['Closing Time'])


def _source_row(cur, transaction_id):
    cur.execute('''SELECT 'pams_tradingview_transactions'::text AS source_table, tv.id::text AS source_id,
        tv.row_hash AS source_key, tv.closing_time::date AS trade_date, tv.closing_time AS trade_time,
        NULL::date AS settle_date, tv.account_code, 'tradingview'::text AS broker,
        tv.portfolio_name AS portfolio_code, NULL::text AS asset_id,
        COALESCE(tv.normalized_symbol,tv.symbol) AS symbol, tv.symbol AS name, 'CN_A'::text AS market,
        tv.currency,tv.side,tv.quantity,tv.fill_price AS price,
        CASE WHEN LOWER(tv.side) IN ('deposit','withdraw','dividend') THEN ABS(COALESCE(tv.quantity,0))
             ELSE ABS(COALESCE(tv.quantity,0)*COALESCE(tv.fill_price,0)) END AS gross_amount,
        ABS(COALESCE(tv.commission,0)) AS fee_amount,0::numeric AS tax_amount,NULL::numeric AS net_cash,
        1::numeric AS fx_rate_to_cny,NULL::numeric AS realized_pnl,tv.raw AS raw_json
        FROM public.pams_tradingview_transactions tv WHERE tv.id=%s''', (transaction_id,))
    result = cur.fetchone()
    if result is None:
        raise ValueError('TradingView transaction missing')
    return dict(result)


def _transaction(cur, batch, item, file, raw, *, allow_existing, allow_create):
    side, quantity, price, fee, closing = _values(raw)
    row_hash = item['raw_fields']['legacy_six_field_hash']
    cur.execute('SELECT * FROM public.pams_tradingview_transactions WHERE row_hash=%s FOR UPDATE', (row_hash,))
    previous = cur.fetchone()
    if previous is None:
        if not allow_create:
            raise ValueError('published TradingView transaction is missing')
        cur.execute('''INSERT INTO public.pams_tradingview_transactions
            (account_code,portfolio_name,source_file,source_hash,row_hash,symbol,normalized_symbol,side,
             quantity,fill_price,commission,currency,closing_time,raw)
            VALUES (%s,'A股',%s,%s,%s,%s,%s,%s,%s,%s,%s,'CNY',%s,%s)
            ON CONFLICT (row_hash) DO NOTHING RETURNING id''',
            (batch['account_code'], file['filename'], file['sha256'], row_hash, raw['Symbol'].strip(),
             normalize_symbol(raw['Symbol']), side, quantity, price, fee, closing, Json(raw)))
        inserted = cur.fetchone()
        if inserted is not None:
            return inserted['id'], True
        cur.execute('SELECT * FROM public.pams_tradingview_transactions WHERE row_hash=%s FOR UPDATE', (row_hash,))
        previous = cur.fetchone()
    expected = (batch['account_code'], file['sha256'], raw, 'A股', raw['Symbol'].strip(),
                normalize_symbol(raw['Symbol']), side, quantity, price, fee, 'CNY', closing)
    actual = tuple(previous[k] for k in ('account_code', 'source_hash', 'raw', 'portfolio_name', 'symbol',
                                          'normalized_symbol', 'side', 'quantity', 'fill_price', 'commission',
                                          'currency', 'closing_time')) if previous else None
    if not allow_existing or actual != expected:
        raise ValueError('existing TradingView transaction lacks matching confirmed file evidence')
    return previous['id'], False


def publish(cur, *, batch_id, confirm=False, allow_existing=False):
    """Publish explicitly confirmed records, or verify a published replay."""
    if not isolation.source_enabled(cur):
        raise isolation.IsolationError('TradingView publication requires isolated mode')
    if cur.connection.autocommit or confirm is not True:
        raise ValueError('TradingView publication requires a transaction and explicit confirmation')
    batch_id = str(UUID(str(batch_id)))
    cur.execute('SAVEPOINT tradingview_source_publish')
    try:
        cur.execute('SELECT * FROM public.source_batches WHERE batch_id=%s FOR UPDATE', (batch_id,))
        batch = cur.fetchone()
        if not batch or batch['institution'] != 'tradingview' or batch['status'] not in {'draft', 'review', 'published'}:
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
        file_by_id = {str(f['file_id']): f for f in files}
        records = []
        for item in items:
            file = file_by_id.get(str(item['file_id']))
            key = (file['sha256'], item['location']) if file else None
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
            identifier, _ = _transaction(cur, batch, item, file, raw, allow_existing=allow_existing or replay,
                                         allow_create=not replay)
            fact = build_investment_trade_fact(_source_row(cur, identifier))
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
        _check_all_source_links(cur, batch_id, records)
        if not replay:
            cur.execute('SELECT revision FROM public.source_batches WHERE batch_id=%s', (batch_id,))
            revision = cur.fetchone()['revision']
            for item in items:
                revision = review_item(cur, batch_id=UUID(batch_id), item_id=item['source_item_id'], expected_revision=revision,
                                       parsed_fields=item['parsed_fields'], review_status='confirmed', review_note='explicit CLI confirmation')
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
