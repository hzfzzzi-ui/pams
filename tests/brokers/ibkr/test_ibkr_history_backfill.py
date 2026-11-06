"""Historical Flex backfill against the marked source schema."""
from datetime import date
from hashlib import sha256
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from domain.brokers.ibkr.ibkr_history_backfill import backfill
from domain.imports.source_records_v2 import create_source
from domain.investments.trade_read_v2 import load_trade_rows
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401

CLOSE = date(2026, 1, 5)
ACCOUNT = 'U_TEST'
REPORT = b'''<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U_TEST" fromDate="20260105" toDate="20260105">
<Trades><Trade accountId="U_TEST" tradeID="42" assetCategory="STK" levelOfDetail="EXECUTION"
  symbol="SYNTH" currency="USD" buySell="BUY" tradeDate="20260105" dateTime="20260104;233000"
  settleDateTarget="20260106" quantity="2" tradePrice="5" tradeMoney="10"
  ibCommission="1" ibCommissionCurrency="USD" taxes="0.1" netCash="-11.1"/>
<Trade accountId="U_TEST" assetCategory="CASH"/></Trades>
<CashTransactions>
<CashTransaction accountId="U_TEST" transactionID="cash1" type="Other Fees"
  dateTime="20260105" reportDate="20260105" currency="USD" amount="-1"/>
<CashTransaction accountId="U_TEST" transactionID="cash2" type="Other Fees"
  dateTime="20260105" reportDate="20260105" currency="USD" amount="-1"/>
<CashTransaction accountId="U_TEST" transactionID="deposit1" type="Deposits/Withdrawals"
  dateTime="20260102" settleDate="20260102" reportDate="20260105" currency="MXN" amount="10"/>
</CashTransactions></FlexStatement>
<FlexStatement accountId="U_TEST" fromDate="20260102" toDate="20260102">
<Trades/><CashTransactions/></FlexStatement>
</FlexStatements></FlexQueryResponse>'''


def _archive(cur, report):
    source_id = str(uuid4())
    create_source(cur, source_id=source_id, account_code='ibkr', source_name='ibkr_daily_holdings',
                  acquisition_method='api', document_type='holding', file_format='xml', content=report)
    cur.execute("UPDATE source_records SET period_end=%s,status='published' WHERE source_id=%s", (CLOSE, source_id))
    return sha256(report).hexdigest()


def test_historical_flex_backfill_replays_and_halts_on_conflict(empty_source_db):
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            digest = _archive(cur, REPORT)
            preview = backfill(cur, digest=digest, account=ACCOUNT, as_of=CLOSE)
            assert preview['trade']['ready'] == 1 and preview['cash']['ready'] == 3
            assert preview['pending'] == [] and preview['excluded_fx_trades'] == 1
            with pytest.raises(ValueError, match='approved'):
                backfill(cur, digest=digest, account=ACCOUNT, as_of=CLOSE, apply=True,
                         approved_counts={'trade': 1, 'cash': 4})
            approved = {'trade': 1, 'cash': 3}
            initial = backfill(cur, digest=digest, account=ACCOUNT, as_of=CLOSE, apply=True,
                               approved_counts=approved)
            assert initial['trade']['published'] == 1 and initial['cash']['published'] == 3
            conn.commit()
            repeat = backfill(cur, digest=digest, account=ACCOUNT, as_of=CLOSE, apply=True,
                              approved_counts=approved)
            assert repeat['trade'] == {'ready': 0, 'duplicate': 1, 'published': 0}
            assert repeat['cash'] == {'ready': 0, 'duplicate': 3, 'published': 0}
            trades = load_trade_rows(cur, account_code='ibkr', start=CLOSE, end=CLOSE)
            assert trades[0]['source_evidence_status'] == 'verified'
            assert str(trades[0]['tax_amount']) == '0.1000000000'
            assert trades[0]['trade_time'] is None
            cur.execute("SELECT event_date FROM pams_cashflow_fact WHERE currency='MXN'")
            assert cur.fetchone()['event_date'] == date(2026, 1, 2)
            cur.execute("SELECT count(*) AS n FROM pams_holdings_snapshot")
            assert cur.fetchone()['n'] == 0
            conflict = _archive(cur, REPORT.replace(b'amount="-1"', b'amount="-2"', 1))
            with pytest.raises(ValueError, match='unresolved'):
                backfill(cur, digest=conflict, account=ACCOUNT, as_of=CLOSE, apply=True)
            cur.execute("SELECT count(*) AS n FROM pams_cashflow_fact")
            assert cur.fetchone()['n'] == 3


def test_historical_flex_backfill_is_atomic_on_cash_write_failure(empty_source_db):
    from contextlib import closing

    with closing(psycopg2.connect(**empty_source_db)) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            digest = _archive(cur, REPORT)
            cur.execute("""ALTER TABLE pams_cashflow_fact ADD CONSTRAINT synthetic_failure CHECK (currency <> 'MXN')""")
        conn.commit()
        with pytest.raises(psycopg2.errors.CheckViolation):
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                backfill(cur, digest=digest, account=ACCOUNT, as_of=CLOSE, apply=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT (SELECT count(*) FROM pams_investment_trade_fact) AS trades,
                (SELECT count(*) FROM pams_cashflow_fact) AS cash,
                (SELECT count(*) FROM source_records) AS sources""")
            assert cur.fetchone() == {'trades': 0, 'cash': 0, 'sources': 1}
