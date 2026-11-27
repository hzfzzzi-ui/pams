"""Read cash-inclusive brokerage values for the account center."""

import os
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from domain.brokers.ibkr import ibkr_daily_close as close_parser
from domain.brokers.ibkr.ibkr_daily_close import POSITION_FIELDS, parse_daily_close_evidence
from domain.brokers.ibkr.ibkr_daily_close_read import historical_fx
from domain.brokers.ibkr.ibkr_daily_holdings import last_publication
from domain.investments.holdings.ths_trade_rollforward import ths_post_snapshot_delta_and_count
from domain.investments.trades.nav_trade_source_v2 import read_reconciled_cash


def _money(value):
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def extract_ibkr_cash_by_currency(
    original: str,
    *,
    account_id: str,
    as_of: date,
    base_currency: str,
    fx_rate: Decimal,
    owner_ratio: Decimal,
) -> list[dict] | None:
    """Extract per-currency ending cash from a verified IBKR Flex XML close.

    Converts each currency using the statement's same-day ConversionRate to IBKR
    base currency, then the internal historical FX rate to the display base currency.
    Fail-closed: returns None if CashReport or required ConversionRate evidence is missing.
    """
    try:
        root = ET.fromstring(original)
        evidence = close_parser._Evidence(root, account_id)
        statements = evidence.by_day.get(as_of) or []
        if len(statements) != 1:
            return None
        statement = statements[0]
        stmt_base = evidence.base(statement)
        if stmt_base != base_currency:
            return None
        section = close_parser._one(close_parser._rows(statement, 'CashReport'), 'CashReport')
        cash_native: dict[str, Decimal] = {}
        for row in close_parser._rows(section, 'CashReportCurrency'):
            if (close_parser._day(row.get('fromDate')) != as_of
                    or close_parser._day(row.get('toDate')) != as_of):
                continue
            ccy = (row.get('currency') or '').strip().upper()
            if not ccy or ccy == 'BASE_SUMMARY':
                continue
            if row.get('levelOfDetail') not in (None, '', 'Currency'):
                continue
            if ccy in cash_native:
                return None
            cash_native[ccy] = close_parser._number(row, 'endingCash')
        if not cash_native:
            return None
        conv_rates: dict[str, Decimal] = {stmt_base: Decimal(1)}
        for row in close_parser._rows(statement, 'ConversionRate'):
            if (row.get('toCurrency') or '').strip().upper() != stmt_base:
                continue
            if close_parser._day(row.get('reportDate')) != as_of:
                continue
            ccy = (row.get('fromCurrency') or '').strip().upper()
            rate_val = close_parser._number(row, 'rate')
            if ccy and rate_val > 0:
                conv_rates[ccy] = rate_val
        if not set(cash_native) <= set(conv_rates):
            return None
        items = []
        scale = fx_rate * owner_ratio
        for ccy, native_val in cash_native.items():
            amount_base = native_val * conv_rates[ccy] * scale
            if abs(amount_base) < Decimal('0.01'):
                continue
            items.append({
                'currency': ccy,
                'native_amount': _money(native_val * owner_ratio),
                'amount_base': _money(amount_base),
                'as_of': as_of.isoformat(),
            })
        items.sort(key=lambda item: (Decimal(item['amount_base']) < 0, -abs(Decimal(item['amount_base']))))
        return items
    except (ET.ParseError, ValueError, KeyError, TypeError):
        return None


def _ibkr_close_value(cur, row, *, as_of, base_currency, owner_ratio, scope_complete):
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
    cash_by_currency = extract_ibkr_cash_by_currency(
        original, account_id=account, as_of=day, base_currency=close['base_currency'],
        fx_rate=fx['rate'], owner_ratio=owner_ratio,
    )
    pos_cash = (sum((Decimal(i['amount_base']) for i in cash_by_currency if Decimal(i['amount_base']) > 0), Decimal(0))
                if cash_by_currency is not None else None)
    neg_cash = (sum((abs(Decimal(i['amount_base'])) for i in cash_by_currency if Decimal(i['amount_base']) < 0), Decimal(0))
                if cash_by_currency is not None else None)
    result = {'amount_base': _money(close['nav_base'] * rate), 'cash_base': _money(close['cash_base'] * rate),
              'base_currency': close['base_currency'],
              'position_value_base': _money(close['position_value_base'] * rate), 'leverage_ratio': leverage,
              'display_period_pnl_base': _money(period['pnl_base'] * rate) if period else None,
              'period_start': period_start.isoformat() if period else None,
              'as_of': day.isoformat(), 'cash_as_of': day.isoformat(), 'status': 'official_close',
              'updated_at': report['updated_at'].isoformat() if report['updated_at'] else None}
    if cash_by_currency is not None:
        result['cash_by_currency'] = cash_by_currency
        result['positive_cash_base'] = _money(pos_cash)
        result['negative_cash_base'] = _money(neg_cash)
    return result


