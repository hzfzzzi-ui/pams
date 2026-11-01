"""TradingView source evidence tests use synthetic CSVs in disposable PG16."""
# ruff: noqa: F811
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from tests.imports.test_account_import_publish import client, sql  # noqa: F401
from domain.imports.tradingview_source import stage_csvs

ROOT = Path(__file__).resolve().parents[2]
HEADER = 'Symbol,Side,Qty,Fill Price,Commission,Closing Time,Note\n'
ROW1 = 'SSE:600000,Buy,10,8,1,2026-10-01 12:00:00,first\n'
ROW2 = 'SSE:600001,Buy,20,9,1,2026-10-02 12:00:00,second\n'


@pytest.fixture
def isolated(client, monkeypatch):
    _, params = client
    monkeypatch.setenv('PAMS_UNIFIED_SOURCE_ISOLATED', 'true')
    return params


def stage(params, files):
    with psycopg2.connect(**params) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return stage_csvs(cur, batch_id=uuid4(), account_code='tonghuashun', files=files)


def test_original_file_and_row_locations_preserved_without_facts(isolated):
    content = (HEADER + ROW1 + ROW2).encode()
    result = stage(isolated, [('trades.csv', content)])
    assert result['files'] == 1 and result['items'] == 2 and result['review_required']
    assert bytes(sql(isolated, 'SELECT content FROM source_files')[0]['content']) == content
    rows = sql(isolated, 'SELECT location, raw_fields, review_status FROM source_items ORDER BY location')
    assert [row['location'] for row in rows] == ['2-2', '3-3']
    assert rows[0]['raw_fields']['raw_fields']['Note'] == 'first'
    assert all(row['review_status'] == 'pending' for row in rows)
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_investment_trade_fact')[0]['n'] == 0
    assert sql(isolated, 'SELECT count(*) AS n FROM pams_holdings_snapshot')[0]['n'] == 0


def test_six_field_match_across_files_is_blocked_even_when_other_fields_differ(isolated):
    first = (HEADER + ROW1).encode()
    second = (HEADER + ROW1.replace('first', 'other note')).encode()
    with pytest.raises(ValueError, match='ambiguous TradingView rows'):
        stage(isolated, [('first.csv', first), ('second.csv', second)])
    assert sql(isolated, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0


def test_same_six_fields_in_previous_batch_are_blocked(isolated):
    first = (HEADER + ROW1).encode()
    stage(isolated, [('first.csv', first)])
    with pytest.raises(ValueError, match='possible repeated'):
        stage(isolated, [('changed.csv', (HEADER + ROW1.replace('first', 'other')).encode())])
    assert sql(isolated, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 1


def test_csv_embedded_newline_uses_physical_line_span(isolated):
    content = (HEADER + 'SSE:600000,Buy,10,8,1,2026-10-01 12:00:00,"first\nsecond"\n').encode()
    stage(isolated, [('multiline.csv', content)])
    item = sql(isolated, 'SELECT location,raw_fields FROM source_items')[0]
    assert item['location'] == '2-3' and item['raw_fields']['raw_fields']['Note'] == 'first\nsecond'


def test_ambiguous_headers_and_repeated_file_never_stage(isolated):
    invalid = (HEADER.rstrip('\n') + ',Note\n' + ROW1.rstrip('\n') + ',duplicate\n').encode()
    with pytest.raises(ValueError, match='ambiguous.*headers'):
        stage(isolated, [('bad.csv', invalid)])
    data = (HEADER + ROW1).encode()
    with pytest.raises(ValueError, match='repeated file content'):
        stage(isolated, [('a.csv', data), ('b.csv', data)])
    assert sql(isolated, 'SELECT count(*) AS n FROM source_batches')[0]['n'] == 0
