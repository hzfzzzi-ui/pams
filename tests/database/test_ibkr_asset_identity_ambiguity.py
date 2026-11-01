"""Synthetic IBKR rows never choose between conflicting asset identities."""
# ruff: noqa: F811
from datetime import date
import psycopg2
from psycopg2.extras import RealDictCursor
from tests.database.test_isolated_source_bootstrap import empty_source_db  # noqa: F401
from scripts.bootstrap_isolated_sources import initialize_isolated_sources
from collectors.ibkr_flex import _write_holdings


def test_ibkr_asset_identity_requires_unique_primary_and_alias_match(empty_source_db, monkeypatch):
    monkeypatch.setenv("PAMS_HOLDING_EVIDENCE_ISOLATED", "true")
    day=date(2026,10,22)
    with psycopg2.connect(**empty_source_db) as conn:
        initialize_isolated_sources(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pams_asset_master(asset_id,primary_symbol,canonical_name,asset_type,currency)
                VALUES ('A','UNIQUE','A','stock','USD'),('B','AMBIG','B','stock','USD'),
                       ('C','AMBIG','C','stock','USD'),('D','CONFLICT','D','stock','USD'),
                       ('E','OTHER','E','stock','USD'),('F','NOMATCH','F','stock','USD')""")
            cur.execute("""INSERT INTO pams_asset_alias(asset_id,alias_type,alias_value)
                VALUES ('E','alias','CONFLICT'),('F','alias','ALIAS_ONLY')""")
        rows=[]
        for symbol in ('UNIQUE','AMBIG','CONFLICT','ALIAS_ONLY'):
            rows.append({'account_code':'ibkr','symbol':symbol,'name':'Synthetic '+symbol,'currency':'USD',
                'asset_liability':'\u8d44\u4ea7','major_category':'synthetic','minor_category':'synthetic',
                'market_value':100,'cost':90,'quantity':2,'buy_price':45,'current_price':50,'as_of':day})
        assert _write_holdings(conn,rows,account_code='ibkr',as_of=day)==4
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT symbol,asset_id,market_value FROM pams_holdings_snapshot WHERE account_code='ibkr'")
            values={row['symbol']:row for row in cur.fetchall()}
        assert values['UNIQUE']['asset_id']=='A'
        assert values['AMBIG']['asset_id'] is None
        assert values['CONFLICT']['asset_id'] is None
        assert values['ALIAS_ONLY']['asset_id']=='F'
        assert all(row['market_value']==100 for row in values.values())
