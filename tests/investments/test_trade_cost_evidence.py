from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/api'))

from domain.investments.trade_cost_evidence import known_trade_charges
from domain.investments.trade_campaigns import build_position_campaigns_from_rows, _summary
from domain.investments.trade_attribution import _trade_fee_drag_cny, _asset_attribution_breakdown
from domain.investments.investment_return_data import _ibkr_trade_flow_events


def test_explicit_total_charges_are_known_without_inventing_tax_component():
    row = {'fee_amount': '5', 'tax_amount': None,
           'raw_json': {'source_row': {'fee_scope': 'total_charges'}}}
    assert known_trade_charges(row) == Decimal('5')
    assert _trade_fee_drag_cny({**row, 'fx_rate_to_cny': '1'}) == Decimal('-5')
    assert known_trade_charges({'fee_amount': '5', 'tax_amount': '0'}) == Decimal('5')
    assert known_trade_charges({'fee_amount': '5', 'tax_amount': '2'}) == Decimal('7')
    assert known_trade_charges({'fee_amount': '5', 'tax_amount': None}) is None


def test_unknown_tax_does_not_create_precise_campaign_or_attribution():
    common = {'account_code': 'acct', 'symbol': 'S', 'currency': 'CNY', 'quantity': '1',
              'gross_amount': '100', 'price': '100', 'fee_amount': '5', 'trade_date': date(2026, 1, 1)}
    rows = [{**common, 'trade_id': 1, 'side': 'buy', 'tax_amount': None},
            {**common, 'trade_id': 2, 'side': 'sell', 'gross_amount': '120', 'tax_amount': '0'}]
    campaign = build_position_campaigns_from_rows(rows)[0]
    assert campaign['status'] == 'closed' and campaign['fee_evidence'] == 'unknown'
    assert campaign['realized_pnl_estimated'] is None and campaign['fees'] is None
    assert campaign['events'][0]['fees'] is None
    assert campaign['lot_matches'][0]['realized_pnl'] is None
    assert _summary([campaign])['realized_pnl_estimated'] is None
    assert _trade_fee_drag_cny(rows[0]) is None
    details = _asset_attribution_breakdown({'fee_drag_cny': None}, {'actual_total_pnl_cny': '100'})
    assert next(x for x in details if x['key'] == 'fees')['value_cny'] is None
    assert next(x for x in details if x['key'] == 'unexplained')['value_cny'] is None


def test_net_cash_can_prove_flow_while_tax_breakdown_is_unknown():
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def execute(self, *args): pass
        def fetchall(self): return self.rows

    day = date(2026, 1, 1)
    base = {'trade_date': day, 'quantity': 1, 'gross_amount': 100, 'fee_amount': 5,
            'tax_amount': None, 'currency': 'HKD'}
    args = dict(start_date=day, end_date=day, base_currency='HKD', fx_timelines={})
    assert _ibkr_trade_flow_events(Cursor([{**base, 'net_cash': -105}]), **args) == [(day, Decimal('105'))]
    assert _ibkr_trade_flow_events(Cursor([{**base, 'net_cash': None}]), **args) == [(day, None)]


def test_section_returns_unknown_if_required_trade_cost_is_missing(monkeypatch):
    from domain.investments import investment_returns as service

    day = date(2026, 1, 1)
    monkeypatch.setattr(service, '_section_first_activity_date', lambda *args: day)
    monkeypatch.setattr(service, 'series_dates', lambda *args: [day])
    monkeypatch.setattr(service, 'load_fx_timelines', lambda *args: {})
    monkeypatch.setattr(service, '_snapshot_category_value_series', lambda *args, **kwargs: {day: Decimal('100')})
    monkeypatch.setattr(service, '_investment_transaction_flow_events', lambda *args, **kwargs: [(day, None)])
    result = service.build_section_return_series_payload(
        object(), section='a_fund', start_date=day, interval='daily', base_currency='CNY', view_as_of=day,
        reported_returns_cfg={}, overview_payload={'accounts': []}, owner_ratio_case_sql=lambda alias: ('1', []),
        account_owner_ratio=lambda account: Decimal('1'), excluded_holding_accounts=set())
    assert result['status'] == 'unavailable' and result['reason'] == 'trade_cost_evidence_missing'
    assert result['summary']['total_return'] is None and result['rows'][0]['net_flow'] is None


