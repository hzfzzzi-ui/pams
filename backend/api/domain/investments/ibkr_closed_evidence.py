"""Read-only display of IBKR exits with original and immutable version proof."""
from collections.abc import Mapping
from domain.investments.holding_write_evidence import _ibkr_retirement_replayed


def verified_ibkr_closed_positions(cur, *, current_keys=None, limit=500):
    """Expose explicit zero facts only, never infer exits from missing trade rows."""
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode=cur.fetchone()
    if not isinstance(mode, Mapping) or not mode.get('readonly') or mode.get('level') not in {'repeatable read','serializable'}:
        return []
    cur.execute("""SELECT bool_and(to_regclass('public.' || name) IS NOT NULL) AS present
        FROM unnest(%s::text[]) AS name""", (['pams_holdings_snapshot','valuation_evidence',
        'source_items','source_batches','source_files'],))
    if not cur.fetchone()['present']:
        return []
    cur.execute("""SELECT DISTINCT h.*, zi.source_item_id AS zero_item, zi.batch_id AS zero_batch,
              zi.raw_fields->>'source_sha256' AS source_sha256,
              ii.source_item_id AS identity_item, ii.batch_id AS identity_batch
        FROM pams_holdings_snapshot h
        CROSS JOIN valuation_evidence v
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS he(input_kind text,input_hash text,evidence jsonb)
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS ze(input_kind text,input_hash text,evidence jsonb)
        CROSS JOIN LATERAL jsonb_to_recordset(v.inputs) AS ie(input_kind text,input_hash text,evidence jsonb)
        JOIN source_items zi ON zi.source_item_id::text=ze.evidence->'source_ref'->'key'->>'source_item_id'
        JOIN source_items ii ON ii.source_item_id::text=ie.evidence->'source_ref'->'key'->>'source_item_id'
        WHERE he.input_kind='holding'
          AND he.evidence->'source_ref'->'key'->>'id'=h.id::text
          AND ze.input_kind='source_item'
          AND ze.evidence->>'role'='ibkr_zero_position'
          AND ie.input_kind='source_item'
          AND ie.evidence->>'role'='ibkr_reviewed_contract_identity'
          AND h.account_code='ibkr' AND h.quantity=0 AND h.cost=0 AND h.market_value=0
          AND NOT EXISTS (SELECT 1 FROM pams_holdings_snapshot newer
              WHERE newer.account_code=h.account_code AND newer.name=h.name AND newer.as_of>h.as_of)
        ORDER BY h.as_of DESC,h.id DESC LIMIT 5000""")
    candidates=cur.fetchall()
    output=[]
    seen=set()
    for row in candidates:
        if row['id'] in seen:
            continue
        ref={'batch_id':row['zero_batch'],'item_id':row['zero_item'],
             'identity_batch_id':row['identity_batch'],'identity_item_id':row['identity_item']}
        if not _ibkr_retirement_replayed(cur,old=row,ref=ref,business_date=row['as_of'],
                                         report_sha256=row['source_sha256'],read_only=True):
            continue
        key=(row['account_code'],str(row['symbol'] or ''),str(row['name'] or ''),
             str(row['major_category'] or ''),str(row['minor_category'] or ''))
        if key in (current_keys or set()):
            continue
        seen.add(row['id'])
        output.append({'account':row['account_code'],'type':row['minor_category'],'symbol':row['symbol'],
            'name':row['name'],'currency':row['currency'],'market_value':row['market_value'],
            'market_value_native':row['market_value'],'market_value_cny':row['market_value'],
            'cost':None,'cost_cny':None,'holding_pnl':None,'holding_pnl_native':None,'holding_pnl_cny':None,
            'daily_pnl':None,'daily_pnl_cny':None,'major_category':row['major_category'],
            'minor_category':row['minor_category'],'as_of':row['as_of'],'snapshot_as_of':row['as_of'],
            'quantity':row['quantity'],'buy_price':None,'current_price':None,'is_closed':True,
            'closed_date':row['as_of'],'closed_evidence':'confirmed_zero_and_immutable_identity'})
        if len(output)>=limit:
            break
    return output
