from datetime import date, timedelta
from decimal import Decimal

from domain.accounts import account_history as history


def test_card_bill_unbilled_principal_and_credit_balance(monkeypatch):
    first = date(2026, 1, 1)
    days = [first + timedelta(days=n) for n in range(5)]
    code = 'card'
    monkeypatch.setattr(history, 'statement_balance_history', lambda *a, **k: {code: [
        {'amount': '-80', 'as_of': days[0]}, {'amount': '55', 'as_of': days[2]}]})
    monkeypatch.setattr(history, 'load_statement_period_points', lambda *a: {})
    principal = {('负债', 'USD', 'loan'): (Decimal(25), first, first)}
    monkeypatch.setattr(history, 'load_unbilled_principal',
                        lambda *a: {(day, code): principal for day in days})
    holdings = {days[1]: {'net_value': Decimal(-90), 'net_value_status': 'confirmed'}}
    monkeypatch.setattr(history, '_holding_observations', lambda *a, **k: holdings)
    cfg = {'accounts': {code: {'account_type': 'liability', 'base_currency': 'USD'}}}
    result = history.read_account_history(None, account_code=code, account_config=cfg, start_date=first, end_date=days[-1])
    assert result['series'] == ['liabilities']
    assert [(p['date'], p['liabilities']) for p in result['rows']] == [
        (days[0], Decimal(105)), (days[1], Decimal(90)), (days[2], 0)]
    assert [p['liabilities_status'] for p in result['rows']] == ['confirmed', 'partial', 'confirmed']


def test_cash_uses_only_reconciled_anchored_intervals(monkeypatch):
    first = date(2025, 12, 31)
    movements = [first + timedelta(days=n) for n in range(1, 5)]
    rows = [{'transaction_date': day, 'source_evidence_status': 'verified', 'currency': 'CNY', 'amount': Decimal(10)}
            for day in movements]
    monkeypatch.setattr(history, 'load_cash_rows', lambda *a, **kw: rows)
    anchors = {first: Decimal(30), movements[1]: Decimal(50), movements[3]: Decimal(75)}
    assert history._reconciled_cash_movements(None, 'fund', 'CNY', anchors) == {
        movements[0]: Decimal(40), movements[1]: Decimal(50)}
    rows[0]['source_evidence_status'] = 'unknown'
    assert history._reconciled_cash_movements(None, 'fund', 'CNY', anchors) == {}


def test_reference_balance_can_bound_cash_movements_but_principal_cannot(monkeypatch):
    days = [date(2026, 1, day) for day in range(1, 4)]
    reference = {'source': 'archive', 'currency': 'CNY', 'amount': '40', 'as_of': days[0]}
    cfg = {'accounts': {'benefit': {'base_currency': 'CNY', 'account_balance_reference': reference}}}
    monkeypatch.setattr(history, 'statement_balance_history', lambda *a, **k: {})
    monkeypatch.setattr(history, 'load_statement_period_points', lambda *a: {})
    monkeypatch.setattr(history, 'load_bound_balance_references', lambda *a: {})
    monkeypatch.setattr(history, 'load_account_totals', lambda *a, **k: {})
    monkeypatch.setattr(history, '_holding_observations', lambda *a, **k: {
        days[-1]: {'net_value': Decimal(50), 'net_value_status': 'confirmed'}})
    monkeypatch.setattr(history, 'load_cash_rows', lambda *a, **k: [{
        'transaction_date': days[1], 'currency': 'CNY', 'source_evidence_status': 'verified', 'amount': Decimal(10)}])

    def read():
        return history.read_account_history(None, account_code='benefit', account_config=cfg,
                                            start_date=days[1], end_date=days[-1])

    result = read()
    assert result['last_prior_date'] == days[0]
    assert [(r['date'], r['net_value'], r['net_value_status']) for r in result['rows']] == [
        (days[1], 50, 'estimated'), (days[2], 50, 'confirmed')]
    reference.update(amount='39')
    assert [r['date'] for r in read()['rows']] == [days[2]]
    reference.update(amount='40', reference_basis='contributed_principal')
    assert [r['date'] for r in read()['rows']] == [days[2]]


def test_account_total_and_liability_have_independent_scope(monkeypatch):
    day = date(2026, 1, 2)
    prior = day - timedelta(days=1)
    code = 'equity'
    cfg = {'accounts': {code: {'account_type': 'brokerage', 'reconcile_method': 'position_nav',
                              'latest_position_evidence': 'recorded_equity_and_debt'}}}
    monkeypatch.setattr(history, '_holding_observations', lambda *a, **k: {
        prior: {'liabilities': Decimal('20'), 'liabilities_status': 'partial'}})
    monkeypatch.setattr(history, '_manual_position_value', lambda *a: None)
    monkeypatch.setattr(history, 'load_account_totals', lambda *a, **k: {
        (day, code): {'net_asset': '130', 'currency': 'CNY', 'covered_account_codes': [code]},
        (prior, code): {'net_asset': '250', 'currency': 'CNY', 'covered_account_codes': [code, 'another']}})
    payload = history.read_account_history(None, account_code=code, account_config=cfg, start_date=prior, end_date=day)
    assert payload['series'] == ['net_value', 'liabilities']
    assert [(p['net_value'], p['liabilities']) for p in payload['rows']] == [(None, Decimal(20)), (Decimal(130), None)]


def test_fund_single_member_update_cannot_replace_whole_account(monkeypatch):
    from domain.investments.nav_period_comparison import DisplayContributions
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(4)]
    samples = {}
    for day, value, members in zip(days, (100, 103, 20, 101), (['A', 'B'], ['A', 'B'], ['B'], ['A', 'B'])):
        sample = DisplayContributions()
        sample['fund'] = (Decimal(value), day, day)
        sample.basis['fund'] = ['position_members', members]
        samples[day] = sample
    monkeypatch.setattr(history, 'load_fx_timelines', lambda *a: {})
    monkeypatch.setattr(history, 'load_position_nav_estimates', lambda *a, **k: samples)
    points = {start: {'net_value': Decimal(100), 'net_value_status':'confirmed'}}
    history._position_estimates(None, 'fund', {'base_currency':'CNY'}, days, points)
    assert [(day, row['net_value']) for day, row in points.items()] == [(start, 100), (days[1], 103)]


def test_dated_fund_reference_is_a_standalone_fallback(monkeypatch):
    monkeypatch.setattr(history, 'prior_single_account_total', lambda *a: None)
    day = date(2026, 1, 2)
    cfg = {'accounts': {'fund': {'account_type':'fund_platform', 'truth_source':'manual',
        'base_currency':'CNY', 'reconcile_method':'position_nav', 'manual_balance': {
            'as_of': day, 'currency':'CNY', 'amount':'71.80', 'source':'confirmed fund holding'}}}}
    monkeypatch.setattr(history, '_holding_observations', lambda *a, **k: {})
    monkeypatch.setattr(history, 'load_account_totals', lambda *a, **k: {})
    def result():
        return history.read_account_history(None, account_code='fund', account_config=cfg,
                                            start_date=day, end_date=day + timedelta(days=1))
    assert result()['rows'] == [{'date':day, 'net_value':Decimal('71.80'), 'net_value_status':'partial'}]
    cfg['accounts']['fund']['manual_balance']['as_of'] = 'unknown'
    assert result()['rows'] == []
