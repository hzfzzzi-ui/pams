"""An account's statement opening is calculated from its own continuous balance rows."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from domain.imports.historical_statement_balance import statement_opening_balance


def _report():
    return SimpleNamespace(statement_start=date(2026,1,1),statement_end=date(2026,5,31),account_tail='1234',rows=[
        {'transaction_date':date(2026,1,6),'currency':'CNY','amount':Decimal('-10'),'balance':Decimal('90'),'raw':{'transaction_time':'08:00:00'}},
        {'transaction_date':date(2026,1,13),'currency':'CNY','amount':Decimal('-10'),'balance':Decimal('80'),'raw':{'transaction_time':'08:00:00'}},
        {'transaction_date':date(2026,3,21),'currency':'CNY','amount':Decimal('1'),'balance':Decimal('81'),'raw':{'transaction_time':'08:00:00'}},
    ])


def test_statement_opening_requires_identity_period_and_continuous_balances():
    report=_report()
    args={'target':date(2025,12,31),'currency':'CNY','account_tail':'1234'}
    assert statement_opening_balance(report,**args)==Decimal('100')
    report.rows[1]['balance']=Decimal('79')
    with pytest.raises(ValueError,match='衔接'):
        statement_opening_balance(report,**args)
    with pytest.raises(ValueError,match='账户或覆盖期'):
        statement_opening_balance(_report(),**{**args,'account_tail':'9999'})
    with pytest.raises(ValueError,match='账户或覆盖期'):
        statement_opening_balance(_report(),**{**args,'target':date(2025,12,30)})


def test_historical_cash_can_bind_original_statement_in_isolation(monkeypatch):
    import os
    from uuid import uuid4
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from domain.imports import source_records_v2 as sources,holding_publication_v2 as publication, isolation
    from domain.imports import historical_statement_balance as adapter
    from domain.investments.position_nav_projection import load_position_nav_estimates
    dsn=os.getenv('PAMS_TEST_POSTGRES_DSN')
    if not dsn: pytest.skip('requires explicitly isolated PostgreSQL')
    monkeypatch.setenv('PAMS_SOURCE_TEST_TOKEN_KEY','12'*32)
    monkeypatch.setenv('PAMS_IMPORT_PUBLISH_ENABLED','true')
    monkeypatch.setattr(adapter,'verify_original_opening',lambda **kw:statement_opening_balance(_report(),target=kw['day'],currency=kw['currency'],account_tail='1234'))
    with psycopg2.connect(dsn) as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                assert isolation.source_mode() == 'test' and isolation.source_enabled(cur)
                cur.execute("INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES ('bank_test','test','CNY')")
                source,item=uuid4(),uuid4()
                sources.create_source(cur,source_id=source,account_code='bank_test',source_name='statement',acquisition_method='file',document_type='holding',filename='synthetic.pdf',file_format='pdf',content=b'%PDF synthetic')
                fields={'business_date':'2025-12-31','asset_type':'cash','currency':'CNY','net_cash':'100','reconstruction_evidence':{'method':'original_statement_opening'}}
                sources.add_item(cur,source_id=source,item_id=item,parse_method='manual',raw_fields={'entered_fields':fields},parsed_fields=fields)
                sources.review_item(cur,source_id=source,item_id=item,expected_revision=2,parsed_fields=fields,review_status='confirmed')
                plan=publication.preview(cur,source_id=source,revision=3,historical=True)
                assert publication.publish(cur,source_id=source,revision=3,preview_token=plan['preview_token'],historical=True)['coverage_status']=='partial'
                args=dict(accounts={'bank_test':{}},dates=[date(2025,12,31),date(2026,1,1)],owner_ratio_case_sql=lambda _:('1::numeric',[]),account_owner_ratio=lambda _:Decimal(1),rate_at=lambda *_:Decimal(1))
                result=load_position_nav_estimates(cur,**args)
                assert result[date(2025,12,31)]['bank_test'][0]==Decimal('100')
                assert 'bank_test' not in result.get(date(2026,1,1),{})
                with pytest.raises(ValueError):
                    publication.publish(cur,source_id=source,revision=3,preview_token=plan['preview_token'],historical=True)
        finally:
            conn.rollback()
