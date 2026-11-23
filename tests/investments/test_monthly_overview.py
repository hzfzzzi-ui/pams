from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from domain.investments import monthly_overview
from domain.ledger.ledger_view import _monthly_close


def test_month_end_cutoff_contributions_and_unknown_cash(monkeypatch):
    today = date(2030, 3, 20)
    dates = ['2029-12-31', '2030-01-31', '2030-02-28', '2030-03-19']
    nav = [{'date': day, 'availableNetWorth': value} for day, value in zip(dates, [100, 112, 110, 200])]
    published = {'projection': {}, 'data': {'nav': {'rows': nav},
                                            'total': {'start': dates[0], 'end': dates[-1]}}}
    monkeypatch.setattr(monthly_overview.page_projection, 'read_page', lambda *a, **kw: published)
    def positions(cur, *, monthly_changes, **kwargs):
        monthly_changes[date(2030, 1, 1)]['stock', 'CNY', 'X'] = Decimal('-3')
        monthly_changes[date(2030, 1, 1)]['fund', 'CNY', 'Y'] = Decimal('0')
    monkeypatch.setattr(monthly_overview, 'build_position_return_series', positions)
    ledger = {date(2030, 1, 1): {'totals': {'income': 30, 'expense': 10}},
              date(2030, 2, 1): {'totals': {'income': None, 'expense': 5}}}
    monkeypatch.setattr(monthly_overview, 'build_ledger_workbench_payload', lambda *a, **kw: ledger)
    cur = Mock()
    cur.fetchall.return_value = []
    jan, feb = monthly_overview.build_monthly_overview(
        cur, today=today, owner_ratio=lambda _: 1, excluded_accounts=set())['months']
    assert (jan['delta'], jan['cash'], jan['pnl'], jan['gap']) == (12, 20, -3, -5)
    assert jan['invest']['基金']['parts']['Y'] == 0
    assert jan['partial'] and jan['assets'] is None
    assert feb['date'] == dates[2] and feb['delta'] == -2
    assert feb['cash'] is None and feb['pnl'] is None and feb['gap'] is None


def test_month_end_money_fund_income_is_counted_once_at_personal_share(monkeypatch):
    jan = date(2030, 1, 1)
    feb = date(2030, 2, 1)
    start, end = '2029-12-31', '2030-02-28'
    published = {'projection': {}, 'data': {'nav': {'rows': []}, 'total': {'start': start, 'end': end}}}
    monkeypatch.setattr(monthly_overview.page_projection, 'read_page', lambda *a, **kw: published)

    def positions(cur, *, monthly_changes, counted_income_sources, **kwargs):
        monthly_changes[jan]['stock', 'CNY', 'X'] = Decimal('8')
        counted_income_sources.add('stock-dividend')

    def closes(cur, *, counted_income_sources, **kwargs):
        base = {'account_code': 'shared', 'transaction_date_obj': jan, 'amount': 4, 'amount_cny': 4,
                'classification_status': 'confirmed', 'source_evidence_status': 'verified',
                'category': '投资收益', 'subcategory': '货币基金收益', 'cashflow_type': 'investment_return_cash',
                'flow_type': 'income'}
        rows = [base, {**base, 'flow_type': 'transfer'}, {**base, 'account_code': 'excluded'},
                {**base, 'source_item_id': 'stock-dividend', 'subcategory': '投资分红'},
                {**base, 'cashflow_type': 'salary_income', 'category': '工资薪酬', 'subcategory': '工资'}]
        config = {'shared': {'display_name': '现金管理'}}
        return {
            jan: _monthly_close(rows, [], jan, date(2030, 1, 31), redesigned=True, account_profiles=config,
                                counted_income_sources=counted_income_sources),
            feb: _monthly_close([{**base, 'transaction_date_obj': feb, 'source_evidence_status': 'unknown',
                                  'amount_cny': None}], [], feb, date(2030, 2, 28), redesigned=True,
                                account_profiles=config, counted_income_sources=counted_income_sources),
        }

    monkeypatch.setattr(monthly_overview, 'build_position_return_series', positions)
    monkeypatch.setattr(monthly_overview, 'build_ledger_workbench_payload', closes)
    cur = Mock()
    cur.fetchall.return_value = []
    first, second = monthly_overview.build_monthly_overview(cur, today=date(2030, 3, 10),
        owner_ratio=lambda _: Decimal('0.5'), excluded_accounts={'excluded'})['months']
    assert (first['pnl'], first['inc'], first['exp']) == (10, 4, 0)
    assert first['invest']['货币基金'] == {'total': 2, 'parts': {'现金管理': 2}}
    assert set(first['receipts']) == {'工资薪酬'}
    assert second['pnl'] is None and second['inc'] == 0


def test_cash_interest_uses_the_receipt_date_and_excludes_instrument_and_statement_interest(monkeypatch):
    first = date(2030, 1, 1)
    end = date(2030, 1, 31)
    row = {'trade_date': end, 'account_code': 'shared', 'asset_id': None, 'symbol': 'CASH_CNY',
           'currency': 'CNY', 'source_evidence_status': 'verified', 'net_cash': Decimal(10), 'source_item_id': 'cash'}
    nav = {str(end): {'date': str(end), 'monthly_book': {'accounts': {
        'shared': {'method': 'trade_cash_and_shares'}, 'statement': {'method': 'broker_cash_legs_and_executions'}
    }}}}
    def read(cur, *, account_code, **kwargs):
        assert account_code == 'shared'
        return [row, {**row, 'asset_id': 'bond', 'symbol': 'B'}]
    monkeypatch.setattr(monthly_overview, 'load_trade_rows', read)
    monkeypatch.setattr(monthly_overview, 'position_ownership', lambda *_: {'shared': {end: Decimal('.25')}})
    assert monthly_overview._cash_interest(None, nav, first, end, lambda _: 1, set()) == (
        {first: Decimal('2.50')}, {'cash'}, set())
    row['source_evidence_status'] = 'unknown'
    assert monthly_overview._cash_interest(None, nav, first, end, lambda _: 1, set()) == ({}, set(), {first})
