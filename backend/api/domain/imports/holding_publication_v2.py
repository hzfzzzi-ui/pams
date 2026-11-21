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


def _holding(cur, fields, account, day, item_id, *, complete, historical=False):
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
    if kind not in {'stock', 'fund'} or (fields.get('cost_basis_type') != 'holding_cost' and not (historical and fields.get('cost_basis_type') == 'unknown')):
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
    value = _amount(fields, 'market_value', 4)
    cost = None if historical and fields.get('cost_basis_type') == 'unknown' and fields.get('cost') in (None, '') else _amount(fields, 'cost', 4)
    if historical and fields.get('cost_basis_type') == 'unknown' and cost is not None:
        raise ValueError('未知成本不能填写数值')
    if abs(quantity * price - value) > Decimal('0.02') or (quantity == 0 and (not complete or value or cost)):
        raise ValueError('持仓数量、市值和价格不一致，或零持仓缺少完整范围确认')
    buy_price = ((cost / quantity).quantize(Decimal('0.000001')) if quantity else Decimal(0)) if cost is not None else None
    if buy_price is not None and buy_price >= Decimal('1e14'):
        raise ValueError('持仓单位成本超出存储范围')
    return dict(base, symbol=asset['primary_symbol'], name=asset['canonical_name'], asset_id=asset['asset_id'],
                major_category='A股', minor_category='A股基金' if kind == 'fund' else 'A股',
                quantity=quantity, current_price=price, cost=cost, market_value=value, buy_price=buy_price)


def _historical_fund_inputs(cur, fields, holding):
    """Bind a reviewed one-day reconstruction to verified trades and daily NAV."""
    from domain.investments.fund_return_history import NAV_SOURCES
    from domain.investments.trade_read_v2 import load_trade_rows

    evidence = fields.get('reconstruction_evidence')
    if not isinstance(evidence, dict) or set(evidence) != {'trade_ids', 'quote_id'}:
        raise ValueError('历史基金重建须提供交易与同日净值证据')
    ids, quote_id = evidence['trade_ids'], evidence['quote_id']
    if (not isinstance(ids, list) or not ids or len(ids) > 2000
            or any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids)
            or type(quote_id) is not int or quote_id <= 0):
        raise ValueError('历史基金证据身份无效')
    cur.execute("""SELECT quote_id,symbol,asset_id,as_of,currency,unit_nav,source FROM market_observations
        WHERE quote_id=%s AND observation_type='daily' AND market='基金' FOR SHARE""", (quote_id,))
    quote = cur.fetchone()
    if (not quote or quote['symbol'] != holding['symbol'] or quote['as_of'] != holding['as_of']
            or quote['currency'] != 'CNY' or quote['source'] not in NAV_SOURCES
            or quote['unit_nav'] != holding['current_price']
            or quote['asset_id'] not in (None, holding['asset_id'])):
        raise ValueError('历史基金净值证据不匹配')
    cur.execute("""SELECT COUNT(DISTINCT unit_nav) AS count FROM market_observations
        WHERE observation_type='daily' AND market='基金' AND symbol=%s AND as_of=%s
          AND currency='CNY' AND source=ANY(%s) AND unit_nav>0""",
        (holding['symbol'], holding['as_of'], list(NAV_SOURCES)))
    if cur.fetchone()['count'] != 1:
        raise ValueError('历史基金净值存在冲突')
    refs = [{'kind': 'quote', 'key': {'quote_id': quote_id}, 'role': 'historical_daily_nav'}]
    total = Decimal(0)
    source_ids = set()
    for ident in ids:
        rows = load_trade_rows(cur, account_code=holding['account_code'], start=date.min,
                               end=holding['as_of'], fact_id=ident, include_fund_execution=True)
        if len(rows) != 1:
            raise ValueError('历史基金交易证据不存在或日期不符')
        row = rows[0]
        if (row['source_evidence_status'] != 'verified' or not row['fund_execution_confirmed']
                or row['asset_id'] != holding['asset_id'] or row['symbol'] != holding['symbol']
                or row['currency'] != 'CNY' or row['trade_type'] not in {'fund_subscription', 'fund_redemption'}
                or row['quantity'] is None or row['quantity'] <= 0):
            raise ValueError('历史基金交易尚未确认或标的不符')
        total += row['quantity'] if row['trade_type'] == 'fund_subscription' else -row['quantity']
        refs.append({'kind': 'trade', 'key': {'trade_id': ident}, 'role': 'historical_quantity'})
        source_ids.add(str(row['source_item_id']))
    refs.extend({'kind': 'source_item', 'key': {'source_item_id': item}, 'role': 'historical_trade_source'}
                for item in sorted(source_ids))
    if total != holding['quantity']:
        raise ValueError('历史基金交易份额与已确认份额不一致')
    return refs


