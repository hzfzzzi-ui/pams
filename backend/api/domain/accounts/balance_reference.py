"""Project an archived balance for display without publishing a valuation."""

from datetime import date
from decimal import Decimal, InvalidOperation


def historical_balance(reference, today, owner_ratio, *, base_currency, basis):
    currency = str(reference.get('currency') or base_currency or 'CNY').upper()
    if currency != 'CNY':
        return None
    try:
        amount = Decimal(str(reference.get('amount')))
        as_of = date.fromisoformat(str(reference.get('as_of')))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or as_of > today:
        return None
    return {
        'balance_amount': str(amount * owner_ratio),
        'balance_currency': currency,
        'balance_label': '历史参考余额',
        'balance_basis': basis,
        'balance_as_of': as_of,
    }


def historical_account_balance(cfg, as_of, owner_ratio):
    reference = cfg.get('account_balance_reference') or {}
    if not reference.get('source'):
        return None
    return historical_balance(reference, as_of, owner_ratio, base_currency=cfg.get('base_currency'),
                              basis='historical_account_reference')
