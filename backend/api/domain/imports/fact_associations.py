"""Verify all fact links for a source batch without a legacy import dependency."""
from uuid import UUID


def check_all_source_links(cur, batch_id, records):
    targets = {'pams_cashflow_fact': 'cashflow_id', 'pams_investment_trade_fact': 'trade_id',
               'pams_holdings_snapshot': 'id'}
    expected = {(record['table'], str(record['id']), str(UUID(str(record['row_id'])))) for record in records}
    actual = set()
    for table, column in targets.items():
        cur.execute(f'SELECT fact.{column} AS fact_id, fact.source_item_id FROM public.{table} fact '
                    'JOIN public.source_items source ON source.source_item_id=fact.source_item_id '
                    'WHERE source.batch_id=%s', (str(batch_id),))
        actual.update((table, str(row['fact_id']), str(row['source_item_id'])) for row in cur.fetchall())
    if actual != expected:
        raise ValueError('source batch has unexpected or missing fact associations')
