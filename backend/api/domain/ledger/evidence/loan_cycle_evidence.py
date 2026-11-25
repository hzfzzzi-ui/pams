"""User-attested borrowing cycles backed by immutable, already booked bank entries."""
from collections import Counter
from datetime import date
from decimal import Decimal
from uuid import uuid4

from domain.imports.sources.source_fact_evidence import _digest
from domain.imports.sources.source_records_v2 import add_manual_item, create_source, review_item
from domain.ledger.cashflow.cashflow_read_v2 import load_cash_rows


PURPOSE = 'borrower_cycle_v1'
PRINCIPAL = 'interest_free_draw_and_payoff'


def _bank_ref(row):
    proof = row.get('pair_evidence') or {}
    if row.get('source_evidence_status') != 'verified' or not proof.get('source_hash'):
        return None
    return dict(id=row['id'], account=row['account_code'], currency=row['currency'],
                amount=str(row['amount']), day=row['transaction_date'].isoformat(),
                item=str(row['source_item_id']), original=str(proof['source_id']), sha256=proof['source_hash'])


def _validate(fields, rows):
    refs = fields['cash']
    if (not isinstance(refs, list) or not refs or not isinstance(fields.get('loan'), str)
            or not fields['loan'].strip() or not isinstance(fields.get('declaration'), str) or not fields['declaration'].strip()
            or any(not isinstance(ref, dict) or type(ref.get('id')) is not int for ref in refs)
            or len({ref['id'] for ref in refs}) != len(refs)
            or any(ref['id'] not in rows or ref != _bank_ref(rows[ref['id']]) for ref in refs)):
        raise ValueError('借款声明与银行原件不符')
    end, start = date.fromisoformat(fields['through']), date.fromisoformat(fields['from'])
    for ref in refs:
        row = rows[ref['id']]
        if row.get('classification_status') == 'confirmed':
            appropriate = 'debt_drawdown' if row['amount'] > 0 else 'debt_repayment'
            if row.get('cashflow_type') != appropriate:
                raise ValueError('借款声明与已有核准分类冲突')
    if not all(start <= date.fromisoformat(ref['day']) <= end and ref['currency'] == fields['currency'] for ref in refs):
        raise ValueError('借款声明日期或币种不符')
    if (fields['scope'] != PRINCIPAL or len(refs) != 2 or Decimal(refs[0]['amount']) <= 0
            or refs[0]['day'] != start.isoformat() or refs[1]['day'] != end.isoformat()
            or Decimal(refs[0]['amount']) != -Decimal(refs[1]['amount'])):
        raise ValueError('无息本金须由完整借入、还清两笔原件证明')


def _rows(cur, ids):
    return {row['id']: row for row in load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY',
            fact_ids=sorted(ids), include_pair_evidence=True)} if ids else {}


def _sources(cur):
    cur.execute("SELECT to_regclass('public.account_snapshot_version') IS NOT NULL AS available")
    if not cur.fetchone()['available']:
        return []
    cur.execute("""SELECT s.source_id,s.account_code,i.source_item_id,i.parsed_fields AS fields,i.raw_fields,
                          i.record_hash,i.review_note
        FROM source_records s JOIN source_items i USING(source_id) JOIN pams_accounts a USING(account_code)
        WHERE s.status='published' AND i.review_status='confirmed' AND NOT a.is_asset
          AND s.acquisition_method='manual' AND i.parse_method='manual' AND s.document_type='statement'
          AND EXISTS (SELECT 1 FROM account_snapshot_version v WHERE v.account_code=s.account_code
                      AND v.scope_attestation->>'liability_scope'='all_personal_loan_principal')
          AND s.content IS NULL AND s.sha256 IS NULL AND i.parsed_fields->>'purpose'=%s""", (PURPOSE,))
    return cur.fetchall()


