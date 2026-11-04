"""Publish reviewed holding sources and freeze their account scope atomically."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import hmac
import json
import time
from zoneinfo import ZoneInfo

from domain.assets.alias_policy import identity_alias_sql
from domain.imports import native_source, native_source_http
from domain.imports.account_import_types import asset_type
from domain.imports.source_records_v2 import SourceRevisionConflict
from domain.investments.account_snapshot_evidence import record_source_scope
from domain.investments.holding_evidence_writer import finish_holding_change, lock_holding_accounts


def _amount(fields, key, scale):
    try:
        value = Decimal(str(fields[key]))
        if value.is_finite() and 0 <= value < Decimal(10) ** (20 - scale) and value == value.quantize(Decimal(1).scaleb(-scale)):
            return value
    except (KeyError, InvalidOperation, ValueError):
        pass
    raise ValueError(f'持仓字段 {key} 缺失或数值无效')


def _holding(cur, fields, account, day, item_id, *, complete):
    kind = asset_type(fields.get('asset_type'))
    base = dict(account_code=account, as_of=day, currency='CNY', asset_liability='资产', source_item_id=str(item_id))
    if fields.get('currency') != 'CNY':
        raise ValueError('持仓资料币种须为人民币')
    if kind == 'cash':
        amount = _amount(fields, 'net_cash', 4)
        if amount >= Decimal('1e14'):
            raise ValueError('现金持仓超出存储范围')
        return dict(base, symbol='CASH_CNY', name='Cash CNY', asset_id=None, major_category='现金',
                    minor_category='现金/活期', quantity=amount, current_price=Decimal(1), cost=amount,
                    market_value=amount, buy_price=Decimal(1))
    if kind not in {'stock', 'fund'} or fields.get('cost_basis_type') != 'holding_cost':
        raise ValueError('请核对标的类型与持仓成本口径')
    cur.execute(f"""SELECT m.asset_id,m.primary_symbol,m.canonical_name,m.asset_type FROM pams_asset_master m
        WHERE m.status='active' AND m.currency='CNY'
          AND (%s::text IS NULL OR m.asset_id=%s)
          AND (m.primary_symbol=%s OR EXISTS (
            SELECT 1 FROM pams_asset_alias al WHERE al.asset_id=m.asset_id AND al.alias_value=%s
            AND {identity_alias_sql('al')})) LIMIT 2""",
                (fields.get('asset_id'), fields.get('asset_id'), fields.get('symbol'), fields.get('symbol')))
    assets = cur.fetchall()
    if len(assets) != 1 or asset_type(assets[0]['asset_type']) != kind:
        raise ValueError('持仓标的须唯一匹配资产底表')
    asset = assets[0]
    quantity, price = _amount(fields, 'quantity', 6), _amount(fields, 'price', 6)
    cost, value = _amount(fields, 'cost', 4), _amount(fields, 'market_value', 4)
    if abs(quantity * price - value) > Decimal('0.02') or (quantity == 0 and (not complete or value or cost)):
        raise ValueError('持仓数量、市值和价格不一致，或零持仓缺少完整范围确认')
    buy_price = (cost / quantity).quantize(Decimal('0.000001')) if quantity else Decimal(0)
    if buy_price >= Decimal('1e14'):
        raise ValueError('持仓单位成本超出存储范围')
    return dict(base, symbol=asset['primary_symbol'], name=asset['canonical_name'], asset_id=asset['asset_id'],
                major_category='A股', minor_category='A股基金' if kind == 'fund' else 'A股',
                quantity=quantity, current_price=price, cost=cost, market_value=value, buy_price=buy_price)


def _plan(cur, source_id, revision, scope):
    cur.execute("SELECT account_code FROM source_records WHERE source_id=%s", (str(source_id),))
    account_row = cur.fetchone()
    if not account_row:
        raise ValueError('持仓来源不存在')
    account = account_row['account_code']
    lock_holding_accounts(cur, [account])
    cur.execute("""SELECT r.*, public.source_original_content(r.account_code,r.sha256,r.content) AS original_content,
        a.active, a.base_currency FROM source_records r
        JOIN pams_accounts a ON a.account_code=r.account_code WHERE r.source_id=%s FOR UPDATE OF r""",
                (str(source_id),))
    source = cur.fetchone()
    if not source or source['revision'] != revision or source['status'] != 'draft':
        raise SourceRevisionConflict('持仓来源版本已变化')
    if source['document_type'] != 'holding' or not source['active'] or source['base_currency'] != 'CNY':
        raise ValueError('目标账户或资料不支持人民币持仓发布')
    if source['original_content'] is not None and sha256(bytes(source['original_content'])).hexdigest() != source['sha256']:
        raise ValueError('持仓原件校验失败')
    cur.execute("""SELECT source_item_id,review_status,raw_fields,parsed_fields,record_hash FROM source_items
        WHERE source_id=%s ORDER BY source_item_id""", (str(source_id),))
    items = cur.fetchall()
    holdings = []
    for item in items:
        if item['review_status'] == 'excluded' and scope is None:
            continue
        if item['review_status'] != 'confirmed':
            raise ValueError('请先完成持仓核对，完整范围须包含来源中的全部记录')
        raw = json.dumps(item['raw_fields'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if sha256(raw.encode()).hexdigest() != item['record_hash']:
            raise ValueError('持仓明细校验失败')
        fields = item['parsed_fields']
        try:
            day = date.fromisoformat(fields['business_date'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('持仓业务日期无效') from exc
        if (day > datetime.now(ZoneInfo('Asia/Shanghai')).date()
                or source['period_start'] and day < source['period_start']
                or source['period_end'] and day > source['period_end']):
            raise ValueError('持仓业务日期超出来源范围')
        holdings.append(_holding(cur, fields, account, day, item['source_item_id'], complete=scope is not None))
    if not holdings or len({row['as_of'] for row in holdings}) != 1:
        raise ValueError('每次发布须包含同一日期的持仓')
    names = [row['name'] for row in holdings]
    symbols = {row['symbol'] for row in holdings}
    if len(set(names)) != len(holdings) or len(symbols) != len(holdings):
        raise ValueError('持仓标的或名称重复')
    day = holdings[0]['as_of']
    cur.execute("SELECT as_of,name,symbol FROM pams_holdings_snapshot WHERE account_code=%s AND as_of >= %s", (account, day))
    if any(row['as_of'] > day or scope is not None or row['name'] in names or row['symbol'] in symbols for row in cur.fetchall()):
        raise ValueError('持仓与现有快照冲突，须先核对已有记录')
    if scope is not None:
        cash = [row for row in holdings if row['major_category'] == '现金']
        if (scope['confirmed'] is not True or not scope['note'].strip() or scope['as_of'] != day.isoformat()
                or scope['member_count'] != len(holdings) or len(cash) != 1
                or _amount(scope, 'cash_amount', 4) != cash[0]['market_value']):
            raise ValueError('完整范围须明确确认日期、成员数、现金及备注')
    return {'source_id': str(source_id), 'revision': revision, 'holdings': holdings, 'scope': scope}


def _token(plan, bucket):
    payload = json.dumps([plan, bucket], sort_keys=True, separators=(',', ':'), default=str).encode()
    return hmac.new(native_source_http._token_key(), b'pams-holding-source-v1:' + payload, sha256).hexdigest()


def preview(cur, *, source_id, revision, scope=None):
    plan = _plan(cur, source_id, revision, scope)
    return {**plan, 'preview_token': _token(plan, int(time.time() // 30))}


def publish(cur, *, source_id, revision, preview_token, scope=None):
    if not native_source.enabled() or cur.connection.autocommit:
        raise ValueError('持仓发布未启用')
    plan = _plan(cur, source_id, revision, scope)
    now = int(time.time() // 30)
    if not any(hmac.compare_digest(preview_token, _token(plan, now - n)) for n in range(31)):
        raise ValueError('持仓预检已失效，请重新核对')
    return publish_validated_holdings(cur, source_id=source_id, as_of=plan['holdings'][0]['as_of'],
                                      holdings=plan['holdings'], scope=scope)


def publish_validated_holdings(cur, *, source_id, as_of, holdings, scope=None):
    """Write a caller-validated snapshot under the existing account lock and transaction."""
    cur.execute('''UPDATE source_records SET status='published',revision=revision+1,updated_at=now()
        WHERE source_id=%s AND status='draft' AND document_type='holding' ''', (str(source_id),))
    if cur.rowcount != 1:
        raise SourceRevisionConflict('持仓来源不再可发布')
    holding_ids = []
    for row in holdings:
        cur.execute("""INSERT INTO pams_holdings_snapshot
            (account_code,as_of,symbol,name,asset_id,currency,asset_liability,major_category,minor_category,
             quantity,current_price,cost,market_value,buy_price,source_item_id)
            VALUES (%(account_code)s,%(as_of)s,%(symbol)s,%(name)s,%(asset_id)s,%(currency)s,%(asset_liability)s,
                    %(major_category)s,%(minor_category)s,%(quantity)s,%(current_price)s,%(cost)s,
                    %(market_value)s,%(buy_price)s,%(source_item_id)s) RETURNING id""", row)
        holding_id = cur.fetchone()['id']
        finish_holding_change(cur, holding_id=holding_id, source_item_id=row['source_item_id'])
        holding_ids.append(holding_id)
    version = record_source_scope(cur, source_id=source_id, holding_ids=holding_ids,
                                  as_of=as_of, scope=scope) if holding_ids else None
    return {'published': len(holding_ids), 'snapshot_version_id': version,
            'coverage_status': 'complete' if scope is not None else 'partial'}
