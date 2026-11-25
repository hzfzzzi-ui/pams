"""Native dated account values shared by account history and personal NAV."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from domain.accounts.monthly_cash_bridge import reconciled_card_months, table_balances_at
from domain.accounts.monthly_credit_evidence import recurring_credit_months
from domain.market.month_end_marks import is_month_end
from domain.accounts.bank_source_status import summaries
from domain.accounts.credit_card_debt import confirmed_table_net_history, uses_table_net_reference, verified_table_postings
from domain.accounts.bank_statement_balance import statement_balance_history
from domain.imports.source_original_read import SourceOriginals
from domain.investments.valuation.statement_period_nav import load_statement_period_points


@dataclass(frozen=True)
class AccountValuation:
    day: date
    currency: str
    amount: Decimal | None
    basis: str
    # A billed balance covers posted items only. An account net already includes debt.
    scope: str = 'account'
    quality: str = 'confirmed'
    observed_on: date | None = None

    evidence_dates: tuple[date, date] | None = None

    @classmethod
    def from_measurement(cls, day, currency, point, field):
        scopes = {'net_value': 'account', 'liabilities': 'debt', 'position_value': 'positions'}
        value = point.get(field)
        quality = 'unavailable' if value is None else point.get(field + '_status', 'confirmed')
        return cls(day, currency, value, field, scopes[field], quality, point.get(field + '_as_of') or day)

    def including_unbilled(self, principal):
        if self.scope != 'billed' or self.amount is None:
            return self
        return AccountValuation(self.day, self.currency, self.amount - principal, self.basis,
                                quality=self.quality, observed_on=self.observed_on, evidence_dates=self.evidence_dates)

    def agrees_with_balance(self, signed_balance, principal=Decimal(0)):
        combined = self.including_unbilled(principal)
        return self.amount is not None and signed_balance in (self.amount, combined.amount)

    def personal_value(self, owner_ratio, rate):
        if (self.scope != 'account' or self.quality not in ('confirmed', 'estimated')
                or self.amount is None or owner_ratio is None):
            return None
        try:
            ratio = Decimal(str(owner_ratio))
            amount = Decimal(str(self.amount))
        except (InvalidOperation, ValueError):
            return None
        if not amount.is_finite() or not ratio.is_finite() or not 0 <= ratio <= 1:
            return None
        owned = amount * ratio
        if not owned:
            return Decimal(0)
        if rate is None:
            return None
        fx = Decimal(str(rate))
        return owned * fx if fx.is_finite() and fx > 0 else None


def statement_closings(cur, accounts, through):
    return statement_balance_history(cur, accounts, through, originals=SourceOriginals(cur))


def statement_valuations(cur, accounts, dates, *, closings=None, table_cards=()):
    """Read signed originals and reconstructed periods once per range, preserving conflicts."""
    if not accounts or not dates:
        return {}
    if closings is None:
        closings = statement_closings(cur, accounts, max(dates))
    result = {}
    for code, rows in closings.items():
        currency, card = accounts[code]
        for row in rows:
            day = row['as_of']
            result[code, day] = AccountValuation(day, currency, Decimal(row['amount']), 'statement',
                                                'billed' if card else 'account')
    charges = {}
    monthly = [d for d in dates if is_month_end(d)]
    cards = {c: spec for c, spec in accounts.items() if spec[1]}
    periods = (load_statement_period_points(cur, list(accounts), dates, dated_charges=charges) if monthly and cards
               else load_statement_period_points(cur, list(accounts), dates))
    for (code, day), (currency, side, value) in periods.items():
        unit, card = accounts[code]
        if currency != unit or (side == '负债') != card:
            continue
        signed = -value if card else value
        previous = result.get((code, day))
        if previous is not None and previous.amount != signed:
            result[code, day] = AccountValuation(day, unit, None, 'conflicting_statements')
        else:
            result[code, day] = AccountValuation(day, unit, signed, 'statement_period' if previous is None else 'statement',
                                                'billed' if card else 'account')
    if monthly:
        brackets = dict(closings)
        if cards and any(not closings.get(c) or closings[c][-1]['as_of'] < max(monthly) for c in cards):
            brackets.update(statement_closings(cur, cards, max(monthly) + timedelta(days=35)))
        evidence = [(reconciled_card_months(cur, {c: spec for c, spec in accounts.items() if c not in table_cards}, monthly, brackets, charges), 'posted_statement_bridge', 'billed'),
                    (recurring_credit_months(cur, accounts, monthly), 'recurring_credit_bridge', 'account')]
        for balances, basis, scope in evidence:
            for key, amount in balances.items():
                code, day = key
                if key not in result:
                    result[key] = AccountValuation(day, accounts[code][0], amount, basis, scope, quality='estimated')
    for code in cards.keys() & set(table_cards):
        for day, value in anchored_table_valuations(cur, code, cards[code][0], dates).items():
            result.setdefault((code, day), value)
    return result


def anchored_table_valuations(cur, code, unit, dates):
    sources = SourceOriginals(cur)
    conflicts = set()
    closes = statement_balance_history(cur, {code: (unit, True)}, max(dates) + timedelta(days=35),
                                      originals=sources, evidence_statements=True, conflicts=conflicts).get(code, [])
    if not closes:
        return {}
    blocked = any(c == code for c, _ in conflicts) or summaries(cur, [code]).get(code, {}).get('duplicate_count')
    movements = {} if blocked else verified_table_postings(cur, code, unit, sources)
    result = {day: AccountValuation(day, unit, amount, basis, 'billed',
                                  quality='estimated' if basis == 'statement_plus_postings' else 'confirmed',
                                  evidence_dates=(first, last))
            for day, (amount, basis, first, last) in table_balances_at(closes, movements, dates).items()}
    # After an original balance is known, a broken bridge cannot fall back to
    # treating the entire ledger as if its opening debt were zero.
    through = max(max(movements, default=closes[-1]['as_of']), closes[-1]['as_of'])
    for day in dates:
        if closes[0]['as_of'] < day <= through and day not in result:
            result[day] = AccountValuation(day, unit, None, 'unreconciled_statement', 'billed', quality='unavailable')
    return result


def posted_card_valuations(cur, code, cfg, currency, through):
    """Return posting-day estimates only for an approved, duplicate-free ledger."""
    if not uses_table_net_reference(code, cfg, cur):
        return {}
    status = summaries(cur, [code]).get(code, {})
    rows = confirmed_table_net_history(cur, code, currency=currency, today=through,
                                       has_duplicate_alerts=bool(status.get('duplicate_count')))
    return {row['as_of']: AccountValuation(row['as_of'], currency, Decimal(row['amount']), 'posted_flow_net', quality='estimated')
            for row in rows}
