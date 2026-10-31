"""Isolated CLI and existing trade-fact publication using synthetic CSVs."""
# ruff: noqa: F811
import json
import csv
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import make_dsn
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from tests.database.test_tradingview_source_candidate import HEADER, ROW1, ROW2
from domain.imports.tradingview_source_candidate import stage_csvs
from domain.imports.tradingview_source_publish import publish, _source_row
from domain.imports import tradingview_source_cli as cli
from domain.investments.investment_trade_fact import build_investment_trade_fact, insert_investment_trade_facts
from collectors.tradingview_portfolio import _row_hash
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    with psycopg2.connect(**params) as conn, conn.cursor() as cur:
        for name in ('019_tradingview_transactions.sql', '080_unified_source_staging.sql', '081_unified_source_fact_links.sql'):
            cur.execute((ROOT / 'database/postgres/schema' / name).read_text(encoding='utf-8'))
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    return params


def stage(params, content, *, allow_existing=False):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return stage_csvs(cur, batch_id=uuid4(), account_code='tonghuashun', files=[('input.csv', content)], allow_existing=allow_existing)['batch_id']


def run_publish(params, batch_id, **kwargs):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return publish(cur, batch_id=batch_id, **kwargs)


def test_confirmed_publication_and_replay_leave_holdings_untouched(isolated):
    batch_id = stage(isolated, (HEADER + ROW1 + ROW2).encode())
    with pytest.raises(ValueError, match='explicit confirmation'):
        run_publish(isolated, batch_id)
    assert run_publish(isolated, batch_id, confirm=True)['published_count'] == 2
    assert run_publish(isolated, batch_id, confirm=True)['replayed']
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact WHERE source_item_id IS NOT NULL')[0]['n'] == 2
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0


def test_replay_blocks_changed_fact_content(isolated):
    batch_id = stage(isolated, (HEADER + ROW1).encode())
    run_publish(isolated, batch_id, confirm=True)
    sql(isolated, 'UPDATE pams_investment_trade_fact SET fee_amount=99')
    with pytest.raises(ValueError, match='Conflicting immutable trade source'):
        run_publish(isolated, batch_id, confirm=True)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1


def test_failure_rolls_back_source_fact_and_legacy_transaction(isolated):
    batch_id = stage(isolated, (HEADER + ROW1 + ROW2).encode())
    sql(isolated, "CREATE FUNCTION reject_tv_link() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic link error'; END $$")
    sql(isolated, 'CREATE TRIGGER reject_tv_link BEFORE UPDATE OF source_item_id ON pams_investment_trade_fact FOR EACH ROW EXECUTE FUNCTION reject_tv_link()')
    with pytest.raises(psycopg2.errors.RaiseException):
        run_publish(isolated, batch_id, confirm=True)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_tradingview_transactions')[0]['n'] == 0
    assert sql(isolated, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


def seed_legacy_fact(isolated, data):
    raw = next(csv.DictReader(StringIO(data.decode())))
    with psycopg2.connect(**isolated) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''INSERT INTO pams_tradingview_transactions(account_code,portfolio_name,source_file,source_hash,row_hash,
            symbol,normalized_symbol,side,quantity,fill_price,commission,currency,closing_time,raw)
            VALUES ('tonghuashun','A股','input.csv',%s,%s,'SSE:600000','600000','Buy',10,8,1,'CNY','2026-10-01 12:00:00',%s)
            RETURNING id''', (sha256(data).hexdigest(), _row_hash(raw), Json(raw)))
        identifier = cur.fetchone()['id']
        insert_investment_trade_facts(cur, [build_investment_trade_fact(_source_row(cur, identifier))], strict_source=True)
    return identifier


def test_existing_fact_reuse_requires_matching_evidence_and_explicit_permission(isolated):
    data = (HEADER + ROW1).encode()
    seed_legacy_fact(isolated, data)
    with pytest.raises(ValueError, match='existing TradingView'):
        stage(isolated, data)
    changed_file = (HEADER.replace('Note', 'Note ') + ROW1).encode()
    with pytest.raises(ValueError, match='matching file evidence'):
        stage(isolated, changed_file, allow_existing=True)
    batch_id = stage(isolated, data, allow_existing=True)
    with pytest.raises(ValueError, match='existing TradingView'):
        run_publish(isolated, batch_id, confirm=True)
    assert run_publish(isolated, batch_id, confirm=True, allow_existing=True)['published_count'] == 1
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 1


def test_existing_fact_content_conflict_rolls_back_link(isolated):
    data = (HEADER + ROW1).encode()
    seed_legacy_fact(isolated, data)
    sql(isolated, 'UPDATE pams_investment_trade_fact SET fee_amount=99')
    batch_id = stage(isolated, data, allow_existing=True)
    with pytest.raises(ValueError, match='Conflicting immutable trade source'):
        run_publish(isolated, batch_id, confirm=True, allow_existing=True)
    assert sql(isolated, 'SELECT source_item_id FROM pams_investment_trade_fact')[0]['source_item_id'] is None
    assert sql(isolated, 'SELECT status FROM source_batches')[0]['status'] == 'draft'


def test_explicit_cli_stage_and_publish(isolated, monkeypatch, tmp_path, capsys):
    path = tmp_path / 'input.csv'
    path.write_text(HEADER + ROW1, encoding='utf-8')
    monkeypatch.setenv('PAMS_TEST_POSTGRES_DSN', make_dsn(**isolated))
    assert cli.main(['stage', '--account', 'tonghuashun', str(path)]) == 0
    batch_id = json.loads(capsys.readouterr().out)['batch_id']
    assert cli.main(['publish', batch_id, '--confirm']) == 0
    assert json.loads(capsys.readouterr().out)['published_count'] == 1


def test_cli_rejects_non_disposable_dsn_before_connect(monkeypatch, tmp_path):
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    monkeypatch.setenv('PAMS_TEST_POSTGRES_DSN', 'host=127.0.0.1 dbname=production user=invalid')
    monkeypatch.setattr(cli.psycopg2, 'connect', lambda *a, **k: pytest.fail('unexpected database connection'))
    path = tmp_path / 'input.csv'
    path.write_text(HEADER + ROW1, encoding='utf-8')
    with pytest.raises(ValueError, match='disposable'):
        cli.main(['stage', '--account', 'tonghuashun', str(path)])
