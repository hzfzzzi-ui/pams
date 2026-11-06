"""Import historical IBKR facts from an already published Flex archive."""
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from domain.brokers.ibkr.ibkr_cash_event_sync import _cash_scope_ok
from domain.brokers.ibkr.ibkr_cash_events import _business_date, normalize_cash_event
from domain.brokers.ibkr.ibkr_daily_close import _day, _document, _rows
from domain.brokers.ibkr.ibkr_report_trade_source import _row
from domain.imports import isolation, native_source, source_records_v2

SOURCE = 'ibkr_historical_flex'
FACT_TABLES = {'trade': 'pams_investment_trade_fact', 'cash': 'pams_cashflow_fact'}


def _trade(raw, account, statement_day, digest):
    day = _day(raw.get('tradeDate'))
    if day != statement_day or raw.get('levelOfDetail') != 'EXECUTION':
        raise ValueError('trade date or execution scope is unverified')
    trade = _row(None, {'raw': raw}, account, day, digest, include_fx=False, historical=True)
    if trade['tax_amount'] is None or trade['explicit_fee_currency'] != trade['currency']:
        raise ValueError('trade tax or fee currency is unverified')
    fields = {key: trade[key] for key in ('symbol', 'name', 'market', 'currency')}
    fields.update({key: str(trade[key]) for key in ('price', 'gross_amount', 'fee_amount', 'tax_amount', 'net_cash')})
    side = trade['side'].lower()
    fields.update(business_date=day.isoformat(), broker='ibkr', trade_type=side, side=side,
                  quantity=str(abs(trade['quantity'])), fee_currency=trade['explicit_fee_currency'])
    if raw.get('dateTime'):
        stamp = datetime.strptime(raw['dateTime'], '%Y%m%d;%H%M%S')
        # The common writer combines this field with the business date.
        # Preserve a timestamp on a different date in the original attributes.
        if stamp.date() == day:
            fields['trade_time'] = stamp.time().isoformat()
    if raw.get('settleDateTarget'):
        fields['settle_date'] = _day(raw['settleDateTarget']).isoformat()
    return trade['source_key'], fields


def _cash(raw, account, statement_day):
    if _business_date(raw.get('reportDate', '')) != statement_day:
        raise ValueError('cash report date is unverified')
    attributes = dict(raw)
    if attributes.get('type') == 'Deposits/Withdrawals':
        attributes['type'] = 'Deposits & Withdrawals'
    cash = normalize_cash_event({'raw': attributes, 'statement_account_id': account},
                                account_code='ibkr', cutoff=date.min, statement_as_of=statement_day)
    day = cash['trade_date']
    if day != statement_day and (cash['side'] not in {'deposit', 'withdraw'} or cash['settle_date'] != day):
        raise ValueError('cash business date differs from report date without settlement evidence')
    amount = cash['net_cash']
    if abs(amount) >= Decimal('1e16') or amount.quantize(Decimal('0.0001')) != amount:
        raise ValueError('cash amount exceeds fact precision')
    fields = {'business_date': day.isoformat(), 'action': cash['side'], 'currency': cash['currency'],
              'net_cash': format(amount.normalize(), 'f'), 'summary': raw['type'],
              'description': raw.get('description') or None}
    return cash['source_key'], fields


def read_candidates(content, account, digest, as_of):
    evidence, _, error = _document(content.decode('utf8'), account, digest)
    if error or max(evidence.by_day) != as_of:
        raise ValueError('archive does not prove the approved account and closing date')
    candidates = {kind: {} for kind in FACT_TABLES}
    pending, excluded = [], 0
    for day, statements in sorted(evidence.by_day.items()):
        if len(statements) != 1 or _day(statements[0].get('fromDate')) != day:
            raise ValueError('archive requires unique single-day statements')
        statement = statements[0]
        for kind, section_tag, row_tag, normalize in (
                ('trade', 'Trades', 'Trade', _trade), ('cash', 'CashTransactions', 'CashTransaction', _cash)):
            sections = _rows(statement, section_tag)
            nodes = _rows(statement, row_tag)
            if (len(sections) != 1 or not _cash_scope_ok(sections[0], evidence.parents, account)
                    or _rows(sections[0], row_tag) != nodes):
                raise ValueError('archive section is missing or ambiguous')
            for ordinal, node in enumerate(nodes, 1):
                raw = {key: value.strip() for key, value in node.attrib.items()}
                position = {'statement_day': day.isoformat(), 'ordinal': ordinal}
                try:
                    if not _cash_scope_ok(node, evidence.parents, account):
                        raise ValueError('account or model scope conflicts')
                    if kind == 'trade' and raw.get('assetCategory') == 'CASH':
                        excluded += 1
                        continue
                    uid, fields = normalize(raw, account, day, digest) if kind == 'trade' else normalize(raw, account, day)
                    if uid in candidates[kind]:
                        raise ValueError('duplicate official ID inside archive')
                    origin = {'provider': 'ibkr_flex', 'source_account_id': account,
                              'report_sha256': digest, 'official_attributes': raw}
                    candidates[kind][uid] = (fields, origin, position)
                except (ValueError, ArithmeticError, KeyError) as exc:
                    pending.append({'kind': kind, **position, 'reason': str(exc)})
    return candidates, pending, excluded


