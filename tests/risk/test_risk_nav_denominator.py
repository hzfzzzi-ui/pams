"""Synthetic NAV differences cannot become invented cash or green ratios."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))
from domain.risk import risk_state  # noqa: E402
from domain.risk.core import risk_engine  # noqa: E402
from domain.risk.portfolio_config import build_portfolio_config_payload_from_inputs  # noqa: E402
from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload_from_inputs  # noqa: E402


def _positions():
    return pd.DataFrame([{'as_of': '2026-06-18', 'account_code': 'synthetic', 'account_name': 'Synthetic',
                          'asset_id': 'TEST', 'asset_name': 'Test', 'asset_type': 'us_stock',
                          'currency': 'CNY', 'market_value_cny': 100.0}])


def _valuation(nav):
    return {'valuation_date': date(2026, 6, 18), 'status': 'published', 'data_completeness': 'complete',
            'net_liquidation_value': nav, 'assets_cny': 100, 'liabilities_cny': 0}


@pytest.mark.parametrize('nav', [0.0, -50.0])
def test_nonpositive_nav_ratios_are_unknown_and_alert_is_red(nav):
    positions = _positions()
    limits = {'limits': {'gross_exposure': {'yellow': 0.8, 'red': 1.0}}}
    assert risk_engine.gross_exposure(positions, limits, nav_override=nav)['gross_exposure_ratio'] is None
    assert risk_engine.negative_cash_ratio(positions, limits, nav_override=nav)['negative_cash_ratio'] is None
    assert pd.isna(risk_engine.account_weight(positions, limits, nav_override=nav).iloc[0]['account_weight'])
    assert pd.isna(risk_engine.position_weight(positions, limits, nav_override=nav).iloc[0]['position_weight'])
    factors = {'symbols': {'TEST': {'SYNTHETIC': 1.0}}}
    factor_rows = risk_engine.factor_exposure(positions, factors, limits, nav_override=nav)
    assert not factor_rows.empty and pd.isna(factor_rows.iloc[0]['exposure_ratio'])
    stress_rows = risk_engine.stress_loss(positions, factors, {'test': {'shocks': {'SYNTHETIC': -0.1}}},
                                          limits, nav_override=nav)
    assert pd.isna(stress_rows.iloc[0]['loss_ratio'])
    alerts = risk_engine.risk_alerts(positions, limits, {}, nav_override=nav)
    assert len(alerts) == 1
    assert alerts[0]['rule'] == 'total_nav' and alerts[0]['level'] == 'red' and alerts[0]['metric'] == nav


@pytest.mark.parametrize('nav', [0.0, -50.0])
def test_nonpositive_published_nav_is_unavailable_in_risk_response(monkeypatch, nav):
    monkeypatch.setattr(risk_state, 'load_risk_limits', lambda: {})
    monkeypatch.setattr(risk_state, 'load_factor_map', lambda: {})
    monkeypatch.setattr(risk_state, 'load_concentration_map', lambda: {})
    monkeypatch.setattr(risk_state, 'load_latest_positions', _positions)
    monkeypatch.setattr(risk_state, '_latest_published_valuation_state', lambda *args: _valuation(nav))
    result = risk_state.build_risk_state()
    assert result['status'] == 'unavailable' and result['reason'] == 'nonpositive_nav'
    assert result['metrics']['total_nav'] == nav
    assert result['metrics']['gross_exposure_ratio'] is None
    assert result['metrics']['negative_cash_ratio'] is None
    assert result['alerts'][0]['level'] == 'red'
    assert not any(alert['level'] == 'green' for alert in result['alerts'])
    assert len(result['holdings']) == 1 and result['holdings'][0]['asset_id'] == 'TEST'


@pytest.mark.parametrize('nav', [50.0, 150.0])
def test_unexplained_difference_is_reviewed_without_synthetic_holding(monkeypatch, nav):
    monkeypatch.setattr(risk_state, 'load_risk_limits', lambda: {'stress_tests': {'synthetic': {'shocks': {}}}})
    monkeypatch.setattr(risk_state, 'load_factor_map', lambda: {})
    monkeypatch.setattr(risk_state, 'load_concentration_map', lambda: {})
    monkeypatch.setattr(risk_state, 'load_latest_positions', _positions)
    monkeypatch.setattr(risk_state, '_latest_published_valuation_state', lambda *args: _valuation(nav))
    monkeypatch.setattr(risk_state, '_load_db_industry_lookup', lambda positions: {})
    monkeypatch.setattr(risk_state, '_load_latest_exposure_payload', lambda *args: None)
    monkeypatch.setattr(risk_state, '_latest_ibkr_performance', lambda: None)
    result = risk_state.build_risk_state()
    assert result['status'] == 'review'
    assert result['metrics']['total_nav'] == nav
    assert result['metrics']['gross_exposure_ratio'] == pytest.approx(100 / nav)
    assert result['valuation_state']['nav_reconciliation_cny'] == nav - 100
    assert all(row.get('asset_id') != 'VALUATION_STATE_ADJUSTMENT' for row in result['positions'])
    assert any(task['id'] == 'data_check:valuation_unexplained' for task in result['risk_tasks']['data_check'])


@pytest.mark.parametrize('nav', [0.0, -50.0])
def test_unavailable_nav_is_not_replaced_by_gross_lookthrough_in_consumers(nav):
    risk = {'status': 'unavailable', 'reason': 'nonpositive_nav', 'metrics': {'total_nav': nav},
            'alerts': [{'rule': 'total_nav', 'level': 'red', 'metric': nav}]}
    lookthrough = {'total_market_value_cny': 100, 'as_of': '2026-06-18', 'exposures': {}}
    configuration = build_portfolio_config_payload_from_inputs(risk, lookthrough)
    assert configuration['status'] == 'unavailable'
    assert configuration['rules'] == [] and configuration['rebalance'] == []
    disclosure = build_portfolio_risk_disclosure_payload_from_inputs(risk, lookthrough)
    assert disclosure['status'] == 'unavailable' and disclosure['nav_cny'] == nav
    assert disclosure['nav_cny'] != lookthrough['total_market_value_cny']
    assert disclosure['dashboard']['status_tone'] == 'risk'
    assert disclosure['dashboard']['score_label'] == '--'
    assert disclosure['categories'] == []
