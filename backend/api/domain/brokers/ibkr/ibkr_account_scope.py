"""Explicitly reviewed IBKR account scope; no inferred cash, FX or ownership."""
from decimal import Context, Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from uuid import UUID, uuid4, uuid5
import xml.etree.ElementTree as ET
from psycopg2.extras import Json, execute_values

from domain.imports.ibkr_holding_source import _extract, _read_source_report, _scope_is_well_formed, _number
from domain.imports.source_fact_evidence import _reviewed_snapshot
from domain.investments.valuation_input_evidence import _hash as holding_hash

from domain.brokers.ibkr.ibkr_instrument_types import SECURITY_COMPONENTS, SECURITY_LABELS


def _cash_amounts(content, *, source_account_id, day):
    root=ET.fromstring(content)
    statements=[n for n in root.iter() if n.tag.rsplit('}',1)[-1]=='FlexStatement'
                and n.get('accountId')==source_account_id and (n.get('toDate') or '').replace('-','')==day.strftime('%Y%m%d')]
    if len(statements)!=1:
        raise ValueError('account cash statement scope unavailable')
    statement=statements[0]
    sections=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]=='CashReport']
    if len(sections)!=1:
        raise ValueError('complete cash section required')
    entries=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]=='CashReportCurrency']
    if not entries or any(n not in set(sections[0].iter()) for n in entries):
        raise ValueError('cash entries outside complete section')
    parents={child:parent for parent in statement.iter() for child in parent}
    cash: dict[str, Decimal]={}
    summaries=[]
    for node in entries:
        ancestor: ET.Element | None=node
        while ancestor is not None:
            attrs={str(k).lower():str(v).strip() for k,v in ancestor.attrib.items()}
            if (len(attrs)!=len(ancestor.attrib) or attrs.get('model') or
                any(attrs.get(k) not in (None,'',source_account_id) for k in ('accountid','acctid'))):
                raise ValueError('cash account or model scope ambiguous')
            ancestor=parents.get(ancestor)
        currency=(node.get('currency') or '').strip()
        if currency=='BASE_SUMMARY':
            if (node.get('toDate') or '').replace('-','')!=day.strftime('%Y%m%d') or _number(node.attrib,('endingCash',)) is None:
                raise ValueError('cash summary scope or amount invalid')
            summaries.append(node)
            if len(summaries)>1: raise ValueError('cash summary is ambiguous')
            continue
        if (len(currency)!=3 or not currency.isascii() or not currency.isalpha() or currency!=currency.upper()
            or currency in cash or node.get('accountId') not in (None,'',source_account_id)
            or (node.get('toDate') or '').replace('-','')!=day.strftime('%Y%m%d')):
            raise ValueError('cash currency scope missing or ambiguous')
        value=_number(node.attrib,('endingCash',))
        if value is None:
            raise ValueError('cash amount missing')
        cash[currency]=value
    if not cash:
        raise ValueError('cash currency evidence missing')
    return cash