def test_published_valuation_does_not_turn_unknown_holding_pnl_into_zero():
    from domain.investments.portfolio_valuation_state import build_portfolio_valuation_state_from_parts
    day = date(2026, 1, 1)
    state = build_portfolio_valuation_state_from_parts(account_balances=[], investment_positions=[{
        'account_code': 'acct', 'asset_id': 'TEST', 'asset_type': 'stock', 'market_value_cny': Decimal('100'),
        'holding_pnl_cny': None, 'as_of': day}])
    assert state['net_liquidation_value'] == Decimal('100')
    assert state['total_pnl_cny'] is None and state['data_completeness'] == 'partial'
    assert state['raw_json']['missing_pnl_positions'] == ['TEST']


def test_incomplete_scope_cannot_publish_a_partial_net_asset_value(monkeypatch):
    import pytest
    from domain.investments import portfolio_valuation_state as service
    day = date(2026, 1, 1)
    state = service.build_portfolio_valuation_state_from_parts(account_balances=[], investment_positions=[{
        'account_code':'broker','asset_id':'stock','asset_type':'stock','market_value_cny':Decimal('100'),
        'holding_pnl_cny':Decimal('10'),'snapshot_scope_status':'partial','as_of':day}])
    assert state['net_liquidation_value'] is None and state['assets_cny'] is None
    assert state['raw_json']['snapshot_scope_unavailable_accounts'] == ['broker']
    monkeypatch.setattr(service, '_table_exists', lambda *args: True)
    monkeypatch.setattr(service, '_build_current_state', lambda *args, **kwargs: state)
    with pytest.raises(ValueError, match='scope is incomplete'):
        service.publish_current_portfolio_valuation_state(object())


def test_summary_retains_unknown_pnl_while_preserving_market_value():
    from domain.investments.portfolio_summary import build_summary2_from_holdings_latest_payload, apply_return_attribution_pnl_to_summary
    holdings = {'as_of': '2026-01-01', 'rows': [
        {'account_code':'a', 'name':'unknown', 'major_category':'A股', 'market_value_cny':'100', 'holding_pnl_cny':None},
        {'account_code':'a', 'name':'known', 'major_category':'A股', 'market_value_cny':'50', 'holding_pnl_cny':'10'}]}
    summary = build_summary2_from_holdings_latest_payload(holdings, base_currency='CNY')
    assert summary['totals']['market_value'] == '150'
    assert summary['totals']['holding_pnl'] is None
    assert summary['by_asset_class'][0]['holding_pnl'] is None
    assert summary['children'][0]['holding_pnl'] is None
    enriched = apply_return_attribution_pnl_to_summary(summary, {'total_pnl_cny':'110'})
    assert enriched['totals']['holding_pnl'] is None and enriched['totals']['current_float_pnl'] is None


def test_partial_account_does_not_create_complete_summary_or_reuse_old_valuation():
    from domain.investments.portfolio_summary import build_summary2_from_holdings_latest_payload, apply_published_valuation_state_to_summary
    holdings = {'as_of':'2026-01-01', 'rows':[
        {'account_code':'broker','name':'stock','major_category':'A股','market_value_cny':'100',
         'holding_pnl_cny':'10','snapshot_scope_status':'partial'}]}
    result = build_summary2_from_holdings_latest_payload(holdings, base_currency='CNY')
    assert result['known_market_value'] == '100' and result['totals']['market_value'] is None
    assert result['totals']['holding_pnl'] is None and result['snapshot_scope_status'] == 'incomplete'
    result = apply_published_valuation_state_to_summary(result, {'net_liquidation_value':'200'})
    assert result['totals']['market_value'] is None and result['valuation_state']['overlay_applied'] is False


def test_attribution_does_not_turn_unknown_pnl_into_a_complete_zero():
    from domain.investments.return_attribution import build_return_attribution_from_positions
    result = build_return_attribution_from_positions([
        {'asset_id':'unknown', 'market_value_cny':100, 'holding_pnl_cny':None},
        {'asset_id':'known', 'market_value_cny':50, 'holding_pnl_cny':10}])
    assert result['status'] == 'unavailable' and result['total_pnl_cny'] is None
    assert result['total_market_value_cny'] == 150 and result['quality']['missing_pnl_positions'] == ['unknown']


