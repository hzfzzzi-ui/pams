"""New-source cash deltas use verified facts and dated, sourced FX."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.imports.test_redesigned_fact_schema import install_redesign
from tests.imports.test_source_record_http import SchemaCursor
from tests.support.account_import_support import scratch_database


def test_account_cash_delta_with_redesigned_evidence():
    from domain.accounts.account_balance import _cash_delta_cny, _cash_source_schema_available
    from domain.imports import source_records_v2 as source
    from domain.imports.source_fact_evidence import verify_native_fact

    class Cursor(SchemaCursor):
        @property
        def description(self):
            return self.inner.description

    with scratch_database() as params, psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        install_redesign(cur)
        cur.execute('SET LOCAL search_path TO redesign_test,public')
        cur.execute('CREATE TABLE redesign_test.pams_fx_rates_daily(base_currency text,quote_currency text,as_of date,rate numeric,source text)')
        wrapped = Cursor(cur)
        source_id = uuid4()
        source.create_source(wrapped, source_id=source_id, account_code='bank-a', source_name='synthetic',
                             acquisition_method='manual', document_type='trade')
        facts = [({'business_date':'2026-01-02','action':'buy','currency':'CNY','net_cash':'-100.00'}, 'buy', '-100.00'),
                 ({'business_date':'2026-01-02','action':'deposit','currency':'USD','net_cash':'10.00'}, 'deposit', '10.00')]
        for fields, action, amount in facts:
            item_id = uuid4()
            source.add_item(wrapped, source_id=source_id, item_id=item_id, parse_method='manual',
                            raw_fields={'entered_fields': fields}, parsed_fields=fields)
            cur.execute("UPDATE redesign_test.source_items SET review_status='confirmed' WHERE source_item_id=%s", (str(item_id),))
            cur.execute('''INSERT INTO redesign_test.pams_investment_trade_fact
                (account_code,trade_date,trade_type,side,currency,net_cash,source_item_id,fact_role)
                VALUES ('bank-a','2026-01-02',%s,%s,%s,%s,%s,'trade')''',
                (action, action, fields['currency'], amount, str(item_id)))
        cur.execute("UPDATE redesign_test.source_records SET status='published' WHERE source_id=%s", (str(source_id),))
        start, end = date(2026,1,1), date(2026,1,2)
        assert _cash_source_schema_available(wrapped)
        assert _cash_delta_cny(wrapped,'bank-a',start,end) is None
        cur.execute("INSERT INTO redesign_test.pams_fx_rates_daily VALUES ('CNY','USD','2026-01-02',7,NULL)")
        assert _cash_delta_cny(wrapped,'bank-a',start,end) is None
        cur.execute("UPDATE redesign_test.pams_fx_rates_daily SET source='synthetic'")
        assert _cash_delta_cny(wrapped,'bank-a',start,end) == Decimal('-30')
        assert _cash_delta_cny(wrapped,'bank-a',start,end,external_only=True) == Decimal('70')
        cur.execute("SELECT trade_id FROM redesign_test.pams_investment_trade_fact WHERE trade_type='deposit'")
        fact_id = cur.fetchone()['trade_id']
        assert verify_native_fact(wrapped, table='pams_investment_trade_fact', fact_id=fact_id)['status'] == 'verified'
        cur.execute("UPDATE redesign_test.pams_investment_trade_fact SET net_cash=20 WHERE trade_type='deposit'")
        assert verify_native_fact(wrapped, table='pams_investment_trade_fact', fact_id=fact_id)['status'] == 'unknown'
        assert _cash_delta_cny(wrapped,'bank-a',start,end) is None