def _complete_nav(content, *, source_account_id, day, cash_amounts):
    """Require an independently complete report NAV and exact native FX bridges."""
    from domain.brokers.ibkr import ibkr_daily_close as daily
    from domain.brokers.ibkr import ibkr_account_audit as audit
    close=daily.parse_daily_close_evidence(content.decode('utf8'),account_id=source_account_id,as_of=day,
        expected_sha256=sha256(content).hexdigest())
    if close.get('status')!='ok':
        raise ValueError('complete official NAV evidence required')
    base=close['base_currency']; root=ET.fromstring(content)
    statements=[n for n in root.iter() if n.tag.rsplit('}',1)[-1]=='FlexStatement' and
                n.get('accountId')==source_account_id and (n.get('toDate') or '').replace('-','')==day.strftime('%Y%m%d')]
    if len(statements)!=1: raise ValueError('NAV statement is ambiguous')
    statement=statements[0]
    rates=audit._elements(statement,'ConversionRate'); fx_check=audit._check('fx')
    cash_nodes=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]=='CashReportCurrency' and n.get('currency')!='BASE_SUMMARY']
    positions=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]=='OpenPosition']
    conversions: dict[str, Decimal]={}
    def rate_for(node):
        currency=node.get('currency')
        rate=audit._rate(fx_check,node,rates,currency,base,day)
        if rate is None or rate<=0 or not rate.is_finite() or rate.adjusted()>64:
            raise ValueError('original report FX is unavailable')
        if currency in conversions and abs(conversions[currency]-rate)>daily.TOLERANCE:
            raise ValueError('original report FX conflicts')
        conversions[currency]=rate
        return rate
    with localcontext(Context(prec=160,Emax=512,Emin=-512)):
        cash_values=[cash_amounts[node.get('currency')]*rate_for(node) for node in cash_nodes]
        cash_total=sum(cash_values,Decimal(0))
        position_values=[]
        position_groups: dict[str,list[Decimal]]={field:[] for field in daily.POSITION_FIELDS}
        position_total=Decimal(0)
        for node in positions:
            category=SECURITY_COMPONENTS.get(node.get('assetCategory') or '')
            if category is None:
                raise ValueError('unsupported NAV security component')
            value=_number(node.attrib,('marketValue','positionValue','value','markValue'))
            if value is None or abs(value).adjusted()>64:
                raise ValueError('position value unavailable')
            converted=value*rate_for(node)
            position_values.append(converted)
            position_groups[category].append(converted)
            position_total+=converted
        components=close['component_proof']['components']
        summaries=[n for n in statement.iter() if n.tag.rsplit('}',1)[-1]=='CashReportCurrency' and n.get('currency')=='BASE_SUMMARY']
        if summaries and abs(_number(summaries[0].attrib,('endingCash',))-cash_total)>daily.TOLERANCE:
            raise ValueError('cash base summary differs')
        checks=close['component_proof']['subcomponent_checks']
        groups={'cash':cash_values,**position_groups,'total':cash_values+position_values}
        for field,values in groups.items():
            gross=checks.get(field+'_long_short')
            positive=sum((value for value in values if value>0),Decimal(0))
            negative=sum((value for value in values if value<0),Decimal(0))
            if (not isinstance(gross,dict) or abs(gross['long']-positive)>daily.TOLERANCE or
                abs(gross['short']-negative)>daily.TOLERANCE):
                raise ValueError('official gross NAV exposure differs or is missing')
            if field not in {'cash','total'} and abs(sum(values,Decimal(0))-components[field])>daily.TOLERANCE:
                raise ValueError('security component differs from report positions')
        unsupported=[field for field in daily.COMPONENT_FIELDS if field not in {'cash',*daily.POSITION_FIELDS}]
        if any(components[field]!=0 or not isinstance(checks.get(field+'_long_short'),dict) or
               checks[field+'_long_short'].get('long')!=0 or checks[field+'_long_short'].get('short')!=0
               for field in unsupported):
            raise ValueError('unsupported NAV components lack explicit gross zero evidence')
        if (abs(cash_total-components['cash'])>daily.TOLERANCE
            or abs(position_total-close['position_value_base'])>daily.TOLERANCE
            or abs(cash_total+position_total-close['nav_base'])>daily.TOLERANCE or fx_check['issue_count']):
            raise ValueError('official NAV includes unrepresented components or differs')
    return {'base_currency':base,'nav_base':str(close['nav_base']),'cash_base':str(cash_total),
            'positions_base':str(position_total),'fx_to_base':{k:str(v) for k,v in sorted(conversions.items())}}


