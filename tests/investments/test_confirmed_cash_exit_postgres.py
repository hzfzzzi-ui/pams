from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources


def test_complete_exit_flows_from_review_to_returns_and_revocation(empty_source_db):
    from domain.imports import source_records_v2 as sources
    from domain.imports import trade_supplemental_evidence as evidence
    from domain.investments.period_trade_returns import load_closed_trade_returns

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_asset','Synthetic')")
            cur.execute("""INSERT INTO pams_asset_master(asset_id,canonical_name,primary_symbol,asset_type,currency)
                           VALUES ('TEST_WEALTH','Synthetic','TEST','wealth_management','CNY')""")
        common = {'symbol': 'TEST', 'asset_id': 'TEST_WEALTH', 'name': 'Synthetic', 'currency': 'CNY',
                  'fee_amount': '0', 'tax_amount': '0'}
        buy = {**common, 'business_date': '2025-12-12', 'trade_type': 'buy', 'side': 'buy', 'net_cash': '-50000', 'gross_amount': '50000'}
        sell = {**common, 'business_date': '2026-03-24', 'trade_type': 'sell', 'side': 'sell', 'net_cash': '50322.39', 'gross_amount': '50322.39'}
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            origin = uuid4()
            sources.create_source(cur, source_id=origin, account_code='test_asset', source_name='manual',
                                  acquisition_method='manual', document_type='trade')
            for fields in [buy, sell]:
                item_id = uuid4()
                cur.execute('SELECT revision FROM source_records WHERE source_id=%s', (str(origin),))
                revision = sources.add_manual_item(cur, source_id=origin, item_id=item_id,
                                                   expected_revision=cur.fetchone()['revision'], parsed_fields=fields)
                sources.review_item(cur, source_id=origin, item_id=item_id, expected_revision=revision,
                                    parsed_fields=fields, review_status='confirmed')
                cur.execute('''INSERT INTO pams_investment_trade_fact
                    (account_code,source_item_id,trade_date,trade_type,side,asset_id,symbol,name,currency,
                     net_cash,gross_amount,fee_amount,tax_amount,fact_role)
                    VALUES ('test_asset',%s,%s,%s,%s,'TEST_WEALTH','TEST','Synthetic','CNY',%s,%s,0,0,'investment_trade')''',
                    (str(item_id), fields['business_date'], fields['trade_type'], fields['side'], fields['net_cash'], fields['gross_amount']))
            cur.execute("UPDATE source_records SET status='published' WHERE source_id=%s", (str(origin),))
            def read(start=date.min):
                return load_closed_trade_returns(cur, start=start, end=date(2026, 11, 19),
                    owner_ratio=lambda _: Decimal(1), sections={'wealth_management'})
            assert read() == {}
            cur.execute("SELECT trade_id,trade_type FROM pams_investment_trade_fact ORDER BY trade_id")
            ids = cur.fetchall()
            trade_id = next(row['trade_id'] for row in ids if row['trade_type'] == 'sell')
            source, item = uuid4(), uuid4()
            fields = {**sell, 'complete_exit': True, 'complete_history': True,
                      'complete_trade_ids': [row['trade_id'] for row in ids]}
            sources.create_source(cur, source_id=source, account_code='test_asset', source_name='manual',
                                  acquisition_method='manual', document_type='trade')
            revision = sources.add_manual_item(cur, source_id=source, item_id=item, expected_revision=1, parsed_fields=fields)
            revision = sources.review_item(cur, source_id=source, item_id=item, expected_revision=revision,
                                           parsed_fields=fields, review_status='confirmed')
            saved = evidence.attach(cur, source_id=source, item_id=item, trade_id=trade_id,
                                    expected_revision=revision, note='Confirmed complete exit', confirmed=True)
            assert Decimal(read()['wealth_management']['amount']) == Decimal('322.39')
            assert read(date(2026, 1, 1)) == {}
            buy_id = next(row['trade_id'] for row in ids if row['trade_type'] == 'buy')
            buy_sources = [
                {**buy, 'quantity': '48118.56'},
                {**buy, 'valuation_date': '2025-12-31', 'valuation_unit_price': '1.040500'},
            ]
            confirmations = []
            for fields in buy_sources:
                proof_source, proof_item = uuid4(), uuid4()
                sources.create_source(cur, source_id=proof_source, account_code='test_asset', source_name='test',
                                      acquisition_method='screenshot', document_type='trade', filename='proof.jpg',
                                      file_format='jpg', content_type='image/jpeg', content=str(proof_item).encode())
                sources.add_item(cur, source_id=proof_source, item_id=proof_item, parse_method='manual',
                                 raw_fields={'synthetic': str(proof_item)}, parsed_fields=fields)
                proof_rev = sources.review_item(cur, source_id=proof_source, item_id=proof_item, expected_revision=2,
                                                parsed_fields=fields, review_status='confirmed')
                confirmations.append(evidence.attach(cur, source_id=proof_source, item_id=proof_item, trade_id=buy_id,
                    expected_revision=proof_rev, note='Owner reviewed screenshot', confirmed=True))
            assert Decimal(read(date(2026, 1, 1))['wealth_management']['amount']) == Decimal('255.02832')
            evidence.revoke(cur, confirmation_id=confirmations[-1]['confirmation_id'], note='Undo mark', confirmed=True)
            assert read(date(2026, 1, 1)) == {}
            cur.execute('SELECT count(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 2
            cur.execute('UPDATE pams_investment_trade_fact SET net_cash=50320 WHERE trade_id=%s', (trade_id,))
            assert read() == {}
            cur.execute('UPDATE pams_investment_trade_fact SET net_cash=50322.39 WHERE trade_id=%s', (trade_id,))
            evidence.revoke(cur, confirmation_id=saved['confirmation_id'], note='Undo', confirmed=True)
            assert read() == {}
