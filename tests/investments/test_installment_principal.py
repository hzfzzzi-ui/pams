from datetime import date
from decimal import Decimal as D

from domain.investments.trades.installment_principal import _principal_steps
from domain.accounts.dated_valuation import AccountValuation
from domain.investments.valuation.valuation_nav import _apply_historical_cash_points


def test_same_day_funding_and_verified_principal_exclude_billed_amounts_and_interest():
    start, last = date(2025, 5, 2), date(2025, 6, 2)
    order = '123-ABC'
    def flow(account, amount, day):
        return dict(account_code=account, amount=D(amount), currency='CNY', transaction_date=day,
                    posting_date=day, description=f'订单 {order}')

    facts = {1:flow('receiving', 100, start), 2:flow('card', -40, start), 3:flow('card', -5, start),
             4:flow('card', 5, start), 5:flow('card', -20, last), 6:flow('card', -2, last)}
    postings = [dict(period=1, date=start.isoformat(), principal='40', net_interest='0', principal_fact=2, fact_ids=[2,3,4]),
                dict(period=2, date=last.isoformat(), principal='20', net_interest='2', principal_fact=5, fact_ids=[5,6])]
    raw = dict(contract_principal='100', unbilled_principal='40', bank_funding_fact=1, verified_postings=postings,
               contract_periods=4, paid_periods_user_confirmed=2, order_number=order)
    fields = dict(order_number=order, asset_type='liability', currency='CNY', symbol='loan',
                  business_date=last.isoformat(), market_value='40')
    row = dict(account_code='card', as_of=last, raw_fields=raw, parsed_fields=fields,
               currency='CNY', market_value=D(40), symbol='loan')
    assert _principal_steps(row, facts) == {start:D(60), last:D(40)}
    assert _principal_steps(row, {**facts, 5:{**facts[5], 'description':f'{order}1'}}) is None
    assert _principal_steps(row, {i:f for i,f in facts.items() if i!=4}) is None
    assert _principal_steps(row, {**facts, 5:{**facts[5], 'posting_date':start}}) is None
    assert _principal_steps({**row, 'market_value':D(43)}, facts) is None


def test_card_statement_and_unbilled_contract_are_disjoint_and_partial():
    day = date(2025, 5, 2)
    code = 'card'
    row = {'assets':D(0), 'liabilities':D(0), 'net_worth':D(0)}
    args = dict(bank_series={day:row}, accounts=[{'account_code':code,'base_currency':'CNY'}],
                dates=[day], rate_at=lambda *_:D(1), points={},
                periods={(code,day):AccountValuation(day,'CNY',D(-42),'statement','billed')},
                unbilled={(day,code):{('负债','CNY','symbol:loan'):(D(60),day,day)}})
    _apply_historical_cash_points(None, **args)
    assert row['account_values'] == {code:D(-102)}
    assert row['liabilities'] == 102 and row['unavailable_accounts'] == [code]
    _apply_historical_cash_points(None, **args)
    assert row['net_worth'] == -102
    # A generic liability observation may already contain the contract.
    point = {'currency':'CNY', 'asset_liability':'负债', 'market_value':D(102)}
    _apply_historical_cash_points(None, **{**args, 'periods':{}, 'points':{(code,day):point}})
    assert row['net_worth'] == -102
