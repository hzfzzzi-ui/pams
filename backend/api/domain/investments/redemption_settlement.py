"""Value confirmed redemption proceeds from share cancellation until bank receipt."""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from domain.imports.source_original_read import SourceOriginals
from domain.imports.trade_supplemental_evidence import read as read_trade_evidence
from domain.investments.fund_return_history import _trade_day
from domain.investments.trade_read_v2 import load_trade_rows
from domain.ledger import confirmed_transfers_v2 as transfers


def _redemption(pair, trade, supplements):
    left, right = pair['from_evidence'], pair['to_evidence']
    if (pair['evidence_status'] != 'confirmed' or trade['source_evidence_status'] != 'verified'
            or trade['trade_type'] not in {'fund_redemption', 'sell'} or not trade['asset_id']
            or left['kind'] != 'trade' or right['kind'] != 'cashflow' or left['id'] != trade['trade_id']
            or left['account_code'] != trade['account_code']
            or left['currency'] != trade['currency'] or right['currency'] != trade['currency']
            or right.get('additional_targets') or left['account_code'] == right['account_code']
            or left.get('cash_event_day', left.get('receipt_day', right['day'])) != right['day']):
        return None
    confirmation = left.get('execution_day') or left.get('confirmed_day') or trade.get('fund_confirmation_date')
    try:
        details = {(s['fields']['execution_date'], Decimal(str(s['fields'].get('quantity'))),
                    Decimal(str(s['fields'].get('unit_nav'))))
                   for s in supplements if s['status'] == 'verified' and s['fields'].get('execution_date')}
        quantity, price = trade.get('quantity'), trade.get('price')
        if details:
            if len(details) != 1:
                return None
            execution, units, mark = details.pop()
            if ((confirmation and confirmation != execution) or quantity not in (None, units)
                    or price not in (None, mark)):
                return None
            confirmation, quantity, price = execution, units, mark
        receipt = date.fromisoformat(left.get('receipt_day', right['day']))
        if trade['trade_type'] == 'fund_redemption':
            if not trade.get('fund_execution_confirmed'):
                return None
            # Use the same effective day that removes the shares from the position.
            start = _trade_day(trade)
            if (start < trade['trade_date'] or confirmation and not start <= date.fromisoformat(confirmation) <= receipt):
                return None
        else:
            start = date.fromisoformat(confirmation)
        amount = trade['net_cash']
        if (start > receipt or start == receipt and trade['trade_type'] != 'fund_redemption'
                or quantity is None or price is None or quantity <= 0 or price <= 0
                or not amount or not all(v.is_finite() for v in (quantity, price, amount))
                or amount <= 0 or Decimal(right['amount']) != amount or Decimal(left['amount']) != -amount
                or trade.get('fee_currency') not in (None, trade['currency'])):
            return None
        # Unit prices and fees are printed to fixed precision by the source.
        gross = quantity * price
        # Independently verified cash can settle a fund order whose share
        # count was reconstructed at rounded precision. Its booked gross,
        # charges and proceeds must still reconcile with the exact receipt.
        if left.get('settlement_confirmation_id') and trade.get('gross_amount') is not None:
            if abs(gross - trade['gross_amount']) > (price + 1) * Decimal('0.005'):
                return None
            gross = trade['gross_amount']
        net = gross - (trade.get('fee_amount') or 0) - (trade.get('tax_amount') or 0)
        if abs(net - amount) > Decimal('0.005'):
            return None
    except (ValueError, TypeError, InvalidOperation):
        return None
    return {'account_code': trade['account_code'], 'asset_id': trade['asset_id'], 'symbol': trade['symbol'],
            'trade_id': trade['trade_id'], 'currency': trade['currency'], 'amount': amount,
            'confirmation': start, 'receipt': receipt}


def load_redemption_receivables(cur, accounts, dates):
    if not accounts or not dates:
        return []
    cur.execute("SELECT to_regclass('public.evidence_confirmation') IS NOT NULL AND to_regclass('public.source_records') IS NOT NULL AS supported")
    if not cur.fetchone()['supported']:
        return []
    originals = SourceOriginals(cur)
    pairs = [p for p in transfers.read(cur, limit=None, originals=originals, redemption_accounts=accounts)
             if p['evidence_status'] == 'confirmed']
    sales = Counter(p['from_trade_id'] for p in pairs)
    receipts = Counter(p['to_cashflow_id'] for p in pairs)
    first_day, last_day = min(dates), max(dates)
    results = []
    for pair in pairs:
        ident = pair['from_trade_id']
        receipt_day = pair['from_evidence'].get('receipt_day', pair['to_evidence']['day'])
        if sales[ident] != 1 or receipts[pair['to_cashflow_id']] != 1 or receipt_day < str(first_day):
            continue
        rows = load_trade_rows(cur, account_code=pair['from_account_code'], start=date.min, end=date.max,
                               fact_id=ident, originals=originals, include_fund_execution=True)
        if len(rows) != 1:
            continue
        trade = rows[0]
        supplements = read_trade_evidence(cur, trade_id=ident) if trade['trade_type'] == 'sell' else []
        observation = _redemption(pair, trade, supplements)
        if observation and observation['confirmation'] <= last_day:
            results.append(observation)
    return results
