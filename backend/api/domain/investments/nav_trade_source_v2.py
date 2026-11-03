"""Evidence-aware projection from redesigned broker facts; not a coverage proof."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from domain.investments.trade_read_v2 import load_trade_rows


def project(cur, *, dates, owner_ratio, series_type):
    result = series_type({day: Decimal('0') for day in dates})
    if not dates:
        return result
    # Individual facts do not prove the opening balance or complete account scope.
    result.cash_scope_unverified = True
    result.unknown_dates.update(dates)
    facts = load_trade_rows(cur, account_code='tonghuashun', start=date.min, end=max(dates))
    markets = {'上海Ａ股': 'sh', '深圳Ａ股': 'sz', 'SSE': 'sh', 'SZSE': 'sz'}
    for day in dates:
        cash = Decimal('0')
        quantities = {}
        exchanges = {}
        for fact in facts:
            if fact['trade_date'] > day:
                continue
            if fact['source_evidence_status'] != 'verified':
                continue
            typ, currency, amount = fact['trade_type'], fact['currency'], fact['net_cash']
            if amount is not None:
                if currency == 'CNY':
                    cash += amount
                else:
                    cur.execute('''SELECT rate FROM pams_fx_rates_daily WHERE base_currency='CNY' AND quote_currency=%s
                        AND as_of=%s AND rate>0 AND rate::text NOT IN ('NaN','Infinity','-Infinity')
                        AND NULLIF(btrim(source),'') IS NOT NULL LIMIT 1''', (currency, fact['trade_date']))
                    fx = cur.fetchone()
                    if fx:
                        cash += amount * fx['rate']
            if typ not in {'buy', 'sell'}:
                continue
            symbol, quantity = fact['symbol'], fact['quantity']
            market = markets.get(fact.get('market')) or markets.get(fact.get('source_market'))
            if not symbol or quantity is None or quantity <= 0:
                continue
            quantities[symbol] = quantities.get(symbol, Decimal('0')) + (quantity if typ == 'buy' else -quantity)
            exchanges.setdefault(symbol, set()).add(market)
        value = cash
        for symbol, quantity in quantities.items():
            if quantity == 0:
                continue
            if quantity < 0 or len(exchanges[symbol]) != 1 or None in exchanges[symbol]:
                result.missing_price_dates.add(day)
                continue
            provider_symbol = next(iter(exchanges[symbol])) + symbol
            cur.execute('''SELECT close,trade_date FROM pams_ohlcv_daily WHERE symbol=%s AND market='CN_A'
                AND trade_date<=%s AND close>0 AND close::text NOT IN ('NaN','Infinity','-Infinity')
                ORDER BY trade_date DESC LIMIT 1''', (provider_symbol, day))
            quote = cur.fetchone()
            if not quote:
                result.missing_price_dates.add(day)
                continue
            value += quantity * quote['close']
            if quote['trade_date'] < day:
                result.estimated_dates.add(day)
        result[day] = value * owner_ratio
    return result
