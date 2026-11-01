"""Attach recorded trade-derived cost data only while its evidence is current."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from domain.imports.source_fact_evidence import NativeFactVerifier
from domain.investments.valuation_input_evidence import _bound_input, _hash, verify_recorded_version


def enrich_trade_snapshot_costs(cur, rows):
    candidates = [row for row in rows if (row.get('account') or row.get('account_code')) == 'tonghuashun'
                  and row.get('currency') == 'CNY' and row.get('holding_id')]
    if not candidates:
        return rows
    cur.execute("SELECT to_regclass('public.source_batches') IS NOT NULL AND to_regclass('public.valuation_evidence') IS NOT NULL AS ready")
    if not cur.fetchone()['ready']:
        return rows
    cur.execute("""SELECT published_result FROM source_batches WHERE account_code='tonghuashun'
        AND workflow='trade_snapshot' AND status='published' ORDER BY created_at DESC""")
    records = [record for batch in cur.fetchall() for record in (batch['published_result'] or {}).get('records', [])
               if isinstance(record, dict)]
    verifier = NativeFactVerifier(cur)
    for row in candidates:
        for record in records:
            if record.get('symbol') != row.get('symbol'):
                continue
            try:
                holding_id, version = int(record['id']), record['version']
                _, original = _bound_input(cur, 'holding', {'id': holding_id}, locking=False)
                if not original or _hash(original) != record['holding_hash'] or not verify_recorded_version(cur, version):
                    continue
                if (original.get('account_code') != 'tonghuashun' or original.get('symbol') != row.get('symbol')
                        or original.get('currency') != row.get('currency')
                        or original.get('asset_id') != row.get('asset_id')
                        or Decimal(str(original['quantity'])) != Decimal(str(row['quantity']))
                        or Decimal(str(original['cost'])) != Decimal(str(row['cost']))
                        or str(original['as_of']) > str(row['as_of'])[:10]):
                    continue
                event_ids = record['all_event_ids']
                if not isinstance(event_ids, list) or not event_ids or len(set(event_ids)) != len(event_ids):
                    continue
                cur.execute('''SELECT trade_id FROM pams_investment_trade_fact WHERE account_code=%s AND symbol=%s
                    AND trade_date <= %s ORDER BY trade_id''', ('tonghuashun', row['symbol'], row['as_of']))
                if set(event_ids) != {r['trade_id'] for r in cur.fetchall()}:
                    continue
                proofs = verifier.verify_many(table='pams_investment_trade_fact', fact_ids=event_ids)
                if any(proofs.get(event_id, {}).get('status') != 'verified' for event_id in event_ids):
                    continue
                cur.execute('SELECT inputs FROM valuation_evidence WHERE version_key=%s', (version,))
                evidence = (cur.fetchone() or {}).get('inputs') or []
                current_refs = [item['evidence'] for item in evidence if item.get('input_kind') in {'trade', 'quote'}]
                if len(current_refs) != len(event_ids) + 1:
                    continue
                consistent = True
                for item in current_refs:
                    bound, raw = _bound_input(cur, item['kind'], item['source_ref']['key'], locking=False)
                    if not raw or bound['snapshot']['content_hash'] != item['snapshot']['content_hash']:
                        consistent = False
                        break
                if not consistent:
                    continue
                diluted = Decimal(record['diluted_cost'])
                realized = Decimal(record['realized_trade_pnl'])
                distributions = Decimal(record['net_distributions'])
                market, cost = Decimal(str(row['market_value'])), Decimal(str(row['cost']))
                if not all(value.is_finite() for value in (diluted, realized, distributions, market, cost)):
                    continue
                total = market - cost + realized + distributions
                row.update(diluted_cost=diluted, diluted_cost_cny=diluted, diluted_cost_source='verified_trade_snapshot',
                           realized_trade_pnl=realized, realized_trade_pnl_native=realized, realized_trade_pnl_cny=realized,
                           net_distributions=distributions, net_distributions_cny=distributions,
                           holding_pnl=total, holding_pnl_native=total, holding_pnl_cny=total,
                           holding_pnl_before_realized_trade=market-cost,
                           holding_pnl_before_realized_trade_native=market-cost,
                           holding_pnl_before_realized_trade_cny=market-cost,
                           cost_basis_mode='weighted_average_trade_snapshot')
                break
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
    return rows
