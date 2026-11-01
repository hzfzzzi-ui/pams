"""Preserve explicit human review and lifecycle behavior with synthetic facts."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.investments.trade_attribution import (  # noqa: E402
    _annotate_trade_lifecycle,
    _price_performance_metrics,
    build_auto_trade_annotation,
    build_auto_trade_review,
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


def test_price_attribution_stops_at_explicit_exit():
    prices = [{'trade_date': date(2025, 1, 1), 'close': Decimal('10')},
              {'trade_date': date(2025, 1, 2), 'close': Decimal('8')},
              {'trade_date': date(2025, 1, 3), 'close': Decimal('2')}]
    result = _price_performance_metrics(
        prices, trade_date=date(2025, 1, 1), trade_price=Decimal('10'),
        exit_date=date(2025, 1, 2), exit_price=Decimal('9'), attribution_end_reason='position_closed')
    assert result['coverage'] == 'ok'
    assert result['latest_date'] == date(2025, 1, 2)
    assert result['latest_price'] == Decimal('9')
    assert result['current_return_pct'] == Decimal('-10')
    assert result['attribution_end_reason'] == 'position_closed'


def test_missing_price_history_does_not_create_a_return():
    result = _price_performance_metrics([], trade_date=date(2025, 1, 1))
    assert result['coverage'] == 'missing'
    assert result['horizons'] == {}
    assert 'current_return_pct' not in result


def test_closed_stop_loss_is_not_misclassified_as_identity_drift():
    row = {'trade_type': 'sell', 'sell_reason': 'risk_control_stop_loss',
           'initial_position_identity': 'trend', 'current_position_identity': 'closed',
           'review_tags': ['timely_stop_loss']}
    metrics = {'coverage': 'ok', 'current_return_pct': Decimal('-10'),
               'max_adverse_excursion_pct': Decimal('-12'),
               'max_favorable_excursion_pct': Decimal('2'), 'points': 5, 'horizons': {}}
    result = build_auto_trade_review(row, metrics)
    assert result['kind'] == 'stop_loss_effective'
    assert result['needs_human_review'] is False
