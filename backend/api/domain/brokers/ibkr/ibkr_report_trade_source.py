"""Normalize and validate trade facts against explicit IBKR report rows."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from domain.brokers.ibkr.ibkr_cash_events import dated_fx_rate
from domain.investments.investment_trade_fact import _is_fx_pair_symbol


def _number(attrs, keys, label):
    values = [str(attrs[key]).strip() for key in keys if key in attrs and str(attrs[key]).strip()]
    if not values:
        raise ValueError('current report trade missing ' + label)
    if any(not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value) for value in values):
        raise ValueError('current report trade has invalid ' + label)
    try:
        nums = [Decimal(value) for value in values]
    except InvalidOperation as exc:
        raise ValueError('current report trade has invalid ' + label) from exc
    if any(not value.is_finite() for value in nums) or len(set(nums)) != 1:
        raise ValueError('current report trade has conflicting ' + label)
    try:
        if any(abs(value) >= Decimal('1e18') or value != value.quantize(Decimal('0.0000000001')) for value in nums):
            raise ValueError('current report trade has unrepresentable ' + label)
    except InvalidOperation as exc:
        raise ValueError('current report trade has unrepresentable ' + label) from exc
    return nums[0]


def _row(cur, trade, account, day, report_sha256):
    raw = trade['raw']
    values = {str(key).lower(): str(value).strip() for key, value in raw.items()}
    if len(values) != len(raw):
        raise ValueError('current report trade has ambiguous attributes')
    official = next(((name, values[name]) for name in ('tradeid', 'transactionid', 'ibexecid') if values.get(name)), None)
    if not official or official[1].lower() in {'0', 'none', 'null', 'unknown'}:
        raise ValueError('current report trade lacks official identity')
    symbol, currency, side = values.get('symbol', ''), values.get('currency', '').upper(), values.get('buysell', '').upper()
    if not symbol or not re.fullmatch(r'[A-Z]{3}', currency) or side not in {'BUY', 'SELL'}:
        raise ValueError('current report trade has unknown security, currency or side')
    is_fx = _is_fx_pair_symbol(symbol)
    if is_fx and values.get('assetcategory', '').upper() != 'CASH':
        raise ValueError('currency-pair trade requires explicit CASH asset category')
    quantity = _number(values, ('quantity',), 'quantity')
    price = _number(values, ('tradeprice',), 'price')
    gross = abs(_number(values, ('trademoney',), 'gross amount'))
    fee = abs(_number(values, ('ibcommission',), 'commission'))
    tax = (abs(_number(values, ('taxamount', 'tax'), 'tax amount'))
           if values.get('taxamount') or values.get('tax') else None)
    net = _number(values, ('netcash',), 'net cash')
    fee_currency = values.get('ibcommissioncurrency', '').upper() or None
    tax_currency = values.get('taxcurrency', '').upper() or None
    if tax is not None and tax != 0 and tax_currency != currency:
        raise ValueError('current report trade has unknown tax currency')
    if fee_currency is not None and not re.fullmatch(r'[A-Z]{3}', fee_currency):
        raise ValueError('current report trade has invalid fee currency')
    if values.get('multiplier'):
        multiplier = _number(values, ('multiplier',), 'multiplier')
    elif values.get('assetcategory', '').upper() == 'STK':
        multiplier = Decimal('1')
    else:
        raise ValueError('current report trade missing contract multiplier')
    if (quantity == 0 or price <= 0 or gross <= 0 or multiplier <= 0 or
        gross != abs(quantity)*price*multiplier):
        raise ValueError('current report trade amounts do not reconcile')
    if not is_fx and tax is not None and (fee == 0 or fee_currency == currency) and net != (-gross-fee-tax if side == 'BUY' else gross-fee-tax):
        raise ValueError('current report trade amounts do not reconcile')
    rate = dated_fx_rate(cur, currency, day)
    identity = json.dumps(['ibkr', account, official[0], official[1]], separators=(',', ':'))
    key = 'ibkr_trade:' + sha256(identity.encode()).hexdigest()
    realized = _number(values, ('fifopnlrealized',), 'realized pnl') if values.get('fifopnlrealized') else None
    return {'source_table': 'ibkr_flex_trade', 'source_id': key, 'source_key': key,
            'trade_date': day, 'account_code': 'ibkr', 'broker': 'ibkr', 'symbol': symbol,
            'name': values.get('description') or symbol, 'market': values.get('assetcategory'),
            'currency': currency, 'side': side, 'quantity': quantity, 'price': price,
            'gross_amount': gross, 'fee_amount': fee, 'tax_amount': tax, 'net_cash': net,
            'realized_pnl': realized, 'fx_rate_to_cny': rate, 'raw_json': raw,
            'explicit_fee_currency': fee_currency,
            'source_evidence': {'broker_source': 'ibkr_flex', 'source_sha256': report_sha256,
                                'confirmed_day': day.isoformat(), 'evidence_role': 'source_report'}}
