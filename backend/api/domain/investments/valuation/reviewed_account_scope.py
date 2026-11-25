"""Reconcile a dated, user-attested full account against its retained components."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from domain.investments.holdings.snapshot_coverage import load_account_snapshot_coverage
from domain.investments.valuation.valuation_input_evidence import _bound_input, _hash
from domain.investments.valuation.wealth_valuation import load_reviewed_wealth_anchors


def _amount(value):
    if not value.is_finite():
        raise ValueError('invalid scope amount')
    return format(value.normalize(), 'f')


def verified_members(cur, account, day, currency, basis):
    """Return the actual member identities, amounts and source references in stable order."""
    cur.execute("""SELECT id,symbol,asset_id,source_item_id,currency,asset_liability,market_value,quantity,current_price
        FROM pams_holdings_snapshot WHERE account_code=%s AND as_of=%s ORDER BY id""", (account,day))
    holdings = cur.fetchall()
    if basis == 'broker_ledger':
        if holdings:
            # A separately recorded snapshot needs reconciliation against the ledger first.
            raise ValueError('scope overlaps additional dated holdings')
        return _broker_members(cur, account, day, currency)
    if basis != 'dated_positions' or not holdings:
        raise ValueError('missing dated account members')
    coverage = load_account_snapshot_coverage(cur, {(account,day)})[account,day]
    if coverage['member_ids'] != {row['id'] for row in holdings}:
        raise ValueError('dated holdings are not fully supported by their sources')
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly")
    locking = not cur.fetchone()['readonly']
    members = {}
    for row in holdings:
        if (row['currency'] != currency or row['asset_liability'] != '资产' or not row['source_item_id']
                or not row['symbol'] or row['symbol'] in members
                or any(v is None or not v.is_finite() or v <= 0 for v in
                       (row['quantity'],row['current_price'],row['market_value']))
                or abs(row['quantity'] * row['current_price'] - row['market_value']) > Decimal('0.005')):
            raise ValueError('ambiguous account member')
        key = str(row['source_item_id'])
        source, _ = _bound_input(cur, 'source_item', {'source_item_id':key}, locking=locking)
        snap = source['snapshot'] or {}
        fields = snap.get('source_fields') or {}
        if (snap.get('source_integrity') != 'verified' or snap.get('review_status') != 'confirmed'
                or snap.get('batch_status') != 'published' or snap.get('business_type') != 'holding'
                or source['source_ref'].get('account_code') != account
                or fields.get('business_date') != day.isoformat() or fields.get('currency') != currency
                or fields.get('symbol') != row['symbol']
                or any(Decimal(str(fields.get(field))) != row[col] for field,col in
                       [('quantity','quantity'), ('price','current_price'), ('market_value','market_value')])):
            raise ValueError('holding and original source differ')
        members[row['symbol']] = {'quantity':_amount(row['quantity']), 'price':_amount(row['current_price']),
            'value':_amount(row['market_value']), 'holding_id':row['id'], 'item_id':key,
            'source_hash':snap['content_hash']}
    for row in load_reviewed_wealth_anchors(cur, accounts=[account], dates=[day]):
        symbol = row['symbol']
        if (not symbol or symbol in members or row['date'] != row['price_date'] or not row['direct_unit_mark']
                or row['currency'] != currency or not row['source_refs']):
            raise ValueError('wealth position needs a unique directly observed unit mark')
        members[symbol] = {'quantity':_amount(row['quantity']), 'price':_amount(row['price']),
            'value':_amount(row['amount']), 'source_refs':row['source_refs']}
    return {'positions':members, 'cash':'0', 'snapshot_version':coverage['version_id']}


def _broker_members(cur, account, day, currency):
    from domain.investments.trades.nav_trade_source_v2 import (
        SOURCE, _read_event_prefix, daily_estimates, securities_by_symbol,
    )
    events, gap = _read_event_prefix(cur, account)
    events = [e for e in events if e['trade_date'] <= day]
    if (not events or currency != 'CNY' or (gap and gap <= day)
            or any(e['account_code'] != account or e['currency'] != currency for e in events)):
        raise ValueError('unreconciled brokerage source')
    symbols = securities_by_symbol(events)
    if not symbols:
        raise ValueError('missing brokerage security identity')
    quantity = defaultdict(Decimal)
    for e in events:
        if e['trade_type'] in {'buy','sell'}:
            quantity[e['symbol']] += e['quantity'] * (1 if e['trade_type'] == 'buy' else -1)
    cur.execute("""SELECT quote_id,symbol,close_price FROM market_observations
        WHERE source=%s AND market='CN_A' AND observation_type='daily' AND currency=%s
          AND as_of=%s AND symbol=ANY(%s) ORDER BY quote_id""", (SOURCE,currency,day,list(symbols.values())))
    prices, members = {}, {}
    for row in cur.fetchall():
        if (row['symbol'] in prices or row['close_price'] is None
                or not row['close_price'].is_finite() or row['close_price'] <= 0):
            raise ValueError('missing or ambiguous daily quote')
        prices[row['symbol']] = (row['close_price'],row['quote_id'])
    for symbol, qty in quantity.items():
        if qty:
            if qty < 0 or symbols[symbol] not in prices:
                raise ValueError('missing daily quote or separate liability')
            mark, quote_id = prices[symbols[symbol]]
            members[symbol] = {'quantity':_amount(qty), 'price':_amount(mark),
                               'value':_amount(mark*qty), 'quote_id':quote_id}
    marked = daily_estimates(events,{k:{day:v[0]} for k,v in prices.items()},[day],symbols)
    if day not in marked or not members:
        raise ValueError('brokerage closing balance unproven')
    cash = marked[day] - sum(Decimal(row['value']) for row in members.values())
    if cash < 0:
        raise ValueError('separate financing evidence required')
    return {'positions':members,'cash':_amount(cash),
            'original_digests':sorted({e['source_digest'] for e in events}),
            'trade_ids':sorted(e['trade_id'] for e in events)}


def verify_scope(cur, value, fields):
    day = date.fromisoformat(value['business_date'])
    if (fields['value_scope'] != 'account_total' or value['covered_account_codes'] != [value['account_code']]
            or fields.get('complete_assets_and_debts') is not True):
        raise ValueError('账户完整范围缺少当日确认')
    actual = verified_members(cur,value['account_code'],day,value['currency'],fields['member_basis'])
    if actual != fields.get('confirmed_members'):
        raise ValueError('账户完整范围与已核验原件不符')
    gross = Decimal(actual['cash']) + sum(Decimal(m['value']) for m in actual['positions'].values())
    if gross != Decimal(value['net_asset']):
        raise ValueError('年末明细合计与确认总额不符')
    value['confirmed_components'] = _hash(actual)


def record(cur, *, account, day, currency, basis, members, net_asset, note):
    """Publish or replay one dated owner confirmation without creating ledger entries."""
    from domain.imports import source_records_v2 as sources
    from domain.imports.isolation import source_enabled
    from domain.investments.valuation import account_total_evidence as totals
    if cur.connection.autocommit or not source_enabled(cur) or not isinstance(note,str) or not note.strip():
        raise ValueError('完整账户确认需要受保护事务及核对说明')
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:'+account,))
    identity = uuid5(NAMESPACE_URL,f'pams:reviewed-account-scope/{day}/v1/{account}')
    item = uuid5(identity,'reviewed-scope')
    fields = {'kind':'account_portfolio_value', 'value_scope':'account_total', 'business_date':day.isoformat(),
              'currency':currency, 'net_asset':str(net_asset), 'covered_account_codes':[account],
              'attestation':'complete_reconstructed_account', 'complete_assets_and_debts':True,
              'member_basis':basis, 'confirmed_members':members}
    cur.execute("""SELECT i.parsed_fields,i.review_note,r.status FROM source_records r
        LEFT JOIN source_items i USING(source_id) WHERE r.source_id=%s""", (str(identity),))
    existing = cur.fetchall()
    if existing:
        if (len(existing) != 1 or existing[0]['parsed_fields'] != fields or existing[0]['review_note'] != note
                or existing[0]['status'] != 'published'):
            raise ValueError('已有当日确认与此次不一致')
    else:
        sources.create_source(cur,source_id=identity,account_code=account,source_name='本人核准完整账户范围',
                              acquisition_method='manual',document_type='statement')
        sources.add_manual_item(cur,source_id=identity,item_id=item,expected_revision=1,parsed_fields=fields)
        sources.review_item(cur,source_id=identity,item_id=item,expected_revision=2,parsed_fields=fields,
                            review_status='confirmed',note=note)
        totals.publish(cur,source_id=identity,revision=3)
    observation = totals.load(cur,dates=[day],accounts={account}).get((day,account))
    if not observation or observation.get('confirmed_components') != _hash(members):
        raise ValueError('完整账户确认与已有证据冲突或无法核验')
    return observation
