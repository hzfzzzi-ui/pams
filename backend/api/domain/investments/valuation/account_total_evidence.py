"""A reviewed account total is a dated portfolio observation, not cash or a holding."""
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from domain.investments.valuation.valuation_input_evidence import _bound_input, _hash, record_result_version, verify_recorded_version

VERSION = 'account-total-observation-v1'


def _conflicts_with_empty(cur, account, day):
    cur.execute("""SELECT EXISTS (SELECT 1 FROM pams_holdings_snapshot
        WHERE account_code=%s AND as_of=%s AND (market_value IS DISTINCT FROM 0 OR quantity<>0)) AS conflict""",
        (account, day))
    return cur.fetchone()['conflict']


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
    """Publish a reviewed total or an explicit confirmation of an empty account."""
    from domain.imports.isolation import source_enabled
    if cur.connection.autocommit or not source_enabled(cur):
        raise ValueError('账户总额发布需要受保护事务')
    cur.execute('''SELECT r.*, a.active,a.is_asset,a.base_currency FROM source_records r
        JOIN pams_accounts a ON a.account_code=r.account_code WHERE r.source_id=%s FOR UPDATE OF r''', (str(source_id),))
    r = cur.fetchone()
    if not r or r['revision'] != revision or r['status'] != 'draft' or not r['active'] or not r['is_asset']:
        raise ValueError('账户来源版本或账户状态无效')
    if r['document_type'] != 'statement':
        raise ValueError('账户总额须有已核对的账单或空仓确认')
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
    if r['acquisition_method'] == 'manual':
        if (r['content'] is not None or r['sha256'] is not None or item['parse_method'] != 'manual'
                or original != fields or not (item['review_note'] or '').strip()):
            raise ValueError('本人确认须绑定完整录入字段、明确的日期和核对说明')
        if fields.get('attestation') == 'account_empty':
            if (fields['value_scope'] != 'account_total' or value['covered_account_codes'] != [r['account_code']]
                    or Decimal(value['net_asset']) != 0 or _conflicts_with_empty(cur, r['account_code'], day)):
                raise ValueError('空仓确认必须覆盖当日本账户所有持仓及现金')
            value['confirmed_empty'] = True
        elif fields.get('attestation') == 'complete_reconstructed_account':
            from domain.investments.valuation.reviewed_account_scope import verify_scope
            verify_scope(cur, value, fields)
        elif not _verified_unchanged(cur, value, fields, locking=True):
            raise ValueError('须引用同范围、同金额的原始账户总额并明确确认当日总额未变')
    elif (r['acquisition_method'] != 'screenshot' or not r['content']
            or sha256(bytes(r['content'])).hexdigest() != r['sha256']):
        raise ValueError('账户总额截图原件校验失败')
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


def load(cur, *, dates, accounts, source_accounts=None):
    """Return only unchanged, uniquely valued same-day observations."""
    if not dates or not accounts:
        return {}
    cur.execute('''SELECT version_key,account_code,business_date,base_currency,result_hash,inputs
        FROM valuation_evidence WHERE result_kind='portfolio' AND calculation_version=%s
          AND account_code=ANY(%s::text[]) AND business_date=ANY(%s::date[])''',
                (VERSION, list(accounts if source_accounts is None else set(accounts) & set(source_accounts)), list(dates)))
    rows = cur.fetchall(); candidates = {}
    cur.execute("SELECT current_setting('transaction_read_only')='on' AS readonly, current_setting('transaction_isolation') AS level")
    mode = cur.fetchone()
    locking = not (mode['readonly'] and mode['level'] in {'repeatable read', 'serializable'})
    for r in rows:
        try:
            current = _verified_source(cur, r, locking)
            if current is None:
                continue
            value, fields, method = current
            if method == 'manual':
                if fields.get('attestation') == 'account_empty':
                    if _conflicts_with_empty(cur, r['account_code'], r['business_date']):
                        continue
                    value['confirmed_empty'] = True
                elif fields.get('attestation') == 'complete_reconstructed_account':
                    from domain.investments.valuation.reviewed_account_scope import verify_scope
                    verify_scope(cur, value, fields)
                elif not _verified_unchanged(cur, value, fields, locking=locking):
                    continue
            if _hash(value) != r['result_hash'] or not set(value['covered_account_codes']) <= set(accounts):
                continue
            if method == 'screenshot' or fields.get('attestation') == 'unchanged_account_total':
                value['observed_total'] = True
            candidates.setdefault((r['business_date'],r['account_code']), []).append(value)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return {key: values[0] for key,values in candidates.items() if len({_hash(value) for value in values}) == 1}


