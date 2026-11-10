"""Import historical IBKR facts from an already published Flex archive."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from uuid import uuid4

from domain.brokers.ibkr.ibkr_cash_event_sync import _cash_scope_ok
from domain.brokers.ibkr.ibkr_cash_events import _business_date, normalize_cash_event
from domain.brokers.ibkr.ibkr_daily_close import _day, _document, _rows
from domain.brokers.ibkr.ibkr_report_trade_source import _row
from domain.imports import isolation, native_source, source_records_v2
from domain.imports.source_original_read import SourceOriginals

SOURCE = 'ibkr_historical_flex'
DAILY_FACTS_SOURCE = 'ibkr_daily_facts'
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


def read_candidates(content, account, digest, as_of, *, required_from=None):
    evidence, _, error = _document(content.decode('utf8'), account, digest)
    if error or max(evidence.by_day) != as_of:
        raise ValueError('archive does not prove the approved account and closing date')
    if required_from is not None:
        from domain.brokers.ibkr.ibkr_report_state import _calendar

        sessions = _calendar().sessions_in_range(required_from.isoformat(), as_of.isoformat())
        if any(session.date() not in evidence.by_day for session in sessions):
            raise ValueError('archive does not cover all new sessions')
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


@lru_cache(maxsize=4)
def _parse_verified_candidates(content, account, digest, as_of):
    return read_candidates(content, account, digest, as_of)


def read_verified_candidates(content, account, digest, as_of):
    """Parse bytes already checked against the archived SHA-256."""
    parser = _parse_verified_candidates if len(content) <= 8 * 1024 * 1024 else read_candidates
    return parser(content, account, digest, as_of)


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


def _contract(origin):
    official = origin['official_attributes']
    return official.get('conid'), official.get('listingExchange') or official.get('exchange')


def reconcile_candidates(cur, candidates, account, *, current_digest, originals=None):
    """Match official IDs to unchanged facts and their own verified originals."""
    originals = originals or SourceOriginals(cur)
    archives = {current_digest: candidates}
    matched = {kind: set() for kind in FACT_TABLES}
    pending = []
    for kind, table in FACT_TABLES.items():
        rows = candidates[kind]
        if not rows:
            continue
        cur.execute(f"""SELECT f.*,i.source_uid,i.raw_fields,i.parsed_fields,i.review_status,
            r.source_id AS original_source_id,r.account_code AS source_account,
            r.content IS NOT NULL AS inline_original,r.sha256,r.period_end,
            r.source_name,r.status AS source_status
            FROM public.{table} f JOIN public.source_items i USING(source_item_id)
            JOIN public.source_records r USING(source_id)
            WHERE f.account_code='ibkr' AND i.source_uid=ANY(%s::text[])""", (list(rows),))
        seen = set()
        for row in cur.fetchall():
            uid = row['source_uid']
            fields, origin, position = rows[uid]
            digest = row['sha256']
            previous = None
            if row['source_account'] == 'ibkr' and _existing_matches(row, fields, kind):
                if digest not in archives:
                    content, verified = originals.read(row)
                    try:
                        archives[digest] = (read_verified_candidates(bytes(content), account, digest, row['period_end'])[0]
                                            if verified else None)
                    except (ValueError, ArithmeticError, TypeError):
                        archives[digest] = None
                if archives[digest] is not None:
                    previous = archives[digest][kind].get(uid)
            if (uid in seen or not previous or previous[0] != fields or previous[1] != row['raw_fields']
                    or _contract(previous[1]) != _contract(origin)):
                pending.append({'kind': kind, **position, 'reason': 'existing fact conflicts with official identity'})
                matched[kind].discard(uid)
            else:
                matched[kind].add(uid)
            seen.add(uid)
    return matched, pending


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
    matched, conflicts = reconcile_candidates(cur, rows_by_kind, account, current_digest=digest)
    pending.extend(conflicts)
    for kind, rows in rows_by_kind.items():
        duplicates = matched[kind]
        for uid in duplicates:
            del rows[uid]
        result[kind] = {'ready': len(rows), 'duplicate': len(duplicates), 'published': 0}
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


def daily_facts_through(cur):
    cur.execute("""SELECT MAX(period_end) AS through FROM public.source_records
        WHERE account_code='ibkr' AND status='published'
          AND ((source_name=%s AND document_type='statement')
            OR (source_name=%s AND document_type IN ('trade','cash')))""", (DAILY_FACTS_SOURCE, SOURCE))
    return cur.fetchone()['through']


def publish_daily_facts(cur, *, content, account, digest, as_of):
    """Process the report under the same account lock as its securities snapshot."""
    cur.execute("""SELECT MIN(period_start) AS start, MAX(period_end) AS through FROM public.source_records
        WHERE account_code='ibkr' AND source_name=%s AND document_type='statement' AND status='published'""",
                (DAILY_FACTS_SOURCE,))
    progress = cur.fetchone()
    if progress['through'] and progress['through'] >= as_of:
        return {'status': 'published', 'through': progress['through'].isoformat(), 'replayed': True}

    baseline = progress['through'] or daily_facts_through(cur)
    if baseline is not None and baseline >= as_of:
        return {'status': 'published', 'through': baseline.isoformat(), 'replayed': True}
    required_from = baseline + timedelta(days=1) if baseline else as_of
    # On first activation, only automate new activity after the approved history.
    start = progress['start'] or required_from
    try:
        candidates, pending, _ = read_candidates(content, account, digest, as_of, required_from=required_from)
    except (ValueError, ArithmeticError, KeyError):
        return {'status': 'blocked', 'reason': 'flex_fact_scope_unverified'}
    if pending:
        return {'status': 'blocked', 'reason': 'flex_fact_rows_unverified', 'pending': len(pending)}

    eligible = {kind: {uid: entry for uid, entry in rows.items()
                       if date.fromisoformat(entry[2]['statement_day']) >= start}
                for kind, rows in candidates.items()}
    matched, conflicts = reconcile_candidates(cur, candidates, account, current_digest=digest)
    if conflicts:
        return {'status': 'blocked', 'reason': 'flex_fact_identity_conflict', 'pending': len(conflicts)}

    result = {'status': 'published', 'through': as_of.isoformat(), 'replayed': False}
    for kind, rows in eligible.items():
        ready = {uid: row for uid, row in rows.items() if uid not in matched[kind]}
        count = _publish(cur, kind, ready, content,
                         min(row[0]['business_date'] for row in ready.values()), as_of) if ready else 0
        result[kind] = {'published': count, 'duplicate': len(rows.keys() & matched[kind])}
    source_id = uuid4()
    source_records_v2.create_source(cur, source_id=source_id, account_code='ibkr', source_name=DAILY_FACTS_SOURCE,
                                    acquisition_method='api', document_type='statement', filename='ibkr-flex.xml',
                                    file_format='xml', content_type='application/xml', content=content)
    cur.execute("""UPDATE public.source_records SET period_start=%s,period_end=%s,status='published',revision=revision+1
        WHERE source_id=%s""", (start, as_of, str(source_id)))
    return result
