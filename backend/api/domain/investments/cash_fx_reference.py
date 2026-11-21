"""Display an already valued foreign cash balance over a missing weekend fixing."""
from datetime import timedelta


def preceding_weekend_dates(day):
    """Look back at most to this weekend's Friday, most recent day first."""
    return [day - timedelta(days=offset) for offset in range(1, day.weekday() - 3)]


def supports_cash_reference(account, currency, rows):
    return (account.get('account_type') == 'funding'
            and account.get('truth_source') in {'bank_statement', 'official_statement'}
            and str(account.get('base_currency') or '').upper() == currency
            and bool((account.get('primary_balance') or {}).get('source'))
            and all(str(row.get('currency') or currency).upper() == currency for row in rows))


def weekend_cash_references(day, bank_series, rate_at):
    """Require the preceding converted account value as well as unchanged native cash."""
    prior_days = preceding_weekend_dates(day)
    if not prior_days:
        return {}
    result = {}
    current = bank_series.get(day, {})
    for code, (currency, native, balance_day, anchor_day) in current.get('_unconverted_cash', {}).items():
        if code not in current.get('unavailable_accounts', ()) or code in current.get('source_evidence_unavailable_accounts', ()):
            continue
        for previous in prior_days:
            rate = rate_at(currency, previous)
            if rate is None:
                continue
            prior = bank_series.get(previous, {})
            amount = native * rate
            if (balance_day <= previous and code not in prior.get('unavailable_accounts', ())
                    and code not in prior.get('source_evidence_unavailable_accounts', ())
                    and prior.get('_comparison_basis', {}).get(code) in ('cash_balance','cash_anchor')
                    and prior.get('account_values', {}).get(code) == amount):
                result[code] = {'amount':amount, 'native_balance':str(native), 'currency':currency,
                                'balance_as_of':balance_day, 'balance_anchor_as_of':anchor_day,
                                'fx_as_of':previous, 'fx_rate':str(rate)}
            # A newer fixing with missing/conflicting balance evidence cannot be bypassed.
            break
    return result
