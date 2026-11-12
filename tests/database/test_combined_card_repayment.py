"""A combined repayment occupies and removes every leg atomically."""
from datetime import date
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401, F811
from tests.database.test_transaction_fx_evidence import _publish_synthetic_cashflow


def test_combined_repayment_confirms_once_and_revokes_all_legs(empty_source_db, monkeypatch):  # noqa: F811
    from domain.ledger import combined_card_repayment, confirmed_transfers, confirmed_transfers_v2, ledger_view
    from domain.ledger.manual_transfer_pairs import pairing_options

    day = date(2026, 9, 27)
    profiles = {
        'bank': {'account_type': 'funding', 'institution': '测试银行'},
        'card_cny': {'account_type': 'liability', 'institution': '测试发卡行'},
        'card_usd': {'account_type': 'liability', 'institution': '测试发卡行'},
    }
    monkeypatch.setattr(ledger_view, '_load_accounts_config', lambda: {'accounts': profiles})
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for code, currency in [('bank', 'CNY'), ('card_cny', 'CNY'), ('card_usd', 'USD')]:
                cur.execute('INSERT INTO pams_accounts(account_code,display_name,base_currency) VALUES (%s,%s,%s)',
                            (code, code, currency))
            cur.execute("""INSERT INTO pams_fx_rates_daily(base_currency,quote_currency,as_of,rate,source)
                VALUES ('CNY','USD',%s,7,'synthetic')""", (day,))
            for account, amount, currency, summary in [
                ('bank', '-1702', 'CNY', '测试发卡行信用卡还款'),
                ('card_cny', '1000', 'CNY', '跨行合并还款'),
                ('card_usd', '100', 'USD', '跨行合并购汇还款'),
            ]:
                _publish_synthetic_cashflow(conn, account=account, classification='debt_repayment', fields={
                    'business_date': str(day), 'net_cash': amount, 'currency': currency, 'summary': summary,
                })
            cur.execute('SELECT cashflow_id, account_code FROM pams_cashflow_fact')
            ids = {row['account_code']: row['cashflow_id'] for row in cur.fetchall()}
            preview = pairing_options(cur, ids['bank'])['repayment_groups']
            assert len(preview) == 1 and preview[0]['reference_difference_cny'] == Decimal(2)
            args = dict(source_id=ids['bank'], target_ids=[ids['card_cny'], ids['card_usd']],
                        note='synthetic confirmation', confirmed=True, profiles=profiles)
            result = combined_card_repayment.confirm(cur, **args)
            assert combined_card_repayment.confirm(cur, **args)['replayed']
            assert confirmed_transfers_v2.occupied_cashflow_ids(cur) == set(ids.values())
            with pytest.raises(ValueError, match='已经参加'):
                combined_card_repayment.confirm(cur, **{**args, 'note': 'different decision'})
            overview = ledger_view.build_ledger_workbench_payload(cur, month='2026-09', view='overview')
            assert overview['periods']['month']['income'] == overview['periods']['month']['expense'] == 0
            summary = overview['transfer_summary_month']
            assert len(summary) == 1 and summary[0]['amount'] == 1702 and summary[0]['count'] == 1
            stream = ledger_view.build_ledger_workbench_payload(
                cur, month='2026-09', view='stream', flow='cash_transfer', account='card_usd', pair_cards=True,
            )
            assert len(stream['rows']) == 1
            assert len(stream['rows'][0]['transfer_pair']['targets']) == 2
            with pytest.raises(ValueError, match='already confirmed'):
                confirmed_transfers_v2.confirm(
                    cur, from_kind='cashflow', from_id=ids['bank'], to_kind='cashflow', to_id=ids['card_usd'],
                    note='reuse existing leg', confirmed=True,
                )
            confirmed_transfers.revoke(cur, transfer_id=result['transfer_id'], note='synthetic reversal', confirmed=True)
            assert confirmed_transfers_v2.occupied_cashflow_ids(cur) == set()
            after = ledger_view.build_ledger_workbench_payload(cur, month='2026-09', view='overview')
            assert (after['periods']['month']['income'], after['periods']['month']['expense']) == (1700, 1702)
