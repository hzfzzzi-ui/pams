"""A reviewed account total is a dated portfolio observation, not cash or a holding."""
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from domain.investments.valuation_input_evidence import _bound_input, _hash, record_result_version, verify_recorded_version

VERSION = 'account-total-observation-v1'


def _value(fields, account, day, currency):
    try:
        covered = fields['covered_account_codes']
        amount = Decimal(str(fields['net_asset']))
        if (fields['kind'] != 'account_portfolio_value' or fields.get('value_scope') not in {'account_total', 'platform_total'}
                or fields['business_date'] != day.isoformat()
                or fields['currency'] != currency or not amount.is_finite() or amount < 0
                or not isinstance(covered, list) or not covered or len(covered) > 100 or len(set(covered)) != len(covered)
                or any(not isinstance(c, str) or not c.strip() for c in covered) or account not in covered):
            raise ValueError
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError('账户总额日期、币种、范围或金额无效') from exc
    return {'account_code': account, 'business_date': day.isoformat(), 'currency': currency,
            'net_asset': str(amount), 'covered_account_codes': covered}


def publish(cur, *, source_id, revision):
    """Publish an already reviewed screenshot and freeze its valuation evidence atomically."""
    from domain.imports.isolation import source_enabled
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('账户总额发布需要受保护事务')
    cur.execute('''SELECT r.*, a.active,a.is_asset,a.base_currency FROM source_records r
        JOIN pams_accounts a ON a.account_code=r.account_code WHERE r.source_id=%s FOR UPDATE OF r''', (str(source_id),))
    r = cur.fetchone()
    if not r or r['revision'] != revision or r['status'] != 'draft' or not r['active'] or not r['is_asset']:
        raise ValueError('账户来源版本或账户状态无效')
    if (r['document_type'] != 'statement' or r['acquisition_method'] != 'screenshot' or not r['content']
            or sha256(bytes(r['content'])).hexdigest() != r['sha256']):
        raise ValueError('账户总额截图原件校验失败')
    cur.execute('SELECT * FROM source_items WHERE source_id=%s FOR UPDATE', (str(source_id),))
    items = cur.fetchall()
    if len(items) != 1 or items[0]['review_status'] != 'confirmed':
        raise ValueError('账户总额须核对唯一来源明细')
    item = items[0]; raw = item['raw_fields']; fields = item['parsed_fields']
    digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    original = raw.get('entered_fields')
    if (digest != item['record_hash'] or not isinstance(original, dict)
            or any(fields.get(key) != value for key, value in original.items())):
        raise ValueError('账户总额明细已变化')
    try:
        day = date.fromisoformat(fields['business_date'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('账户总额日期无效') from exc
    value = _value(fields, r['account_code'], day, r['base_currency'])
    cur.execute('SELECT account_code FROM pams_accounts WHERE active AND is_asset AND account_code=ANY(%s::text[])', (value['covered_account_codes'],))
    if {x['account_code'] for x in cur.fetchall()} != set(value['covered_account_codes']):
        raise ValueError('账户总额覆盖账户不存在')
    cur.execute("UPDATE source_records SET status='published',revision=revision+1,updated_at=now() WHERE source_id=%s", (str(source_id),))
    inp, _ = _bound_input(cur, 'source_item', {'source_item_id':str(item['source_item_id'])})
    inp['role'] = 'account_total_source'
    version = record_result_version(cur, result_kind='portfolio', scope_key='account-total:'+r['account_code'],
        account_code=r['account_code'], business_date=day, base_currency=r['base_currency'],
        calculation_version=VERSION, result_hash=_hash(value), inputs=[inp])
    return {'version_key':version, **value}


def load(cur, *, dates, accounts):
    """Return only unchanged, uniquely valued same-day observations."""
    if not dates or not accounts:
        return {}
    cur.execute('''SELECT version_key,account_code,business_date,base_currency,result_hash,inputs
        FROM valuation_evidence WHERE result_kind='portfolio' AND calculation_version=%s
          AND account_code=ANY(%s::text[]) AND business_date=ANY(%s::date[])''', (VERSION, list(accounts), list(dates)))
    rows = cur.fetchall(); candidates = {}
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    locking = not (mode['readonly'] and mode['level'] in {'repeatable read', 'serializable'})
    for r in rows:
        try:
            if not verify_recorded_version(cur, r['version_key']) or len(r['inputs']) != 1:
                continue
            saved = r['inputs'][0]['evidence']; key = saved['source_ref']['key']
            current, _ = _bound_input(cur, 'source_item', key, locking=locking)
            snap = current['snapshot']; old = saved['snapshot']; fields = snap['source_fields']
            if (saved.get('role') != 'account_total_source' or current['source_ref'].get('account_code') != r['account_code']
                    or snap['batch_status'] != 'published' or snap['review_status'] != 'confirmed'
                    or snap['business_type'] != 'statement' or snap.get('source_integrity') != 'verified'
                    or snap['content_hash'] != old['content_hash']):
                continue
            value = _value(fields, r['account_code'], r['business_date'], r['base_currency'])
            if _hash(value) != r['result_hash'] or not set(value['covered_account_codes']) <= set(accounts):
                continue
            candidates.setdefault((r['business_date'],r['account_code']), []).append(value)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return {key: values[0] for key,values in candidates.items() if len({_hash(value) for value in values}) == 1}
