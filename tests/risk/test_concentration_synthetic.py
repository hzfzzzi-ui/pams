"""Protect retained concentration rules with synthetic positions."""

from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend' / 'api'))
from domain.risk.core.concentration import concentration_exposure  # noqa: E402


def test_industry_weights_split_but_style_exposures_can_overlap():
    positions = pd.DataFrame([{'source_type': 'pams_postgres', 'account_code': 'test',
        'asset_id': 'SYNTH', 'symbol': 'SYNTH', 'asset_name': 'Synthetic', 'asset_type': 'stock',
        'market_value': 100, 'fx_rate_to_cny': 1, 'owner_ratio': 1}])
    mapping = {'symbols': {'SYNTH': {'industry_exposures': {'科技TMT': 0.5, '高端制造': 0.5},
                                     'style_exposures': {'主题A': 1.0, '主题B': 1.0}}},
               'default': {'industry': '未分类', 'style': '未分类'}}
    result = concentration_exposure(positions, 100, mapping)
    assert {x['label']: x['exposure_cny'] for x in result['industry']} == {'科技TMT': 50, '高端制造': 50}
    assert {x['label']: x['exposure_cny'] for x in result['style']} == {'主题A': 100, '主题B': 100}


def test_cash_liability_and_account_center_manual_asset_are_excluded():
    common = {'account_code': 'test', 'fx_rate_to_cny': 1, 'owner_ratio': 1}
    positions = pd.DataFrame([
        {**common, 'source_type': 'pams_postgres', 'asset_id': 'SYNTH', 'symbol': 'SYNTH',
         'asset_name': 'Synthetic', 'asset_type': 'stock', 'market_value': 100},
        {**common, 'source_type': 'manual', 'asset_id': 'CASH_TEST', 'symbol': 'CASH_TEST',
         'asset_name': 'Cash', 'asset_type': 'cash', 'market_value': 100},
        {**common, 'source_type': 'manual', 'asset_id': 'DEBT_TEST', 'symbol': 'DEBT_TEST',
         'asset_name': 'Debt', 'asset_type': 'liability', 'market_value': -30},
        {**common, 'source_type': 'account_center', 'asset_id': 'MANUAL_TEST', 'symbol': 'MANUAL_TEST',
         'asset_name': 'Manual', 'asset_type': 'manual_asset', 'market_value': 200},
    ])
    mapping = {'symbols': {'SYNTH': {'industry': '科技TMT', 'style': '主题A'}},
               'default': {'industry': '未分类', 'style': '未分类'}}
    result = concentration_exposure(positions, 100, mapping)
    assert [(x['label'], x['exposure_cny']) for x in result['industry']] == [('科技TMT', 100)]
    assert [(x['label'], x['exposure_cny']) for x in result['style']] == [('主题A', 100)]
