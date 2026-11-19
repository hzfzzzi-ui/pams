from datetime import date
from decimal import Decimal

import pytest

from domain.investments.position_evidence_replay import replay_reviewed_cycle


def inputs():
    common = {'account_code': 'example', 'asset_id': 'asset', 'currency': 'CNY', 'source_evidence_status': 'verified'}
    rows = [{**common, 'trade_id': 1, 'trade_date': date(2025, 12, 12), 'trade_type': 'buy', 'net_cash': Decimal('-50000')},
            {**common, 'trade_id': 2, 'trade_date': date(2026, 3, 24), 'trade_type': 'sell', 'net_cash': Decimal('50322.39')}]
    verified = lambda fields: {'status': 'verified', 'fields': fields}
    proofs = {1: [verified({'quantity': '48118.56', 'valuation_date': '2025-12-31', 'valuation_unit_price': '1.040500',
                           'valuation_observations': [{'date': '2025-12-31', 'cumulative_nav': '1.040500'},
                                                      {'date': '2026-03-20', 'cumulative_nav': '1.045800'}]})],
              2: [verified({'complete_exit': True, 'complete_history': True, 'complete_trade_ids': [1, 2]}),
                  verified({'execution_date': '2026-03-20', 'quantity': '48118.56', 'unit_nav': '1.0458'})]}
    return rows, proofs


def test_reviewed_cash_settlement_keeps_distinct_trade_date_and_estimate_quality():
    rows, proofs = inputs()
    replay = replay_reviewed_cycle(rows, proofs)
    assert replay['buy_quantity'] == Decimal('48118.56')
    assert replay['exit_date'] == date(2026, 3, 20)
    assert replay['prices'][date(2026, 3, 20)] == Decimal('1.0458')
    assert replay['valuation_status'] == 'estimated_cumulative_nav'


@pytest.mark.parametrize('case', ['missing_exit', 'wrong_cash', 'wrong_units', 'unknown_source', 'conflicting_nav', 'conflicting_closure'])
def test_replay_never_invents_or_silently_reconciles_evidence(case):
    rows, proofs = inputs()
    if case == 'missing_exit': proofs[2].pop()
    if case == 'wrong_cash': rows[1]['net_cash'] = Decimal('50300')
    if case == 'wrong_units': proofs[2][1]['fields']['quantity'] = '48118.55'
    if case == 'unknown_source': proofs[1][0]['status'] = 'unknown'
    if case == 'conflicting_nav': proofs[1][0]['fields']['valuation_observations'].append({'date':'2026-03-20', 'cumulative_nav':'1.1'})
    if case == 'conflicting_closure': proofs[1][0]['fields']['valuation_closed_dates'] = ['2026-03-20']
    assert replay_reviewed_cycle(rows, proofs) is None
