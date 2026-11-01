"""Prepare, but never persist, trade-derived securities snapshot candidates."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.investments.diluted_trade_positions import calculate_diluted_positions


def build_trade_snapshot_candidates(*, events, identities, quotes, as_of, closed_adjustments=None):
    """Require verified inputs; snapshots cover securities only, not account cash.

    Quote validity for the market session remains a separate caller decision.
    Values here are review candidates, not published holdings or official costs.
    """
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise ValueError('snapshot business date is required')
    if not events or any(row.get('source_verified') is not True for row in events):
        raise ValueError('every event requires verified source evidence')
    if any(date.fromisoformat(str(row['trade_date'])[:10]) > as_of for row in events):
        raise ValueError('trade is later than snapshot')
    separate = [] if closed_adjustments is not None else None
    positions = calculate_diluted_positions(events, closed_adjustments=separate)
    candidates = []
    for position in positions:
        symbol = position['symbol']
        identity = identities.get(symbol)
        quote = quotes.get(symbol)
        if not identity or not identity.get('asset_id') or not identity.get('name') or identity.get('symbol') != symbol:
            raise ValueError(f'asset identity unavailable: {symbol}')
        stamp = quote.get('price_time') if quote else None
        if (not quote or quote.get('price_date') != as_of or not isinstance(stamp, datetime)
                or stamp.utcoffset() is None or stamp.astimezone(ZoneInfo('Asia/Shanghai')).date() != as_of
                or not quote.get('source') or str(quote.get('symbol') or '')[2:] != symbol
                or not isinstance(quote.get('price'), Decimal) or not quote['price'].is_finite()
                or quote['price'] <= 0):
            raise ValueError(f'same-date price evidence unavailable: {symbol}')
        amount = position['quantity'] * quote['price']
        if amount != amount.quantize(Decimal('0.0001')):
            raise ValueError(f'market value exceeds storage precision: {symbol}')
        cost = position['diluted_cost']
        if cost != cost.quantize(Decimal('0.0001')):
            raise ValueError(f'cost exceeds storage precision: {symbol}')
        candidates.append({**position, 'asset_id': identity['asset_id'], 'name': identity['name'],
                           'as_of': as_of, 'price': quote['price'], 'price_time': quote['price_time'],
                           'price_source': quote.get('source'), 'market_value': amount,
                           'unrealized_pnl': amount - position['remaining_cost'],
                           'total_pnl': amount - position['remaining_cost'] + position['realized_trade_pnl'] + position['net_distributions'],
                           'total_pnl_status': 'coverage_unverified',
                           'scope': 'securities_only', 'cost_basis': 'weighted_average',
                           'broker_cost_basis': 'diluted_trade_estimate'})
    if closed_adjustments is not None:
        closed_adjustments.extend(separate)
    return candidates
