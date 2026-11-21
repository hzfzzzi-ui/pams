"""Expose verified borrowing movements without assuming an opening principal balance."""
from collections import defaultdict
from bisect import bisect_right
from datetime import date
from decimal import Decimal

from domain.investments.snapshot_coverage import load_account_snapshot_coverage
from domain.ledger.cashflow_classification import PERSONAL_LOAN_CATEGORY, personal_loan_principal
from domain.ledger.cashflow_read_v2 import load_cash_rows


def _principal_anchors(cur, codes, through):
    # A complete credit-card or personal receivable balance cannot certify
    # all privately borrowed principal. Require that explicit attestation.
    cur.execute("""SELECT v.account_code,v.as_of,v.version_id,h.market_value,h.currency,h.id,
            v.scope_attestation->'point_in_time_only'='true'::jsonb AS point_in_time_only
        FROM account_snapshot_version v JOIN account_snapshot_member m ON m.version_id=v.version_id
        JOIN pams_holdings_snapshot h ON h.id=m.holding_id JOIN pams_accounts a ON a.account_code=v.account_code
        WHERE v.as_of<=%s AND NOT a.is_asset AND (a.active OR v.account_code=ANY(%s))
          AND v.coverage_status='complete' AND h.asset_liability='负债' AND h.major_category='现金'
          AND v.scope_attestation->>'liability_scope'='all_personal_loan_principal'""", (through,sorted(codes)))
    records = cur.fetchall()
    coverage = load_account_snapshot_coverage(cur, {(r['account_code'], r['as_of']) for r in records})
    by_day = defaultdict(list)
    for record in records:
        proof = coverage[record['account_code'], record['as_of']]
        if (proof['version_id'] == str(record['version_id']) and proof['status'] == 'complete'
                and proof['member_ids'] == {record['id']}):
            by_day[record['as_of']].append(record)
    anchors = {}
    for day, proofs in by_day.items():
        if len(proofs) == 1:
            proof = proofs[0]
            if proof['account_code'] in codes:
                anchors[day] = proof
            elif proof['market_value'] == 0 and proof['point_in_time_only']:
                # A dated declaration that all borrowing is zero requires no
                # monetary contribution from its source account to this NAV.
                anchors[day] = {**proof, 'scope_only_zero': True}
    return anchors


def annotate_personal_loans(rows, facts, anchors, value_in_base):
    movements = sorted((r for r in facts if personal_loan_principal(r)), key=lambda r: r['transaction_date'])
    if not movements and not anchors:
        return
    running = defaultdict(lambda: defaultdict(Decimal))
    anchor_dates = sorted(anchors)
    index = 0
    for row in rows:
        day = row['date']
        activity = defaultdict(lambda: {'borrowed': Decimal(0), 'repaid': Decimal(0)})
        while index < len(movements) and movements[index]['transaction_date'] <= day:
            fact = movements[index]
            code, currency, amount = fact['account_code'], fact['currency'], fact['amount']
            running[code][currency] += amount
            if fact['transaction_date'] == day:
                activity[currency]['borrowed' if amount > 0 else 'repaid'] += abs(amount)
            index += 1
        changes = [dict(currency=currency, **{k: str(v) for k, v in activity[currency].items()})
                   for currency in sorted(activity)]
        proof = anchors.get(day)
        if index == 0 and not proof:
            continue
        prior = bisect_right(anchor_dates, day) - 1
        if not proof and prior >= 0:
            last_anchor = anchors[anchor_dates[prior]]
            if (not last_anchor.get('point_in_time_only') and last_anchor['market_value'] == 0
                    and movements[index - 1]['transaction_date'] <= anchor_dates[prior]):
                # A confirmed payoff closes the earlier borrowing warning. It
                # does not provide a carried zero balance for this later day.
                continue
        if proof:
            amount = proof['market_value']
            observed = row.get('_account_contributions', {}).get(proof['account_code'])
            value = value_in_base(proof)
            confirmed_zero = proof.get('scope_only_zero') and amount == 0 and observed is None
            if value is not None and (observed == -value or confirmed_zero and value == 0):
                row['personal_loan_coverage'] = dict(status='confirmed', balance=str(amount),
                                                     currency=proof['currency'], day_movements=changes)
                continue
        row['personal_loan_coverage'] = dict(status='principal_movements_only', balance=None, day_movements=changes)
        # The bank cash is still real. Its movement by itself cannot establish
        # a change in net worth when the corresponding liability is unvalued.
        row['_unanchored_loan_changes'] = {
            code: {unit: str(amount.normalize()) for unit, amount in totals.items() if amount}
            for code, totals in running.items() if any(totals.values())
        }
        if row.get('net_worth') is not None:
            row['available_net_worth'] = row['net_worth']
            row['net_worth'] = None
        row['liabilities'] = None
        row['scope_status'] = 'incomplete'


def apply_personal_loan_coverage(cur, rows, codes, value_in_base):
    if not rows or not codes:
        return
    cur.execute("""SELECT to_regclass('public.source_records') IS NOT NULL AND
        to_regclass('public.cashflow_classification') IS NOT NULL AS facts,
        to_regclass('public.account_snapshot_version') IS NOT NULL AND
        to_regclass('public.account_snapshot_member') IS NOT NULL AS anchors""")
    schema = cur.fetchone()
    if not schema['facts']:
        return
    days = [row['date'] for row in rows]
    cur.execute("""SELECT c.cashflow_id FROM cashflow_classification c JOIN pams_cashflow_fact f USING(cashflow_id)
        WHERE c.category=%s AND c.status='confirmed' AND c.classification_source='manual'
          AND f.account_code=ANY(%s) AND f.event_date<=%s""", (PERSONAL_LOAN_CATEGORY, sorted(codes), max(days)))
    selected = [r['cashflow_id'] for r in cur.fetchall()]
    facts = load_cash_rows(cur, start=date.min, end=max(days), base_currency='CNY', fact_ids=selected) if selected else []
    anchors = _principal_anchors(cur, codes, max(days)) if schema['anchors'] else {}
    annotate_personal_loans(rows, facts, anchors, value_in_base)