def prior_single_account_total(cur, account, before, currency):
    """Recheck the latest earlier observation using the same scope and source rules."""
    cur.execute("""SELECT MAX(business_date) AS day FROM valuation_evidence
        WHERE result_kind='portfolio' AND calculation_version=%s AND account_code=%s
          AND base_currency=%s AND business_date<%s""", (VERSION, account, currency, before))
    day = cur.fetchone()['day']
    if day:
        total = load(cur, accounts=[account], dates=[day]).get((day, account))
        if total:
            return day, Decimal(total['net_asset'])
    return None


def _verified_source(cur, row, locking):
    if not verify_recorded_version(cur, row['version_key']) or len(row['inputs']) != 1:
        return None
    saved = row['inputs'][0]['evidence']
    current, raw = _bound_input(cur, 'source_item', saved['source_ref']['key'], locking=locking)
    snap = current['snapshot']
    if (not raw or saved.get('role') != 'account_total_source'
            or current['source_ref'].get('account_code') != row['account_code']
            or snap['batch_status'] != 'published' or snap['review_status'] != 'confirmed'
            or snap['business_type'] != 'statement' or snap.get('source_integrity') != 'verified'
            or snap['content_hash'] != saved['snapshot']['content_hash']):
        return None
    fields = snap['source_fields']
    value = _value(fields, row['account_code'], row['business_date'], row['base_currency'])
    return value, fields, raw['batch']['acquisition_method']


def _verified_unchanged(cur, value, fields, *, locking):
    """A dated user confirmation may point only to a freshly verified original observation."""
    key = fields.get('prior_version_key')
    if fields.get('attestation') != 'unchanged_account_total' or not isinstance(key, str) or not key:
        return False
    cur.execute('''SELECT version_key,account_code,business_date,base_currency,result_hash,inputs
        FROM valuation_evidence WHERE version_key=%s AND result_kind='portfolio' AND calculation_version=%s
          AND account_code=%s AND base_currency=%s AND business_date<%s''',
        (key, VERSION, value['account_code'], value['currency'], value['business_date']))
    anchor = cur.fetchone()
    if not anchor:
        return False
    proof = _verified_source(cur, anchor, locking)
    if not proof:
        return False
    original, prior_fields, method = proof
    if (method != 'screenshot' or _hash(original) != anchor['result_hash']
            or prior_fields['value_scope'] != fields['value_scope']
            or set(original['covered_account_codes']) != set(value['covered_account_codes'])
            or Decimal(original['net_asset']) != Decimal(value['net_asset'])):
        return False
    cur.execute('''SELECT version_key,account_code,business_date,base_currency,result_hash,inputs
        FROM valuation_evidence WHERE result_kind='portfolio' AND calculation_version=%s
          AND account_code=%s AND business_date=%s AND version_key<>%s''',
        (VERSION, anchor['account_code'], anchor['business_date'], key))
    for sibling in cur.fetchall():
        checked = _verified_source(cur, sibling, locking)
        if checked and checked[2] == 'screenshot' and _hash(checked[0]) == sibling['result_hash']:
            if sibling['result_hash'] != anchor['result_hash'] or checked[1]['value_scope'] != prior_fields['value_scope']:
                return False
    return True
