"""Signed, source-bound preview and atomic publication of trade snapshots."""
from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import time
from zoneinfo import ZoneInfo

from domain.imports.account_import_store import norm
from domain.imports.native_source_http import _token_key
from domain.investments.diluted_trade_positions import calculate_diluted_positions
from domain.investments.holding_evidence_writer import lock_holding_accounts
from domain.investments.trade_snapshot_preview import build_trade_snapshot_candidates
from domain.investments.trade_snapshot_quotes import stage_isolated_quotes
from domain.investments.trade_snapshot_source import load_verified_equity_events
from domain.investments.trade_snapshot_writer import preview as stored_preview, publish_isolated
from domain.investments.valuation_input_evidence import _hash
from domain.market.tencent_stock_quote import fetch_tencent_stock_quotes

_PREFIX = b'pams-trade-snapshot-v1:'
_ALGORITHM = 'weighted-and-diluted-v1'


def _provider_symbol(symbol):
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError('unsupported stock symbol')
    if symbol[0] in '569':
        return 'sh' + symbol
    if symbol[0] in '0123':
        return 'sz' + symbol
    raise ValueError('stock exchange requires explicit mapping')


def _seal(payload):
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
    encoded = base64.urlsafe_b64encode(raw).decode('ascii')
    signature = hmac.new(_token_key(), _PREFIX + encoded.encode('ascii'), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


def _open(token):
    if not isinstance(token, str) or len(token) > 20000 or token.count('.') != 1:
        raise ValueError('invalid preview token')
    encoded, signature = token.split('.')
    wanted = hmac.new(_token_key(), _PREFIX + encoded.encode('ascii'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, wanted):
        raise ValueError('invalid preview token')
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode('ascii'))
        issued = int(payload['issued'])
        if issued > int(time.time()) + 30 or int(time.time()) - issued > 900:
            raise ValueError('preview expired')
        if (payload['account'] != 'tonghuashun' or payload.get('algorithm') != _ALGORITHM
                or not isinstance(payload['quotes'], dict)):
            raise ValueError('invalid preview scope')
        return payload
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError('invalid or expired preview') from exc


def preview(cur, account_code):
    events, identities = load_verified_equity_events(cur, account_code)
    closed = []
    positions = calculate_diluted_positions(events, closed_adjustments=closed)
    symbols = sorted(row['symbol'] for row in positions)
    if not symbols:
        raise ValueError('no open securities')
    provider = {symbol: _provider_symbol(symbol) for symbol in symbols}
    try:
        received = fetch_tencent_stock_quotes(list(provider.values()))
    except OSError as exc:
        raise ValueError('Tencent quotes unavailable') from exc
    if set(received) != set(provider.values()):
        raise ValueError('Tencent quotes incomplete')
    quotes = {symbol: received[provider[symbol]] for symbol in symbols}
    dates = {row['price_date'] for row in quotes.values()}
    if len(dates) != 1:
        raise ValueError('quotes have different business dates')
    day = next(iter(dates))
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if day > now.date() or any(row['price_time'] > now for row in quotes.values()):
        raise ValueError('quote date is in the future')
    separate = []
    candidates = build_trade_snapshot_candidates(events=events, identities=identities, quotes=quotes,
                                                  as_of=day, closed_adjustments=separate)
    payload = {'account': account_code, 'issued': int(time.time()), 'as_of': day.isoformat(), 'algorithm': _ALGORITHM,
               'events_hash': _hash(norm(events)), 'quotes': norm(quotes)}
    return {'as_of': day.isoformat(), 'candidates': norm(candidates), 'closed_adjustments': norm(separate),
            'scope': 'securities_only', 'cash_coverage': 'unknown', 'preview_token': _seal(payload)}


def publish(cur, token, *, confirmed_scope):
    if confirmed_scope is not True:
        raise ValueError('securities coverage confirmation required')
    payload = _open(token)
    account = payload['account']
    try:
        day = date.fromisoformat(payload['as_of'])
        quotes = {symbol: {'symbol': row['symbol'], 'source': row['source'],
                           'price': Decimal(row['price']), 'price_date': date.fromisoformat(row['price_date']),
                           'price_time': datetime.fromisoformat(row['price_time'])}
                  for symbol, row in payload['quotes'].items()}
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError('invalid signed quote data') from exc
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if day > now.date() or any(row['price_time'].utcoffset() is None or row['price_time'] > now for row in quotes.values()):
        raise ValueError('quote date is in the future')
    lock_holding_accounts(cur, [account])
    events, identities = load_verified_equity_events(cur, account, for_write=True)
    if not hmac.compare_digest(payload['events_hash'], _hash(norm(events))):
        raise ValueError('transactions changed; preview again')
    closed = []
    positions = calculate_diluted_positions(events, closed_adjustments=closed)
    symbols = {row['symbol'] for row in positions}
    if set(quotes) != symbols:
        raise ValueError('quote scope changed')
    selected = {symbol: identities[symbol] for symbol in symbols}
    build_trade_snapshot_candidates(events=events, identities=identities, quotes=quotes,
                                    as_of=day, closed_adjustments=[])
    quote_ids = stage_isolated_quotes(cur, as_of=day, identities=selected, quotes=quotes, allow_runtime=True)
    prepared = stored_preview(cur, account_code=account, as_of=day, quote_ids=quote_ids, for_write=True)
    return publish_isolated(cur, account_code=account, as_of=day, quote_ids=quote_ids,
                            expected_digest=prepared['digest'], confirmed_scope=True, allow_runtime=True)