def read(cur, *, paired=None):
    sources = _sources(cur)
    if not sources:
        return []
    from domain.ledger.transfers.confirmed_transfers_v2 import paired_cashflow_ids
    occupied = paired if paired is not None else paired_cashflow_ids(cur)
    claims = Counter()
    for record in sources:
        refs = record['fields'].get('cash')
        if isinstance(refs, list):
            claims.update(ref['id'] for ref in refs if isinstance(ref, dict) and type(ref.get('id')) is int)
    rows = _rows(cur, claims)
    result = []
    for record in sources:
        f = record['fields']
        if (not record['review_note'] or record['review_note'] != f.get('declaration') or record['raw_fields'] != {'entered_fields': f}
                or _digest(record['raw_fields']) != record['record_hash']):
            continue
        try:
            _validate(f, rows)
            if any(ref['id'] in occupied or claims[ref['id']] != 1 for ref in f['cash']):
                continue
        except (ValueError, KeyError, TypeError, ArithmeticError):
            continue
        result.append(dict(account_code=record['account_code'], source_item_id=str(record['source_item_id']), **f))
    return result


def record(cur, *, account_code, loan, scope, start, end, cash_ids, declaration, confirmed):
    """Keep the original bank rows and their classifications. Replay identical evidence once."""
    if (cur.connection.autocommit or confirmed is not True or not declaration.strip() or not loan.strip()
            or not cash_ids or any(type(i) is not int or i <= 0 for i in cash_ids)):
        raise ValueError('须明确确认借款声明并绑定原流水')
    cur.execute("""SELECT 1 FROM pams_accounts a WHERE a.account_code=%s AND NOT a.is_asset
        AND EXISTS (SELECT 1 FROM account_snapshot_version v WHERE v.account_code=a.account_code
        AND v.scope_attestation->>'liability_scope'='all_personal_loan_principal')""", (account_code,))
    if not cur.fetchone():
        raise ValueError('须指定负债账户')
    # Share the bank-account lock with transfers; serialize simultaneous declarations as well.
    cur.execute("SELECT DISTINCT account_code FROM pams_cashflow_fact WHERE cashflow_id=ANY(%s)", (list(cash_ids),))
    accounts = {r['account_code'] for r in cur.fetchall()} | {account_code}
    for code in sorted(accounts):
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('account-import:' + code,))
    rows = _rows(cur, cash_ids)
    refs = [_bank_ref(rows[ident]) if ident in rows else None for ident in cash_ids]
    if not all(refs):
        raise ValueError('缺少借款现金原件')
    fields = dict(purpose=PURPOSE, loan=loan.strip(), scope=scope, declaration=declaration.strip(),
                  currency=refs[0]['currency'], cash=refs, **{'from':start.isoformat(), 'through':end.isoformat()})
    _validate(fields, rows)
    existing = _sources(cur)
    for previous in existing:
        captured = previous['fields']
        if (captured.get('loan') == fields['loan'] and previous['account_code'] == account_code
                and captured['from'] <= fields['through'] and fields['from'] <= captured['through']
                or any(ref['id'] in cash_ids for ref in captured.get('cash', []))):
            if (previous['account_code'] == account_code and captured == fields
                    and any(c['source_item_id'] == str(previous['source_item_id']) for c in read(cur))):
                return str(previous['source_item_id']), False
            raise ValueError('既有借款声明与本次确认冲突')
    from domain.ledger.transfers.confirmed_transfers_v2 import occupied_cashflow_ids
    if occupied_cashflow_ids(cur).intersection(cash_ids):
        raise ValueError('现金原件已绑定其他资金关系')
    source_id, item_id = uuid4(), uuid4()
    create_source(cur, source_id=source_id, account_code=account_code, source_name='本人核准的借款事实',
                  acquisition_method='manual', document_type='statement')
    version = add_manual_item(cur, source_id=source_id, item_id=item_id, expected_revision=1, parsed_fields=fields)
    version = review_item(cur, source_id=source_id, item_id=item_id, expected_revision=version,
                          parsed_fields=fields, review_status='confirmed', note=fields['declaration'])
    cur.execute("""UPDATE source_records SET status='published',revision=revision+1,updated_at=NOW()
        WHERE source_id=%s AND status='draft' AND revision=%s""", (str(source_id), version))
    return str(item_id), True


def principal_ids(cycles):
    return {ref['id'] for cycle in cycles for ref in cycle['cash']}
