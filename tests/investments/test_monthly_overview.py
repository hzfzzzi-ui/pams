from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from domain.investments import monthly_overview


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
