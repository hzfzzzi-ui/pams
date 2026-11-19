from decimal import Decimal

import pytest

from domain.ledger.confirmed_transfers_v2 import PURCHASE_PURPOSE, is_manual_funding_pair
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401


def legs():
    cash = {'kind': 'cashflow', 'id': 1, 'account_code': 'bank', 'source_item_id': 'item-a',
            'source_id': 'source-a', 'source_hash': 'hash-a', 'day': '2026-01-29',
            'currency': 'CNY', 'amount': Decimal('-100')}
    trade = {'kind': 'trade', 'id': 2, 'account_code': 'investment', 'source_item_id': 'item-b',
             'source_id': 'source-b', 'source_hash': 'hash-b', 'day': '2026-01-29',
             'currency': 'CNY', 'amount': Decimal('100'), 'purpose': PURCHASE_PURPOSE,
             'bank_cashflow_id': 1, 'bank_source_item_id': 'item-a',
             'bank_source_id': 'source-a', 'bank_source_hash': 'hash-a'}
    return cash, trade


def test_purchase_uses_existing_bank_evidence():
    cash, trade = legs()
    assert is_manual_funding_pair(cash, trade)


@pytest.mark.parametrize('field,value', [
    ('bank_cashflow_id', 3), ('bank_source_item_id', 'other'), ('bank_source_id', 'other'),
    ('bank_source_hash', 'other'), ('amount', Decimal('99')), ('currency', 'USD'),
    ('day', '2026-01-30'), ('account_code', 'bank'),
])
def test_purchase_rejects_conflicting_evidence(field, value):
    cash, trade = legs()
    trade[field] = value
    assert not is_manual_funding_pair(cash, trade)


def test_purchase_confirmation_in_isolated_database(empty_source_db):
    from uuid import uuid4
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from scripts.bootstrap_isolated_sources import initialize_isolated_sources
    from domain.imports import source_records_v2 as sources
    from domain.ledger import confirmed_transfers_v2 as transfers
    from domain.ledger.manual_transfer_pairs import pairing_options
    from domain.ledger.ledger_view import build_ledger_workbench_payload

    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pams_accounts(account_code,display_name) VALUES ('test_bank','Bank'),('test_asset','Asset')")
            source, item = uuid4(), uuid4()
            sources.create_source(cur, source_id=source, account_code='test_bank', source_name='test',
                                  acquisition_method='file', document_type='cash', filename='bank.csv',
                                  file_format='csv', content_type='text/csv', content=b'synthetic bank')
            fields = {'business_date': '2026-01-29', 'net_cash': '-100', 'currency': 'CNY', 'summary': 'purchase'}
            sources.add_item(cur, source_id=source, item_id=item, parse_method='manual', raw_fields={'row': 1}, parsed_fields=fields)
            rev = sources.review_item(cur, source_id=source, item_id=item, expected_revision=2, parsed_fields=fields, review_status='confirmed')
            sources.publish_cash_source(cur, source_id=source, expected_revision=rev)
            cur.execute('SELECT cashflow_id FROM pams_cashflow_fact WHERE source_item_id=%s', (str(item),))
            cash = cur.fetchone()['cashflow_id']
            cur.execute("UPDATE cashflow_classification SET cashflow_type='internal_transfer',status='confirmed',classification_source='manual' WHERE cashflow_id=%s", (cash,))
            cur.execute('SELECT sha256 FROM source_records WHERE source_id=%s', (str(source),))
            proof = {'cashflow_id': cash, 'source_item_id': str(item), 'source_id': str(source), 'source_hash': cur.fetchone()['sha256']}
            ts, ti = uuid4(), uuid4()
            sources.create_source(cur, source_id=ts, account_code='test_asset', source_name='test', acquisition_method='manual', document_type='trade')
            trade_fields = {'business_date': '2026-01-29', 'net_cash': '-100', 'currency': 'CNY',
                            'trade_type': 'buy', 'symbol': 'TEST', 'name': 'Synthetic', 'quantity': '10',
                            'price': '10', 'gross_amount': '100', 'fee_amount': '0'}
            sources.add_item(cur, source_id=ts, item_id=ti, parse_method='manual', raw_fields={'bank_cashflow_evidence': proof}, parsed_fields=trade_fields)
            rev = sources.review_item(cur, source_id=ts, item_id=ti, expected_revision=2, parsed_fields=trade_fields, review_status='confirmed')
            sources.publish_trade_source(cur, source_id=ts, expected_revision=rev)
            cur.execute('SELECT trade_id FROM pams_investment_trade_fact WHERE source_item_id=%s', (str(ti),))
            trade = cur.fetchone()['trade_id']
            candidate, = pairing_options(cur, cash)['rows']
            assert candidate['id'] == trade
            before = build_ledger_workbench_payload(cur, month='2026-01', view='overview')['periods']['month']
            saved = transfers.confirm(cur, from_kind='cashflow', from_id=cash, to_kind='trade', to_id=trade,
                                      note='Synthetic purchase', confirmed=True, manual_anchor_id=cash)
            assert transfers.confirm(cur, from_kind='cashflow', from_id=cash, to_kind='trade', to_id=trade,
                                     note='Synthetic purchase', confirmed=True, manual_anchor_id=cash)['replayed']
            decision, = transfers.read(cur)
            assert decision['evidence_status'] == 'confirmed' and str(decision['transfer_id']) == saved['transfer_id']
            from domain.ledger.ledger_view import _load_transfer_matches
            from datetime import date
            matches = _load_transfer_matches(cur, start=date(2026, 1, 1), end=date(2026, 1, 31),
                                             base_currency='CNY', cash_pairs_only=True)
            match, = matches
            assert match['match_type'] == 'investment_funding'
            assert match['source_row_id'] == cash and match['target_trade_id'] == trade
            assert match['amount_cny'] == Decimal('100')
            after = build_ledger_workbench_payload(cur, month='2026-01', view='overview')['periods']['month']
            assert before['expense'] - after['expense'] == 100
            assert after['investment_funding'] - before['investment_funding'] == 100
            assert before['income'] == after['income'] and before['cash_net'] == after['cash_net']
            cur.execute('SELECT COUNT(*) AS n FROM pams_cashflow_fact')
            assert cur.fetchone()['n'] == 1
            cur.execute('SELECT COUNT(*) AS n FROM pams_investment_trade_fact')
            assert cur.fetchone()['n'] == 1