def _plan(cur, source_id, revision, scope, historical=False):
    if historical and scope is not None:
        raise ValueError('历史单日证据不能声明完整范围')
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
    input_refs = {}
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
        holding = _holding(cur, fields, account, day, item['source_item_id'], complete=scope is not None, historical=historical)
        if fields.get('reconstruction_evidence') is not None:
            if not historical or holding['minor_category'] != 'A股基金':
                raise ValueError('重建证据仅用于历史单日基金')
            input_refs[str(item['source_item_id'])] = _historical_fund_inputs(cur, fields, holding)
        holdings.append(holding)
    if not holdings or len({row['as_of'] for row in holdings}) != 1:
        raise ValueError('每次发布须包含同一日期的持仓')
    names = [row['name'] for row in holdings]
    symbols = {row['symbol'] for row in holdings}
    if len(set(names)) != len(holdings) or len(symbols) != len(holdings):
        raise ValueError('持仓标的或名称重复')
    day = holdings[0]['as_of']
    cur.execute("SELECT as_of,name,symbol FROM pams_holdings_snapshot WHERE account_code=%s AND as_of >= %s", (account, day))
    if any((row['as_of'] > day and not historical) or (row['as_of'] == day and (scope is not None or row['name'] in names or row['symbol'] in symbols)) for row in cur.fetchall()):
        raise ValueError('持仓与现有快照冲突，须先核对已有记录')
    if scope is not None:
        cash = [row for row in holdings if row['major_category'] == '现金']
        if (scope['confirmed'] is not True or not scope['note'].strip() or scope['as_of'] != day.isoformat()
                or scope['member_count'] != len(holdings) or len(cash) != 1
                or _amount(scope, 'cash_amount', 4) != cash[0]['market_value']):
            raise ValueError('完整范围须明确确认日期、成员数、现金及备注')
    return {'source_id': str(source_id), 'revision': revision, 'holdings': holdings, 'scope': scope, 'historical': historical, 'input_refs': input_refs}


def _token(plan, bucket):
    payload = json.dumps([plan, bucket], sort_keys=True, separators=(',', ':'), default=str).encode()
    return hmac.new(native_source_http._token_key(), b'pams-holding-source-v1:' + payload, sha256).hexdigest()


def preview(cur, *, source_id, revision, scope=None, historical=False):
    plan = _plan(cur, source_id, revision, scope, historical)
    return {**plan, 'preview_token': _token(plan, int(time.time() // 30))}


def publish(cur, *, source_id, revision, preview_token, scope=None, historical=False):
    if not native_source.enabled() or cur.connection.autocommit:
        raise ValueError('持仓发布未启用')
    plan = _plan(cur, source_id, revision, scope, historical)
    now = int(time.time() // 30)
    if not any(hmac.compare_digest(preview_token, _token(plan, now - n)) for n in range(31)):
        raise ValueError('持仓预检已失效，请重新核对')
    return publish_validated_holdings(cur, source_id=source_id, as_of=plan['holdings'][0]['as_of'],
                                      holdings=plan['holdings'], scope=scope,
                                      partial_attestation={'point_in_time_only': True} if historical else None, input_refs=plan['input_refs'])


def publish_validated_holdings(cur, *, source_id, as_of, holdings, scope=None, partial_attestation=None, input_refs=None):
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
        finish_holding_change(cur, holding_id=holding_id, source_item_id=row['source_item_id'],
                              input_refs=(input_refs or {}).get(str(row['source_item_id'])))
        holding_ids.append(holding_id)
    version = record_source_scope(cur, source_id=source_id, holding_ids=holding_ids,
                                  as_of=as_of, scope=scope,
                                  partial_attestation=partial_attestation) if holding_ids else None
    return {'published': len(holding_ids), 'snapshot_version_id': version,
            'coverage_status': 'complete' if scope is not None else 'partial'}