def _reconciled_trade_cash_value(cur, row, *, as_of, base_currency, owner_ratio, scope_complete):
    if (scope_complete or base_currency != 'CNY' or row['has_snapshot_cash']
            or row['fx_missing_count'] or row['known_value_base'] is None or not row['single_snapshot_day']):
        return None
    day = row['holdings_as_of']
    effective_as_of = max(day, as_of) if as_of else day
    evidence = read_reconciled_cash(cur, as_of=effective_as_of, account_code=row['account_code'])
    pos_delta = Decimal(0)
    projected_count = None
    if evidence and evidence[0] > day:
        res = ths_post_snapshot_delta_and_count(
            cur, snap_day=day, as_of=effective_as_of, account_code=row['account_code'],
        )
        if res is None:
            # Fail-closed: if post-snapshot position delta cannot be validated/priced, stay on snapshot date.
            evidence = read_reconciled_cash(cur, as_of=day, account_code=row['account_code'])
        else:
            pos_delta, projected_count = res
    if not evidence:
        return None
    cash_day, broker_cash = evidence
    cash = broker_cash * owner_ratio
    updated_at = row['holdings_updated_at']
    known_value = row['known_value_base'] + pos_delta * owner_ratio
    net_value = known_value + cash
    gross = row.get('gross_position_value')
    if gross is not None:
        gross = gross + pos_delta
    val_day = max(day, cash_day)
    leverage = float(gross * owner_ratio / net_value) if gross is not None and net_value > 0 else None
    result = {'amount_base': _money(net_value), 'cash_base': _money(cash),
              'gross_cash_base': _money(broker_cash), 'owner_ratio': str(owner_ratio),
              'leverage_ratio': leverage,
              'as_of': val_day.isoformat(), 'cash_as_of': cash_day.isoformat(),
              'updated_at': updated_at.isoformat() if updated_at else None,
              'status': 'cash_carried_forward' if cash_day < val_day else 'reconciled_cash'}
    if projected_count is not None:
        result['projected_holdings_count'] = projected_count
    return result


_PORTFOLIO_VALUE_READERS = {
    "official_broker_close": _ibkr_close_value,
    "reconciled_trade_cash": _reconciled_trade_cash_value,
}


def read_account_portfolio_value(cur, row, *, evidence_mode, as_of, base_currency, owner_ratio, scope_complete):
    reader = _PORTFOLIO_VALUE_READERS.get(evidence_mode)
    if reader is None:
        return None
    return reader(cur, row, as_of=as_of, base_currency=base_currency, owner_ratio=owner_ratio,
                  scope_complete=scope_complete)


def account_center_balance(holdings, currency):
    """Project the same valuation into the compact account list."""
    portfolio = holdings.get("portfolio_value") or {}
    total = holdings.get("total_value_base")
    amount = portfolio.get("amount_base")
    if amount is None:
        amount = total if total is not None else holdings.get("known_value_base")
    if amount is None:
        return None
    if portfolio.get("status") == "cash_carried_forward":
        label = "组合估值"
    elif portfolio or total is not None:
        label = "组合价值"
    else:
        label = "已覆盖市值"
    return {
        "balance_amount": amount,
        "balance_currency": currency,
        "balance_as_of": portfolio.get("as_of") or holdings.get("holdings_as_of"),
        "valuation_updated_at": (portfolio.get("updated_at") if portfolio
                                 else holdings.get("holdings_updated_at")),
        "balance_label": label,
    }
