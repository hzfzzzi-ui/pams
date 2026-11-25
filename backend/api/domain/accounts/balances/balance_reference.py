"""Project an archived balance for display without publishing a valuation."""

from datetime import date
from decimal import Decimal, InvalidOperation


def historical_balance(reference, today, owner_ratio, *, base_currency, basis):
    currency = str(reference.get('currency') or base_currency or 'CNY').upper()
    # A single reference can represent the account's own currency only.
    # Conversion is performed by the NAV reader using dated FX evidence.
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        return None
    if base_currency and currency != str(base_currency).upper():
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


def load_bound_balance_references(cur, accounts):
    """Recheck the published principal against its original before using a configured estimate."""
    from domain.investments.valuation.valuation_input_evidence import _bound_input, _hash, verify_recorded_version

    codes = [code for code, cfg in accounts.items() if cfg.get('account_balance_reference')]
    if not codes:
        return {}
    cur.execute("""SELECT v.*, a.base_currency account_currency, a.active, a.is_asset
        FROM valuation_evidence v LEFT JOIN pams_accounts a USING(account_code)
        WHERE v.result_kind='portfolio' AND v.calculation_version='historical-principal-reference-v1'
          AND v.account_code=ANY(%s::text[])""", (codes,))
    rows = cur.fetchall()
    if not rows:
        return {}
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    locking = not (mode['readonly'] and mode['level'] in {'repeatable read', 'serializable'})
    bindings = {row['account_code']: None for row in rows}
    rejected = set()
    for row in rows:
        code = row['account_code']
        reference = accounts[code]['account_balance_reference']
        if str(reference.get('as_of')) != row['business_date'].isoformat():
            continue
        try:
            saved = row['inputs'][0]['evidence']
            if (len(row['inputs']) != 1 or saved['kind'] != 'source_item' or saved['role'] != 'reported_principal_reference'
                    or row['scope_key'] != 'account-balance-reference:' + code
                    or not row['active'] or not row['is_asset']
                    or row['account_currency'] != row['base_currency'] or row['base_currency'] != reference['currency']
                    or not verify_recorded_version(cur, row['version_key'])):
                raise ValueError
            current, raw = _bound_input(cur, 'source_item', saved['source_ref']['key'], locking=locking)
            snapshot = current['snapshot']
            fields = snapshot['source_fields']
            amount = Decimal(str(fields['reference_amount']))
            if (not raw or snapshot != saved['snapshot'] or snapshot['source_integrity'] != 'verified'
                    or snapshot['batch_status'] != 'published' or snapshot['review_status'] != 'confirmed'
                    or snapshot['business_type'] != 'statement' or raw['batch']['acquisition_method'] != 'file'
                    or not raw['batch']['sha256'] or not raw['batch']['content']
                    or current['source_ref']['account_code'] != code or fields['account_code'] != code
                    or fields['business_date'] != row['business_date'].isoformat() or fields['currency'] != row['base_currency']
                    or fields['reference_basis'] != 'contributed_principal' or _hash(fields) != row['result_hash']
                    or not amount.is_finite() or amount < 0 or amount != Decimal(str(reference['amount']))):
                raise ValueError
            bindings[code] = {**reference, 'amount': amount, 'reference_basis': fields['reference_basis']}
        except (KeyError, IndexError, TypeError, ValueError, InvalidOperation):
            rejected.add(code)
    for code in rejected:
        bindings[code] = None
    return bindings


def historical_account_balance(cfg, as_of, owner_ratio, *, account_code=None, bindings=None):
    reference = cfg.get('account_balance_reference') or {}
    if bindings and account_code in bindings:
        reference = bindings[account_code]
    if not reference or not reference.get('source'):
        return None
    result = historical_balance(reference, as_of, owner_ratio, base_currency=cfg.get('base_currency'),
                                basis='historical_account_reference')
    if result and reference.get('reference_basis') == 'contributed_principal':
        result['balance_label'] = '历史参考本金'
        result['balance_reference_basis'] = 'contributed_principal'
    return result
