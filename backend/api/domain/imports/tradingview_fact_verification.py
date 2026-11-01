"""Read-only, request-scoped verification of TradingView published trade facts."""
from __future__ import annotations

from hashlib import sha256
from typing import Any

from collectors.tradingview_portfolio import TradingViewImportError
from domain.imports.source_fact_evidence import _reviewed_snapshot
from domain.imports.tradingview_source import _prepare
from domain.imports.tradingview_source_publish import source_row_from_item
from domain.investments.investment_trade_fact import _STRICT_FACT_COLUMNS, build_investment_trade_fact


class TradingViewFactVerifier:
    """Verify raw bytes, reviewed snapshot, location and every immutable fact field.

    One instance is confined to one caller-owned repeatable-read transaction.
    Verification shows consistency, not external identity authentication.
    """
    def __init__(self, cur):
        self.cur = cur
        self.snapshots: dict[Any, Any] = {}
        self.batches: dict[Any, Any] = {}
        self.files: dict[Any, Any] = {}
        self.transaction: tuple[Any, Any] | None = None

    def verify_many(self, fact_ids: list[int]) -> dict[int, bool]:
        result = {ident: False for ident in fact_ids}
        if not fact_ids or self.cur.connection.autocommit:
            return result
        self.cur.execute("""SELECT current_setting('transaction_read_only') AS ro,
            current_setting('transaction_isolation') AS isolation,pg_is_in_recovery() AS recovering,
            pg_backend_pid() AS backend,
            CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_xact_id()::text END AS xid""")
        mode = self.cur.fetchone()
        if mode['ro'] != 'on' or mode['isolation'] not in {'repeatable read', 'serializable'}:
            self.snapshots.clear(); self.batches.clear(); self.files.clear(); self.transaction = None
            return result
        key = (mode['backend'], mode['xid']) if mode['xid'] else None
        if key is None or key != self.transaction:
            self.snapshots.clear(); self.batches.clear(); self.files.clear(); self.transaction = key
        self.cur.execute("""SELECT f.*, i.batch_id,i.file_id,i.raw_fields AS evidence,
            i.record_hash AS evidence_hash,i.location AS evidence_location,i.source_uid AS evidence_uid,
            i.parse_method AS evidence_method,i.review_status AS item_status,
            sf.filename AS evidence_filename,sf.sha256 AS file_hash
            FROM pams_investment_trade_fact f
            JOIN source_items i ON i.source_item_id=f.source_item_id
            JOIN source_files sf ON sf.file_id=i.file_id AND sf.batch_id=i.batch_id
            WHERE f.trade_id=ANY(%s::bigint[]) AND f.source_table='tradingview_csv'""", (fact_ids,))
        rows = [dict(row) for row in self.cur.fetchall()]
        for row in rows:
            ident, batch_id, file_id = row['trade_id'], row['batch_id'], row['file_id']
            if batch_id not in self.batches:
                self.cur.execute('SELECT * FROM source_batches WHERE batch_id=%s', (batch_id,))
                batch = self.cur.fetchone()
                self.batches[batch_id] = dict(batch) if batch else None
            batch = self.batches[batch_id]
            if (not batch or batch['status'] != 'published' or batch['institution'] != 'tradingview'
                    or batch['account_code'] != row['account_code'] or row['item_status'] != 'confirmed'
                    or not batch.get('published_source_hash')
                    or not _reviewed_snapshot(self.cur, batch, batch_id, locking=False, snapshot_cache=self.snapshots)):
                continue
            if file_id not in self.files:
                self.cur.execute('SELECT content FROM source_files WHERE file_id=%s AND batch_id=%s',(file_id,batch_id))
                file_row = self.cur.fetchone()
                content = bytes(file_row['content']) if file_row else b''
                try:
                    if sha256(content).hexdigest() != str(row['file_hash']).strip():
                        raise ValueError('source file hash mismatch')
                    parsed = _prepare([(row['evidence_filename'], content)])
                    self.files[file_id] = {f'{first}-{last}': (file_hash, first, last, key, raw)
                        for _, _, file_hash, items in parsed for first, last, key, raw in items}
                except (ValueError, UnicodeError, TradingViewImportError):
                    self.files[file_id] = {}
            source = self.files[file_id].get(row['evidence_location'])
            if not source:
                continue
            file_hash, first, last, key, raw = source
            exact = {'file_sha256': file_hash, 'line_start': first, 'line_end': last,
                     'legacy_six_field_hash': key, 'raw_fields': raw}
            if (row['evidence'] != exact or row['evidence_uid'] != f'{file_hash}:{first}:{last}'
                    or row['evidence_method'] != 'template'):
                continue
            try:
                item = {'source_item_id': row['source_item_id'], 'source_uid': row['evidence_uid']}
                expected = build_investment_trade_fact(source_row_from_item(
                    {'account_code': row['account_code']}, item, {'filename': row['evidence_filename']}, raw))
                if all(row[key] == expected[key] for key in _STRICT_FACT_COLUMNS):
                    result[ident] = True
            except (KeyError, ValueError, TypeError):
                continue
        return result