def _current_members(cur,batch,scope,items,content):
    raw=scope['raw_fields']; account=raw['source_account_id']; day=batch['date_to']
    _, statement, positions=_extract(content,source_account_id=account,business_date=day)
    if statement!=raw.get('statement_attributes') or not _scope_is_well_formed(content,account,day):
        raise ValueError('position scope unavailable')
    if len(items)!=len(positions) or set(items)!={'position:'+str(p['ordinal']) for p in positions}:
        raise ValueError('position source item scope differs')
    cash=_cash_amounts(content,source_account_id=account,day=day)
    nav=_complete_nav(content,source_account_id=account,day=day,cash_amounts=cash)
    cur.execute("SELECT row_to_json(h)::text AS body,md5(to_jsonb(h)::text) AS row_hash FROM pams_holdings_snapshot h WHERE account_code=%s AND as_of=%s ORDER BY id",('ibkr',day))
    rows=[]
    for result in cur.fetchall():
        row=json.loads(result['body'],parse_float=Decimal); row['row_hash']=result['row_hash']; rows.append(row)
    for row in rows:
        amount=row.get('market_value'); liability=row.get('asset_liability')
        if liability not in {'资产','负债'} or amount is None:
            raise ValueError('holding sign or asset/liability is unknown')
        try:
            value=Decimal(str(amount))
        except (InvalidOperation,TypeError,ValueError) as exc:
            raise ValueError('holding value is invalid') from exc
        if not value.is_finite() or (liability=='资产' and value<0) or (liability=='负债' and value>0):
            raise ValueError('holding sign and asset/liability disagree')
    def is_cash(row):
        return (row['major_category'] in {'现金','借款'} and
                row.get('symbol')=='CASH_'+str(row.get('currency') or ''))
    securities=[r for r in rows if not is_cash(r)]
    cash_rows=[r for r in rows if is_cash(r)]
    if len(securities)!=len(positions) or len(cash_rows)!=len(cash):
        raise ValueError('scope does not cover current securities and cash')
    cash_current={}
    for row in cash_rows:
        currency=row['currency']
        if currency in cash_current or row['symbol']!='CASH_'+currency or row['market_value'] is None:
            raise ValueError('cash holding identity ambiguous')
        cash_current[currency]=Decimal(str(row['market_value']))
    if cash_current!=cash:
        raise ValueError('cash report and current balances differ')
    bound=[]
    for position in positions:
        uid='position:'+str(position['ordinal']); item=items.get(uid)
        if not item or item['review_status']!='confirmed' or not (item['parsed_fields'] or {}).get('source_row_review'):
            raise ValueError('security source not reviewed')
        expected={'kind':'position','source_sha256':raw['source_sha256'],'source_account_id':account,**position}
        if item['raw_fields']!=expected:
            raise ValueError('security source differs from original')
        identity=(item['parsed_fields'] or {}).get('holding_identity_review')
        if not isinstance(identity,dict) or identity.get('identity_authentication')!='unknown':
            raise ValueError('security identity review missing')
        matches=[r for r in securities if r['id']==identity.get('holding_id')]
        if len(matches)!=1:
            raise ValueError('security holding missing or duplicated')
        row=matches[0]; attrs=position['raw_attributes']; without_hash={k:v for k,v in row.items() if k!='row_hash'}
        category=attrs.get('assetCategory')
        if category in SECURITY_LABELS and row.get('minor_category')!=SECURITY_LABELS[category]:
            raise ValueError('security classification differs from original contract type')
        if (position['state']=='unknown' or row['symbol']!=position['symbol'] or row['currency']!=position['currency']
            or identity.get('holding_hash')!=holding_hash(without_hash)
            or identity.get('conid')!=str(attrs.get('conid') or '').strip()
            or identity.get('source_account_id')!=account or identity.get('source_sha256')!=raw['source_sha256']
            or identity.get('business_date')!=day.isoformat()
            or any(Decimal(str(row[k]))!=_number(attrs,names) for k,names in {
                'quantity':('position','quantity','qty'),'cost':('costBasisMoney','costBasis','cost'),
                'market_value':('marketValue','positionValue','value','markValue')}.items())):
            raise ValueError('security holding identity or value changed')
        bound.append({'holding_id':row['id'],'source_item_id':str(item['source_item_id']),'row_hash':row['row_hash']})
    if len({i['holding_id'] for i in bound})!=len(securities):
        raise ValueError('security identity ambiguous')
    bound.extend({'holding_id':r['id'],'source_item_id':str(scope['source_item_id']),'row_hash':r['row_hash']} for r in cash_rows)
    return sorted(bound,key=lambda x:x['holding_id']),{k:str(v) for k,v in sorted(cash.items())},nav


def build_attestation(cur,batch,scope,items,content,note,revision):
    raw=scope['raw_fields']; parsed=scope['parsed_fields'] or {}; account=raw.get('source_account_id')
    binding=parsed.get('account_binding_review') or {}
    if (scope['review_status']!='confirmed' or not parsed.get('scope_review') or
        binding.get('account_code')!='ibkr' or binding.get('source_account_id')!=account or
        binding.get('business_date')!=batch['date_to'].isoformat() or binding.get('source_sha256')!=raw.get('source_sha256') or
        binding.get('authentication')!='unknown' or not binding.get('note') or not note.strip()):
        raise ValueError('separate scope and account review required')
    members,cash,nav=_current_members(cur,batch,scope,items,content)
    return {'kind':'ibkr_reviewed_complete_account','account_code':'ibkr','as_of':batch['date_to'].isoformat(),
            'source_sha256':raw['source_sha256'],'cash_amounts':cash,'nav_evidence':nav,'members':members,
            'revision':revision,'note':note.strip(),'authentication':'unknown'}


