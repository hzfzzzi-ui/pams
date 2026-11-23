"""Month-end custody and currency changes for confirmed same-currency cash transfers."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from domain.brokers.ibkr.funding_marks import funding_marks
from domain.ledger import confirmed_transfers_v2

ZERO = Decimal(0)


def _custody(day, sent, received, amount):
    return amount * (int(sent <= day) - int(received <= day))


def _translated(amount, unit, day, rate_at):
    if not amount:
        return ZERO
    rate = rate_at(unit, day)
    return amount * rate if rate is not None else None


def _month_change(day, source, target, amount, currency, ratio, rate_at, flow_rate_at):
    """Residual of released capital minus sent capital after both transit inventory endpoints."""
    prior = day.replace(day=1) - timedelta(days=1)
    sent, sent_fx_day, sent_unit, sent_amount = source
    received, received_fx_day, received_unit, received_amount = target
    parts = [
        _translated(_custody(day, sent, received, amount), currency, day, rate_at),
        _translated(-_custody(prior, sent, received, amount), currency, prior, rate_at),
    ]
    for booked, fixing_day, unit, signed in ((sent, sent_fx_day, sent_unit, -sent_amount),
                                           (received, received_fx_day, received_unit, received_amount)):
        if prior < booked <= day:
            parts.append(_translated(signed, unit, fixing_day, flow_rate_at))
    return sum(parts, ZERO) * ratio if ratio is not None and None not in parts else None


def cash_in_transit(cur, books, rate_at, *, fx_changes=None, flow_rate_at=None):
    """A broker cash event enters its account NAV on the daily close recognizing its capital."""
    if not books:
        return {}
    broker_codes = {code for accounts in books.values() for code, r in accounts.items()
                    if r.get('method') == 'broker_cash_legs_and_executions'}
    cur.execute("""SELECT e.confirmation_id,coalesce(f.posting_date,f.event_date) sent,
                   coalesce(t.posting_date,t.event_date) received
              FROM evidence_confirmation e JOIN pams_cashflow_fact f ON f.cashflow_id=e.from_cashflow_id
              JOIN pams_cashflow_fact t ON t.cashflow_id=e.to_cashflow_id
              WHERE e.confirmation_type='transfer' AND e.revoked_at IS NULL AND f.currency=t.currency
                AND (f.account_code=ANY(%s::text[]) OR t.account_code=ANY(%s::text[])
                  OR EXISTS (SELECT 1 FROM unnest(%s::date[]) AS dates(d)
                    WHERE d>=least(coalesce(f.posting_date,f.event_date),coalesce(t.posting_date,t.event_date))
                      AND d<greatest(coalesce(f.posting_date,f.event_date),coalesce(t.posting_date,t.event_date))))""",
                (sorted(broker_codes), sorted(broker_codes), list(books)))
    periods = {str(r['confirmation_id']): (r['sent'], r['received']) for r in cur.fetchall()}
    pairs = [pair for pair in confirmed_transfers_v2.read(cur, limit=None, transfer_ids=periods)
             if pair['evidence_status'] == 'confirmed' and not pair['to_evidence'].get('additional_targets')]
    broker_ids = {leg['id'] for pair in pairs for leg in (pair['from_evidence'], pair['to_evidence'])
                  if leg['account_code'] in broker_codes}
    marks = funding_marks(cur, broker_ids)
    result = defaultdict(list)
    for pair in pairs:
        left, right = pair['from_evidence'], pair['to_evidence']
        amount, unit = Decimal(right['amount']), right['currency']
        if amount <= 0 or amount != -Decimal(left['amount']) or unit != left['currency']:
            continue
        bank_dates = periods[str(pair['confirmation_id'])]
        sides = []
        includes_broker = False
        for leg, posted in zip((left, right), bank_dates):
            if leg['account_code'] in broker_codes:
                includes_broker = True
                booked, base, translated = marks.get(leg['id'], (None, None, None))
                sides.append((booked, booked, base, abs(translated) if translated is not None else None))
            else:
                # Ledger cash FX translates the bank leg on the transaction day.
                event_day = date.fromisoformat(leg['day']) if leg.get('day') else posted
                sides.append((posted, event_day, unit, amount))
        sent, received = sides[0][0], sides[1][0]
        proven = all(s[3] is not None for s in sides)
        earliest = min(d for d in (*bank_dates, sent, received) if d is not None)
        latest = max(sent, received) if sent is not None and received is not None else None
        for day, accounts in books.items():
            if earliest > day or (latest is not None and latest < day.replace(day=1)):
                continue
            source, target = (accounts.get(leg['account_code']) for leg in (left, right))
            if source is None or target is None:
                continue
            signed = _custody(day, sent, received, amount) if proven else None
            if signed or (not proven and (latest is None or day < latest)):
                ratio = (source if signed is None or signed > 0 else target)['owner_ratio']
                value = _translated(signed, unit, day, rate_at) if signed is not None else None
                result[day].append({'from_account': left['account_code'], 'to_account': right['account_code'],
                                    'source_fact_date': bank_dates[0], 'target_fact_date': bank_dates[1],
                                    'sent': sent, 'received': received, 'currency': unit, 'native': signed,
                                    'personal': value * ratio if None not in (value, ratio) else None})
            if fx_changes is not None and includes_broker:
                ratio = source['owner_ratio'] if source['owner_ratio'] == target['owner_ratio'] else None
                value = (_month_change(day, *sides, amount, unit, ratio, rate_at, flow_rate_at)
                         if proven else None)
                existing = fx_changes.get(day, ZERO)
                fx_changes[day] = existing + value if None not in (existing, value) else None
    return result
