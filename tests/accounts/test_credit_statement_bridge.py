from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import os

import pytest

from domain.imports.historical_statement_balance import credit_statement_bridge


def _statement(start, end, balance, rows, tail='1234'):
    return SimpleNamespace(statement_start=date.fromisoformat(start), statement_end=date.fromisoformat(end),
                           statement_balance=D(balance), account_tail=tail, rows=rows)


def _row(transaction, posting, amount):
    return {'transaction_date': date.fromisoformat(transaction), 'posting_date': date.fromisoformat(posting),
            'amount': D(amount), 'currency': 'USD'}


def test_credit_bridge_uses_transaction_day_and_both_posting_cycles():
    first = _statement('2025-12-03', '2026-01-02', '75', [
        _row('2025-12-20', '2025-12-21', '-20'), _row('2026-01-01', '2026-01-02', '-5')])
    later = _statement('2026-01-03', '2026-02-02', '182', [
        _row('2025-12-31', '2026-01-03', '-7'), _row('2026-01-02', '2026-01-03', '-100')])
    args = dict(target=date(2025, 12, 31), currency='USD', first_previous=D('50'), following_previous=D('75'))
    assert credit_statement_bridge(first, later, **args) == D('77')
    with pytest.raises(ValueError, match='不连续'):
        credit_statement_bridge(first, later, **{**args, 'following_previous': D('76')})
    with pytest.raises(ValueError, match='不衔接'):
        credit_statement_bridge(first, later, **{**args, 'first_previous': D('49')})
    later.statement_start = date(2026, 1, 4)
    with pytest.raises(ValueError, match='不连续'):
        credit_statement_bridge(first, later, **args)


def test_statement_pair_publishes_only_verified_balance_evidence(monkeypatch):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import isolation,source_records_v2 as sources,historical_statement_balance as adapter,account_import_statement
    from collectors import bank_statement_parser
    dsn=os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn: pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setattr(account_import_statement,'profiles',lambda:{'test_credit':[{'statement_template':'ccb_credit_pdf'}]})
    monkeypatch.setattr(bank_statement_parser,'parse_statement',lambda path,*a,**k: SimpleNamespace(
        statement_end=date(2026,1,2) if path.read_bytes()==b'first' else date(2026,2,2),
        statement_balance=D('75') if path.read_bytes()==b'first' else D('182')))
    monkeypatch.setattr(adapter,'verify_original_credit_bridge',lambda **_:D('77'))
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_enabled(cur)
                cur.execute("insert into pams_accounts(account_code,display_name,base_currency,is_asset) values ('test_credit','test','USD',false)")
                ids=[]
                for num,content in enumerate((b'first',b'second')):
                    source,item=uuid4(),uuid4(); ids.append(str(source))
                    sources.create_source(cur,source_id=source,account_code='test_credit',source_name='synthetic',acquisition_method='file',document_type='statement',filename='test.pdf',file_format='pdf',content=content)
                    f={'action':'balance','currency':'USD','business_date':('2026-01-02','2026-02-02')[num],'net_cash':('75','182')[num]}
                    sources.add_item(cur,source_id=source,item_id=item,parse_method='template',raw_fields={'source':{'balance_candidate':True},'initial_fields':f},parsed_fields=f,source_position={'row':'statement_balance'})
                    sources.review_item(cur,source_id=source,item_id=item,expected_revision=2,parsed_fields=f,review_status='confirmed')
                with pytest.raises(ValueError): adapter.publish_statement_pair(cur,source_ids=ids,account='test_credit',day=date(2025,12,31),currency='USD',expected='76')
                adapter.publish_statement_pair(cur,source_ids=ids,account='test_credit',day=date(2025,12,31),currency='USD',expected='77')
                cur.execute('select count(*) n from pams_cashflow_fact'); assert cur.fetchone()['n']==0
                cur.execute("select count(*) n from source_records where source_id=any(%s::uuid[]) and status='published'",(ids,)); assert cur.fetchone()['n']==2
        finally: conn.rollback()


@pytest.mark.parametrize('document_type', ['cash', 'statement'])
def test_credit_bridge_publication_preserves_partial_date_and_fx(monkeypatch, document_type):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import isolation, source_records_v2 as sources, holding_publication_v2 as publication
    from domain.imports import historical_statement_balance as adapter
    from domain.investments.position_nav_projection import load_position_nav_estimates

    dsn = os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY', '12' * 32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED', 'true')
    monkeypatch.setattr(adapter, 'verify_original_credit_bridge', lambda **_: D('77'))
    day = date(2025, 12, 31)
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_mode() == 'test' and isolation.source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency,is_asset) VALUES ('test_credit','Test','USD',false)")
                ids = []
                for num, balance in enumerate(('75', '182')):
                    source, item = uuid4(), uuid4()
                    sources.create_source(cur, source_id=source, account_code='test_credit', source_name='Statement',
                        acquisition_method='file', document_type=document_type, filename=f'{num}.pdf', file_format='pdf', content=b'%PDF synthetic'+bytes([num]))
                    fields = {'action':'balance', 'currency':'USD', 'business_date':('2026-01-02','2026-02-02')[num], 'net_cash':balance}
                    raw = {'source':{'balance_candidate':True}, 'initial_fields':fields}
                    sources.add_item(cur, source_id=source, item_id=item, parse_method='template', raw_fields=raw,
                                     parsed_fields=fields, source_position={'row':'statement_balance'})
                    sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='excluded')
                    cur.execute("UPDATE source_records SET status='published',revision=revision+1 WHERE source_id=%s", (str(source),))
                    ids.append(str(item))
                source, item = uuid4(), uuid4()
                sources.create_source(cur, source_id=source, account_code='test_credit', source_name='Credit bridge', acquisition_method='manual', document_type='holding')
                fields = {'asset_type':'cash','currency':'USD','business_date':day.isoformat(),'net_cash':'77',
                          'reconstruction_evidence':{'method':'credit_statement_bridge','statement_source_item_ids':ids}}
                sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'entered_fields':fields}, parsed_fields=fields)
                sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
                plan = publication.preview(cur, source_id=source, revision=3, historical=True)
                assert publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])['coverage_status'] == 'partial'
                values = load_position_nav_estimates(cur, accounts={'test_credit':{}}, dates=[day,date(2026,1,1)],
                    owner_ratio_case_sql=lambda _: ('1::numeric', []), account_owner_ratio=lambda _: D(1), rate_at=lambda *_:D('7'))
                assert values[day]['test_credit'][0] == D('-539')
                assert 'test_credit' not in values.get(date(2026,1,1), {})
                from domain.investments.valuation_nav import _apply_historical_cash_points
                bucket = {day:{'assets':D('1000'), 'liabilities':D('70'), 'net_worth':D('930'),
                               'account_values':{'test_credit':D('-70')}, 'available_accounts':['test_credit']}}
                _apply_historical_cash_points(cur, bank_series=bucket,
                    accounts=[{'account_code':'test_credit','base_currency':'USD','owner_ratio':D(1)}],
                    dates=[day], rate_at=lambda *_:D('7'))
                assert bucket[day]['liabilities'] == D('539')
                assert bucket[day]['net_worth'] == D('461')
                with pytest.raises(ValueError):
                    publication.publish(cur, source_id=source, revision=3, historical=True, preview_token=plan['preview_token'])
        finally:
            conn.rollback()