def record_complete(cur,batch_id):
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(str(batch_id),)); batch=cur.fetchone()
    att=batch.get('scope_attestation') if batch else None
    if not batch or batch['status']!='published' or not isinstance(att,dict) or att.get('kind')!='ibkr_reviewed_complete_account':
        raise ValueError('published account scope required')
    if not verify_source(cur,batch,att):
        raise ValueError('published account scope evidence changed')
    from domain.investments.account_snapshot_evidence import _unresolved_prior_holdings
    if _unresolved_prior_holdings(cur,'ibkr',batch['date_to']):
        raise ValueError('previous account members remain unresolved')
    identifier=uuid4()
    members = sorted(
        [(str(identifier),m['holding_id'],m['row_hash'],m['source_item_id']) for m in att['members']],
        key=lambda member: (str(member[3]), member[1]),
    )
    execute_values(cur, """INSERT INTO account_snapshot_member
        (version_id,holding_id,holding_row_hash,source_item_id) VALUES %s""", members)
    cur.execute("""INSERT INTO account_snapshot_version(version_id,account_code,as_of,coverage_status,cash_status,
        cash_amount,source_batch_id,source_revision,source_sha256,scope_attestation,calculation_version,reason)
        VALUES (%s,'ibkr',%s,'complete','known',NULL,%s,%s,%s,%s,'ibkr-reviewed-scope-v1','explicit account scope review')""",
        (str(identifier),batch['date_to'],str(batch_id),batch['revision'],batch['published_source_hash'],Json(att)))
    return str(identifier)


def verify_source(cur,batch,att):
    try:
        if (batch['account_code']!='ibkr' or batch['business_type']!='holding' or batch['workflow']!='ibkr_holding_review'
            or batch['institution']!='ibkr' or batch['status']!='published' or batch['date_from']!=batch['date_to']
            or not _reviewed_snapshot(cur,batch,str(batch['batch_id']),locking=False,snapshot_cache={})):
            return False
        cur.execute('SELECT * FROM source_items WHERE batch_id=%s ORDER BY source_order',(str(batch['batch_id']),))
        rows=cur.fetchall(); scopes=[i for i in rows if i['source_uid']=='scope']
        if len(scopes)!=1: return False
        scope=scopes[0]; items={i['source_uid']:i for i in rows if i['source_uid']!='scope'}
        if len(items)!=len(rows)-1 or str(scope['source_item_id'])!=str(uuid5(UUID(str(batch['batch_id'])),'scope')):
            return False
        content=_read_source_report(cur,batch,scope,locking=False)
        if sha256(content).hexdigest()!=(scope['raw_fields'] or {}).get('source_sha256'):
            return False
        return build_attestation(cur,batch,scope,items,content,att['note'],batch['revision'])==att
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return False


def verified_complete(cur,version):
    att=version['scope_attestation']
    if (not isinstance(att,dict) or att.get('kind')!='ibkr_reviewed_complete_account' or
        version['account_code']!='ibkr' or version['cash_amount'] is not None or version['source_revision']!=att.get('revision')):
        return False
    cur.execute('SELECT * FROM source_batches WHERE batch_id=%s',(version['source_batch_id'],)); batch=cur.fetchone()
    if (not batch or batch['scope_attestation']!=att or batch['published_source_hash']!=version['source_sha256'] or
        batch['revision']!=version['source_revision'] or batch['date_to']!=version['as_of'] or not verify_source(cur,batch,att)):
        return False
    from domain.investments.account_snapshot_evidence import _unresolved_prior_holdings
    if _unresolved_prior_holdings(cur,'ibkr',version['as_of']): return False
    cur.execute('''SELECT holding_id,source_item_id,holding_row_hash FROM account_snapshot_member
        WHERE version_id=%s ORDER BY holding_id''',(version['version_id'],))
    members=[{'holding_id':r['holding_id'],'source_item_id':str(r['source_item_id']),'row_hash':r['holding_row_hash']} for r in cur.fetchall()]
    return members==att['members']
