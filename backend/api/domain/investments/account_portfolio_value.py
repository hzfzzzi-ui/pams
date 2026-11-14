"""Read cash-inclusive brokerage values for the account center."""

import os
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from domain.brokers.ibkr.ibkr_daily_close import POSITION_FIELDS, parse_daily_close_evidence
from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
from domain.brokers.ibkr.ibkr_daily_holdings import last_publication
from domain.investments.nav_trade_source_v2 import read_reconciled_cash


def _money(value):
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _ibkr_value(cur, *, as_of, base_currency, owner_ratio):
    account = os.environ.get('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', '').strip()
    report = last_publication(cur, through=as_of) if account else None
    if not report:
        return None
    day = report['period_end']
    cur.execute("SELECT content FROM public.ibkr_source_originals WHERE sha256=%s", (report['sha256'],))
    stored = cur.fetchone()
    if not stored:
        return None
    try:
        original = bytes(stored['content']).decode('utf8')
    except UnicodeDecodeError:
        return None
    close = parse_daily_close_evidence(original, account_id=account, as_of=day,
                                       expected_sha256=report['sha256'])
    if close['status'] != 'ok':
        return None
    fx = historical_fx(cur, currency=close['base_currency'], base_currency=base_currency, as_of=day)
    if fx['status'] != 'ok':
        return None
    rate = fx['rate'] * owner_ratio
    splits = close['component_proof']['subcomponent_checks']
    position_splits = [splits.get(f'{field}_long_short') for field in POSITION_FIELDS]
    leverage = None
    if close['nav_base'] > 0 and None not in position_splits:
        gross = sum(split['long'] - split['short'] for split in position_splits if split is not None)
        leverage = float(gross / close['nav_base'])
    period_start = date(day.year, 1, 1)
    period = parse_daily_close_evidence(
        original, account_id=account, as_of=day, period_start=period_start,
        expected_sha256=report['sha256'],
    ).get('period')
    return {'amount_base': _money(close['nav_base'] * rate), 'cash_base': _money(close['cash_base'] * rate),
            'position_value_base': _money(close['position_value_base'] * rate), 'leverage_ratio': leverage,
            'display_period_pnl_base': _money(period['pnl_base'] * rate) if period else None,
            'period_start': period_start.isoformat() if period else None,
            'as_of': day.isoformat(), 'cash_as_of': day.isoformat(), 'status': 'official_close',
            'updated_at': report['updated_at'].isoformat() if report['updated_at'] else None}


def read_account_portfolio_value(cur, row, *, as_of, base_currency, owner_ratio):
    code = row['account_code']
    if code == 'ibkr':
        return _ibkr_value(cur, as_of=as_of, base_currency=base_currency, owner_ratio=owner_ratio)
    if (code != 'tonghuashun' or base_currency != 'CNY' or row['has_snapshot_cash']
            or row['fx_missing_count'] or row['known_value_base'] is None or not row['single_snapshot_day']):
        return None
    day = row['holdings_as_of']
    evidence = read_reconciled_cash(cur, as_of=day)
    if not evidence:
        return None
    cash_day, broker_cash = evidence
    cash = broker_cash * owner_ratio
    updated_at = row['holdings_updated_at']
    net_value = row['known_value_base'] + cash
    gross = row.get('gross_position_value')
    leverage = float(gross * owner_ratio / net_value) if gross is not None and net_value > 0 else None
    return {'amount_base': _money(net_value), 'cash_base': _money(cash), 'leverage_ratio': leverage,
            'as_of': day.isoformat(), 'cash_as_of': cash_day.isoformat(),
            'updated_at': updated_at.isoformat() if updated_at else None,
            'status': 'cash_carried_forward' if cash_day < day else 'reconciled_cash'}
