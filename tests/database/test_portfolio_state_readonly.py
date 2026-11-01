"""Portfolio scope reads use a server-enforced stable snapshot."""
# ruff: noqa: F811
import psycopg2
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from domain.investments import portfolio_state


def test_portfolio_state_reads_are_readonly_repeatable_read(empty_source_db,monkeypatch):
    observed=[]
    def balances(cur,*args):
        cur.execute('SHOW transaction_isolation'); isolation=cur.fetchone()['transaction_isolation']
        cur.execute('SHOW transaction_read_only'); readonly=cur.fetchone()['transaction_read_only']
        observed.append((isolation,readonly))
        return []
    monkeypatch.setattr(portfolio_state,'_load_accounts_config',lambda:{})
    monkeypatch.setattr(portfolio_state,'load_account_balances',balances)
    monkeypatch.setattr(portfolio_state,'load_investment_positions',lambda *a,**k:[])
    for name in ('load_investment_transactions','load_cash_transactions','load_transfer_matches','load_fx_rates','load_valuation_series'):
        monkeypatch.setattr(portfolio_state,name,lambda *a,**k:[])
    result=portfolio_state.load_portfolio_state_parts(conn_factory=lambda:psycopg2.connect(**empty_source_db))
    assert result['account_balances']==[] and observed==[('repeatable read','on')]
