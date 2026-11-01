"""Preserve explicit human review and lifecycle behavior with synthetic facts."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.investments.trade_attribution import (  # noqa: E402
    _annotate_trade_lifecycle,
    build_auto_trade_annotation,
)


@pytest.mark.parametrize('status', ['reviewed', 'confirmed'])
def test_auto_annotation_does_not_replace_human_review(status):
    row = {'trade_id': 1, 'trade_type': 'buy', 'symbol': 'SYNTH',
           'buy_reason': 'trend_signal', 'initial_position_identity': 'trend',
           'current_position_identity': 'trend', 'review_status': status,
           'review_tags': ['plan_followed'], 'notes': 'human note'}
    payload = {'side': 'buy', 'auto_review': {'suggested_tags': ['early_bottom'],
                                             'needs_human_review': True}}
    result = build_auto_trade_annotation(
        row, payload, learned_defaults={'synth': {'buy_reason': 'fundamental_valuation',
                                                  'current_position_identity': 'value'}})
    assert result['updates'] == {}
    assert row['notes'] == 'human note'


def test_full_sale_closes_only_matching_account_position():
    rows = [
        {'trade_id': 1, 'trade_date': date(2025, 1, 1), 'account_code': 'test-a',
         'symbol': 'SYNTH', 'trade_type': 'buy', 'quantity': Decimal('4')},
        {'trade_id': 2, 'trade_date': date(2025, 1, 2), 'account_code': 'test-a',
         'symbol': 'SYNTH', 'trade_type': 'sell', 'quantity': Decimal('4'),
         'price': Decimal('12'), 'sell_reason': 'risk_control_stop_loss'},
        {'trade_id': 3, 'trade_date': date(2025, 1, 1), 'account_code': 'test-b',
         'symbol': 'SYNTH', 'trade_type': 'buy', 'quantity': Decimal('4')},
    ]
    _annotate_trade_lifecycle(rows)
    assert rows[0]['position_lifecycle_status'] == 'closed'
    assert rows[0]['lifecycle_close_date'] == date(2025, 1, 2)
    assert rows[0]['lifecycle_close_price'] == Decimal('12')
    assert rows[0]['lifecycle_close_reason'] == 'risk_control_stop_loss'
    assert rows[1]['position_lifecycle_status'] == 'closed'
    assert rows[2]['position_lifecycle_status'] == 'open'


def test_partial_sale_keeps_remaining_position_open():
    rows = [
        {'trade_id': 1, 'trade_date': date(2025, 1, 1), 'account_code': 'test',
         'symbol': 'SYNTH', 'trade_type': 'buy', 'quantity': Decimal('4')},
        {'trade_id': 2, 'trade_date': date(2025, 1, 2), 'account_code': 'test',
         'symbol': 'SYNTH', 'trade_type': 'sell', 'quantity': Decimal('1'), 'price': Decimal('12')},
    ]
    _annotate_trade_lifecycle(rows)
    assert rows[0]['position_lifecycle_status'] == 'partially_closed'
    assert rows[1]['position_lifecycle_status'] == 'reduced'