def test_risk_does_not_compute_ratios_from_incomplete_account_snapshot(monkeypatch):
    from domain.risk import risk_state
    monkeypatch.setattr(risk_state, 'load_risk_limits', lambda: {})
    monkeypatch.setattr(risk_state, 'load_factor_map', lambda: {})
    monkeypatch.setattr(risk_state, 'load_concentration_map', lambda: {})
    monkeypatch.setattr(risk_state, 'build_portfolio_state', lambda **kwargs: {
        'snapshot_scope_status':'incomplete', 'snapshot_scope_unavailable_accounts':['broker'],
        'account_balances':[{'account_code':'broker','account_type':'brokerage','amount':None,'include_in_nav':True}],
        'investment_positions':[{'account_code':'broker','asset_name':'known','market_value':100,
                                 'market_value_cny':100,'cost_cny':None}], 'fx_rates':[]})
    result = risk_state.build_risk_state()
    assert result['status'] == 'unavailable' and result['reason'] == 'snapshot_scope_incomplete'
    assert result['metrics']['total_nav'] is None and result['holdings'][0]['market_value_cny'] == 100
    assert result['holdings'][0]['cost_cny'] is None


def test_attribution_rejects_incomplete_account_snapshot(monkeypatch):
    from domain.investments import return_attribution as service
    monkeypatch.setattr(service, '_load_accounts_config', lambda: {})
    monkeypatch.setattr(service, 'load_account_balances', lambda *args: [
        {'account_code':'broker', 'snapshot_scope_status':'partial', 'include_in_nav':True}])
    monkeypatch.setattr(service, 'load_investment_positions', lambda *args, **kwargs: [])
    monkeypatch.setattr(service, '_latest_snapshot_date', lambda *args: date(2026, 1, 1))
    monkeypatch.setattr(service, 'build_holdings_latest_payload', lambda *args, **kwargs: {'rows':[], 'as_of':'2026-01-01'})
    result = service.build_return_attribution_payload(object())
    assert result['status'] == 'unavailable' and result['reason'] == 'snapshot_scope_incomplete'
    assert result['portfolio_nav_cny'] is None and result['total_pnl_cny'] is None


def test_missing_account_balance_is_unknown_instead_of_zero(monkeypatch):
    from domain.investments import portfolio_state as service
    monkeypatch.setattr(service, '_latest_fx_rates', lambda *args: {})
    monkeypatch.setattr(service, '_statement_balances', lambda *args: {})
    monkeypatch.setattr(service, '_platform_balances', lambda *args: {})
    monkeypatch.setattr(service, '_manual_balance', lambda *args: None)
    monkeypatch.setattr(service, '_apply_daily_close_balance', lambda *args, **kwargs: None)
    cfg = {'accounts': {'funding': {'account_type':'funding', 'include_in_nav':True},
                        'excluded': {'account_type':'funding', 'include_in_nav':False}}}
    rows = {row['account_code']:row for row in service.load_account_balances(object(), 'CNY', cfg)}
    assert rows['funding']['amount'] is None and rows['funding']['amount_cny'] is None
    assert rows['funding']['snapshot_scope_status'] == 'unknown'
    state = service.build_portfolio_state_from_parts(as_of='2026-01-01', base_currency='CNY',
        account_balances=list(rows.values()), investment_positions=[])
    assert state['risk_snapshot']['total_nav'] is None and state['snapshot_scope_unavailable_accounts'] == ['funding']


def test_ibkr_charges_require_an_explicit_matching_currency_for_nonzero_fee():
    base={'source_table':'ibkr_flex_trade','currency':'USD','fee_amount':'5','tax_amount':'2'}
    assert known_trade_charges({**base,'fee_currency':None}) is None
    assert known_trade_charges({**base,'fee_currency':'EUR'}) is None
    assert known_trade_charges({**base,'fee_currency':'USD'})==Decimal('7')
    assert known_trade_charges({**base,'fee_amount':'0','fee_currency':None})==Decimal('2')
    assert known_trade_charges({**base,'tax_amount':None,'fee_currency':'USD'}) is None


def test_unconverted_ibkr_commission_keeps_trade_cash_but_cost_and_returns_unknown():
    class Cursor:
        def execute(self,*args): pass
        def fetchall(self): return [row]
    day=date(2026,1,1)
    row={'trade_id':1,'account_code':'ibkr','symbol':'S','currency':'USD','quantity':1,
         'gross_amount':100,'fee_amount':5,'tax_amount':0,'net_cash':-105,'trade_date':day,
         'side':'buy','source_table':'ibkr_flex_trade','fee_currency':'EUR'}
    assert _ibkr_trade_flow_events(Cursor(),start_date=day,end_date=day+timedelta(days=1),base_currency='USD',fx_timelines={})==[(day+timedelta(days=1),None)]
    campaign=build_position_campaigns_from_rows([row])[0]
    assert campaign['fee_evidence']=='unknown' and campaign['total_buy_cost'] is None
    assert _trade_fee_drag_cny({**row,'fx_rate_to_cny':7}) is None
    assert row['net_cash']==-105
