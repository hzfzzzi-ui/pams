"""Dated ownership of pooled accounts using reconciled personal funding legs."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from domain.imports.source_original_read import SourceOriginals
from domain.ledger.cashflow_read_v2 import load_cash_rows


def contribution_ratios(events, personal_legs, dates, through, personal_total, account_total):
    """Apply signed principal changes on brokerage receipt, including partial contributions."""
    broker_legs = defaultdict(Decimal)
    for event in events:
        if event['trade_type'] in {'deposit', 'withdraw'} and event['trade_date'] <= through:
            key = event['trade_date'], event['trade_type']
            broker_legs[key] += event['net_cash']
    if sum(broker_legs.values()) != account_total or sum(personal_legs.values()) != personal_total:
        return {}
    for key, amount in personal_legs.items():
        if key not in broker_legs or amount * broker_legs[key] <= 0 or abs(amount) > abs(broker_legs[key]):
            return {}
    personal = total = Decimal(0)
    result = {}
    wanted = set(dates)
    for day in sorted(wanted | {day for day, _ in broker_legs}):
        if day > through:
            break
        for kind in ('deposit', 'withdraw'):
            key = day, kind
            total += broker_legs.get(key, 0)
            personal += personal_legs.get(key, 0)
        if total < 0 or not 0 <= personal <= total:
            return {}
        if day in wanted and total > 0:
            result[day] = personal / total
    return result


def load_contribution_ratios(cur, cfg, events, dates):
    """Revalidate the reviewed bank allocations against their original facts and broker totals."""
    try:
        history = cfg['funding_history']
        through = date.fromisoformat(str(history['as_of']))
        personal_total = Decimal(str(cfg['personal_capital']))
        account_total = Decimal(str(cfg['capital_base']))
        if (not personal_total.is_finite() or not account_total.is_finite()
                or history['complete_personal_flows'] is not True or not cfg['funding_source']):
            return {}
        receipts = {str(UUID(source)): date.fromisoformat(str(day))
                    for source, day in history['bank_receipts'].items()}
    except (AttributeError, InvalidOperation, ValueError, KeyError, TypeError):
        return {}
    if not receipts or any(day > through for day in receipts.values()):
        return {}
    cur.execute("""SELECT cashflow_id,source_item_id FROM pams_cashflow_fact
        WHERE account_code=%s AND source_item_id=ANY(%s::uuid[]) ORDER BY cashflow_id""",
        (cfg['funding_source'], list(receipts)))
    refs = cur.fetchall()
    if len(refs) != len(receipts) or {str(r['source_item_id']) for r in refs} != receipts.keys():
        return {}
    originals = SourceOriginals(cur)
    legs = defaultdict(Decimal)
    for ref in refs:
        received = receipts[str(ref['source_item_id'])]
        rows = load_cash_rows(cur, start=date.min, end=through, base_currency='CNY',
                              account_codes=[cfg['funding_source']], fact_id=ref['cashflow_id'], originals=originals)
        if len(rows) != 1 or rows[0]['currency'] != 'CNY' or rows[0]['source_evidence_status'] != 'verified':
            return {}
        row = rows[0]
        amount = -row['amount']
        if amount == 0 or (amount > 0 and received < row['transaction_date']) or (amount < 0 and received > row['transaction_date']):
            return {}
        legs[received, 'deposit' if amount > 0 else 'withdraw'] += amount
    return contribution_ratios(events, legs, dates, through, personal_total, account_total)