def _existing_matches(row, fields, kind):
    if row['source_name'] != SOURCE or row['source_status'] != 'published' or row['review_status'] != 'confirmed':
        return False
    if row['parsed_fields'] != fields:
        return False
    if kind == 'cash':
        return (row['event_date'].isoformat() == fields['business_date'] and row['currency'] == fields['currency']
                and row['amount'] == Decimal(fields['net_cash']) and row['summary'] == fields['summary']
                and row['description'] == fields['description'] and row['posting_date'] is None
                and row['counterparty'] is None)
    from domain.investments.trade_read_v2 import _matches
    return _matches(row)


def _publish(cur, kind, rows, content, start, end):
    source_id = uuid4()
    source_records_v2.create_source(cur, source_id=source_id, account_code='ibkr', source_name=SOURCE,
                                    acquisition_method='api', document_type=kind, filename='ibkr-flex.xml',
                                    file_format='xml', content_type='application/xml', content=content)
    for uid, (fields, origin, position) in rows.items():
        source_records_v2.add_item(cur, source_id=source_id, item_id=uuid4(), parse_method='api',
                                   source_uid=uid, raw_fields=origin, parsed_fields=fields, source_position=position)
    cur.execute("""UPDATE source_items SET review_status='confirmed',
        review_note='Verified against archived IBKR Flex evidence' WHERE source_id=%s""", (str(source_id),))
    cur.execute("""UPDATE source_records SET period_start=%s,period_end=%s WHERE source_id=%s RETURNING revision""",
                (start, end, str(source_id)))
    revision = cur.fetchone()['revision']
    if kind == 'cash':
        return source_records_v2.publish_cash_source(cur, source_id=source_id, expected_revision=revision,
                                                     verified_ibkr_ids=True)['published']
    return source_records_v2.publish_trade_source(cur, source_id=source_id, expected_revision=revision)['published']


def backfill(cur, *, digest, account, as_of, apply=False, approved_counts=None):
    """Plan or publish one archive, guarded by the target database and approved scope."""
    mode = isolation.source_mode()
    if (cur.connection.autocommit or mode not in ('test', None)
            or (mode is None and approved_counts is None) or not isolation.source_enabled(cur)):
        raise ValueError('historical Flex backfill requires a designated transaction')
    if mode is None and apply and not native_source.enabled():
        raise ValueError('source publication is disabled')
    if approved_counts is not None and (set(approved_counts) != set(FACT_TABLES)
            or any(type(count) is not int or count < 0 for count in approved_counts.values())):
        raise ValueError('invalid approved fact counts')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('account-import:ibkr',0))")
    cur.execute("""SELECT o.content FROM source_records r JOIN ibkr_source_originals o USING(sha256)
        WHERE r.account_code='ibkr' AND r.source_name='ibkr_daily_holdings' AND r.document_type='holding'
          AND r.status='published' AND r.sha256=%s AND r.period_end=%s""", (digest, as_of))
    archives = cur.fetchall()
    if len(archives) != 1:
        raise ValueError('expected one matching published IBKR archive')
    content = bytes(archives[0]['content'])
    rows_by_kind, pending, excluded = read_candidates(content, account, digest, as_of)
    result = {'as_of': as_of.isoformat(), 'sha256': digest, 'pending': pending, 'excluded_fx_trades': excluded}
    for kind, rows in rows_by_kind.items():
        cur.execute(f"""SELECT f.*,i.source_uid,i.parsed_fields,i.review_status,
                r.source_name,r.status AS source_status
            FROM {FACT_TABLES[kind]} f JOIN source_items i USING(source_item_id)
            JOIN source_records r USING(source_id) WHERE f.account_code='ibkr' AND i.source_uid=ANY(%s::text[])""",
                    (list(rows),))
        matched = set()
        duplicates = 0
        for row in cur.fetchall():
            uid = row['source_uid']
            fields, _, position = rows[uid]
            if uid in matched or not _existing_matches(row, fields, kind):
                pending.append({'kind': kind, **position, 'reason': 'existing fact conflicts with official identity'})
            else:
                duplicates += 1
            matched.add(uid)
        for uid in matched:
            del rows[uid]
        result[kind] = {'ready': len(rows), 'duplicate': duplicates, 'published': 0}
    if approved_counts is not None:
        ready = {kind: result[kind]['ready'] for kind in FACT_TABLES}
        duplicates = {kind: result[kind]['duplicate'] for kind in FACT_TABLES}
        empty = dict.fromkeys(FACT_TABLES, 0)
        totals = {}
        for kind, table in FACT_TABLES.items():
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE account_code='ibkr'")
            totals[kind] = cur.fetchone()['n']
        if (ready, duplicates, totals) not in ((approved_counts, empty, empty),
                                               (empty, approved_counts, approved_counts)):
            raise ValueError('fact counts differ from approved first run or completed replay')
    if apply and pending:
        raise ValueError('backfill has unresolved source or fact conflicts; preview required')
    if apply:
        for kind, rows in rows_by_kind.items():
            if rows:
                start = min(fields['business_date'] for fields, _, _ in rows.values())
                result[kind]['published'] = _publish(cur, kind, rows, content, start, as_of)
    return result
