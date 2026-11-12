"""Normalize a single business day's Flex position summary for the common publisher."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import os
import re
from uuid import uuid4

from domain.brokers.ibkr.ibkr_daily_close import _document, _day, _rows, _number
from domain.brokers.ibkr.ibkr_instrument_types import SECURITY_COMPONENTS, SECURITY_LABELS
from domain.imports import source_records_v2
from domain.imports.holding_publication_v2 import publish_validated_holdings
from domain.investments.holding_evidence_writer import lock_holding_accounts

ACCOUNT = 'ibkr'
SOURCE = 'ibkr_daily_holdings'
ALIAS_TYPE = 'ibkr_conid'
CONID = re.compile(r'[1-9][0-9]{0,17}')
MARKET_LABELS = {
    'NASDAQ': '美股', 'NYSE': '美股', 'ARCA': '美股', 'AMEX': '美股',
    'SEHK': '港股', 'KRX': '韩股', 'KSE': '韩股', 'SFB': '瑞典',
}


class ReportValidationError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def credentials():
    from domain.imports import isolation, native_source

    if isolation.source_mode() == 'development' or not native_source.enabled():
        return None
    token = os.environ.get('IBKR_FLEX_TOKEN', '').strip()
    query = os.environ.get('PAMS_IBKR_HOLDINGS_QUERY_ID', '').strip()
    account = os.environ.get('PAMS_IBKR_HOLDINGS_ACCOUNT_ID', '').strip()
    secret = os.environ.get('PAMS_IBKR_HOLDINGS_SYNC_TOKEN', '').strip()
    if token and re.fullmatch(r'[0-9]+', query) and account and len(secret) >= 32:
        return token, query, account, secret
    return None


def read_report(content: bytes, source_account: str, day: date):
    try:
        return _read_report(content, source_account, day)
    except ReportValidationError:
        raise
    except (ValueError, ArithmeticError) as exc:
        raise ReportValidationError('position_report_unverified') from exc


def _read_report(content: bytes, source_account: str, day: date):
    evidence, digest, error = _document(content.decode('utf8'), source_account, None)
    if error:
        raise ValueError(error)
    rows, complete = _read_day(evidence, day)
    return digest, rows, complete


def _read_day(evidence, day):
    statements = evidence.by_day.get(day, [])
    if not statements and max(evidence.by_day) < day:
        raise ReportValidationError('stale_statement_date')
    if len(statements) != 1 or _day(statements[0].get('fromDate')) != day:
        raise ValueError('position_statement_scope_unverified')
    statement = statements[0]
    base = evidence.base(statement)
    sections = _rows(statement, 'OpenPositions')
    if len(sections) != 1 or len(_rows(statement, 'OpenPosition')) != len(_rows(sections[0], 'OpenPosition')):
        raise ValueError('position_section_unverified')
    evidence.scope(sections[0], statement, base, day, day, leaf_account=False)
    rows = {}
    for node in _rows(sections[0], 'OpenPosition'):
        evidence.scope(node, statement, base, day, day, currency=False)
        fields = dict(node.attrib)
        if (_day(fields.get('reportDate')) != day or fields.get('levelOfDetail') != 'SUMMARY'
                or not CONID.fullmatch(fields.get('conid', ''))
                or not re.fullmatch(r'[A-Z]{3}', fields.get('currency', ''))
                or fields.get('assetCategory') not in SECURITY_COMPONENTS):
            raise ValueError('position_identity_unverified')
        conid, symbol = fields['conid'], fields.get('symbol', '').strip()
        description = fields.get('description', '').strip()
        name = description if description.upper() == symbol.upper() else f'{symbol} - {description}'
        if (not symbol or not description or len(symbol) > 100 or len(name) > 180
                or conid in rows):
            raise ValueError('position_identity_ambiguous')
        quantity, price, cost, value, buy_price = (
            _number(node, key) for key in ('position', 'markPrice', 'costBasisMoney', 'positionValue', 'costBasisPrice')
        )
        if price < 0 or (quantity == 0 and (cost != 0 or value != 0)) or quantity * value < 0:
            raise ValueError('position_amount_unverified')
        kind = fields['assetCategory']
        major = MARKET_LABELS.get(fields.get('listingExchange'), '其他') if kind == 'STK' else '其他'
        minor = SECURITY_LABELS.get(kind, major)
        rows[conid] = (fields, dict(
            symbol=symbol, name=name, currency=fields['currency'], asset_id=None,
            asset_liability='负债' if quantity < 0 else '资产', major_category=major, minor_category=minor,
            quantity=quantity, current_price=price, cost=cost, market_value=value, buy_price=buy_price,
        ))
    # An empty section alone cannot establish liquidation. Full broker NAV evidence
    # is required before allowing inferred exits, including an all-flat account.
    try:
        nav = evidence.nav(statement, day, base)
        evidence.holdings(statement, day, base, nav)
        checks = nav['component_proof']['subcomponent_checks']
        totals = {key: [Decimal(0), Decimal(0)] for key in SECURITY_COMPONENTS.values()}
        for fields, row in rows.values():
            rate = Decimal(fields.get('fxRateToBase') or ('1' if row['currency'] == base else 'NaN'))
            if not rate.is_finite() or rate <= 0 or (row['currency'] == base and rate != 1):
                raise ValueError('position_scope_fx_unverified')
            value = row['market_value'] * rate
            totals[SECURITY_COMPONENTS[fields['assetCategory']]][0 if value >= 0 else 1] += value
        complete = all(
            key + '_long_short' in checks and
            abs(checks[key + '_long_short']['long'] - amounts[0]) <= Decimal('0.02') and
            abs(checks[key + '_long_short']['short'] - amounts[1]) <= Decimal('0.02')
            for key, amounts in totals.items()
        ) and all(nav['component_proof']['components'][key] == 0 for key in ('notes', 'crypto'))
    except (ValueError, ArithmeticError):
        complete = False
    if not rows and not complete:
        raise ValueError('empty_positions_unverified')
    return rows, complete


@lru_cache(maxsize=2)
def historical_us_position_values(content: bytes, source_account: str, digest: str):
    """Read daily US holdings only when the broker's dated security totals reconcile."""
    try:
        evidence, _, error = _document(content.decode('utf8'), source_account, digest)
    except UnicodeError:
        return {}
    if error:
        return {}
    values = {}
    for day in sorted(evidence.by_day):
        try:
            rows, complete = _read_day(evidence, day)
        except (ValueError, ArithmeticError):
            continue
        if not complete:
            continue
        positions = [row for _, row in rows.values() if row['major_category'] == '美股' and row['quantity']]
        if not positions or any(row['currency'] != 'USD' or row['quantity'] < 0 or row['cost'] <= 0
                                for row in positions):
            continue
        values[day] = (sum(row['market_value'] for row in positions), sum(row['cost'] for row in positions))
    return values


def last_publication(cur, *, through=None):
    cutoff = " AND r.period_end <= %s" if through is not None else ""
    params = (ACCOUNT, SOURCE, through) if through is not None else (ACCOUNT, SOURCE)
    cur.execute("""SELECT r.source_id, r.period_end, r.sha256, r.updated_at FROM public.source_records r
        JOIN public.ibkr_source_originals o ON o.sha256=r.sha256
        WHERE r.account_code=%s AND r.source_name=%s AND r.document_type='holding'
          AND r.status='published' AND r.acquisition_method='api' AND r.period_start=r.period_end
        """ + cutoff + " ORDER BY r.period_end DESC LIMIT 1", params)
    return cur.fetchone()


def publication_status(cur):
    from domain.brokers.ibkr.ibkr_history_backfill import daily_facts_through
    from domain.brokers.ibkr.ibkr_report_state import expected_statement_date

    target = expected_statement_date()
    cur.execute("SELECT to_regclass('public.ibkr_source_originals') IS NOT NULL AS ready")
    ready = cur.fetchone()['ready']
    latest = last_publication(cur) if ready else None
    facts_through = daily_facts_through(cur) if ready else None
    return {
        'configured': bool(ready and credentials()),
        'expected_statement_as_of': target.isoformat(),
        'statement_as_of': latest['period_end'].isoformat() if latest else None,
        'publication_verified': bool(latest and latest['period_end'] == target),
        'facts_as_of': facts_through.isoformat() if facts_through else None,
        'facts_verified_for_target': bool(facts_through and facts_through >= target),
    }


def _bind_positions(cur, parsed, previous, complete):
    # Prefer an explicit contract alias. Existing name-based snapshot readers
    # require the same stored name across dates for a single contract.
    cur.execute("""SELECT a.alias_value AS conid, a.asset_id, m.currency, m.status
        FROM public.pams_asset_alias a JOIN public.pams_asset_master m USING (asset_id)
        WHERE a.alias_type=%s FOR SHARE OF a, m""", (ALIAS_TYPE,))
    aliases, contracts = {}, {}
    for alias in cur.fetchall():
        if CONID.fullmatch(alias['conid']):
            aliases.setdefault(alias['conid'], []).append(alias)
            contracts.setdefault(alias['asset_id'], []).append(alias['conid'])

    prior_by_contract = {}
    reserved_names = {row['name'] for row in previous}
    for old in previous:
        conid = old['conid']
        if not conid or not CONID.fullmatch(conid):
            candidates = contracts.get(old['asset_id'], [])
            conid = candidates[0] if len(candidates) == 1 else None
        if not conid:
            if old['quantity'] or old['market_value']:
                raise ReportValidationError('position_legacy_identity_unverified')
            continue
        if conid in prior_by_contract:
            raise ReportValidationError('position_contract_mapping_conflict')
        prior_by_contract[conid] = old

    missing = {key: old for key, old in prior_by_contract.items()
               if key not in parsed and (old['quantity'] or old['market_value'])}
    if missing and not complete:
        raise ReportValidationError('position_exit_scope_unverified')
    for conid, old in missing.items():
        row = {key: value for key, value in old.items() if key not in ('as_of', 'conid')}
        row.update(quantity=Decimal(0), market_value=Decimal(0), cost=Decimal(0),
                   current_price=Decimal(0), buy_price=Decimal(0))
        parsed[conid] = ({'position_absent_from_verified_scope': True, 'conid': conid}, row)

    for conid, (_, row) in sorted(parsed.items()):
        old = prior_by_contract.get(conid)
        candidates = aliases.get(conid, [])
        if (len(candidates) > 1 or (candidates and (
                candidates[0]['currency'] != row['currency'] or candidates[0]['status'] != 'active'))
                or (old and old['currency'] != row['currency'])):
            raise ReportValidationError('position_contract_mapping_conflict')
        asset_id = candidates[0]['asset_id'] if candidates else None
        if old and old['asset_id'] and old['asset_id'] != asset_id:
            raise ReportValidationError('position_contract_mapping_conflict')
        row['asset_id'] = asset_id
        if old:
            row['name'] = old['name']
        elif row['name'] in reserved_names:
            row['name'] = f"{row['name'][:175]} [{conid}]"
            if row['name'] in reserved_names:
                raise ReportValidationError('position_contract_mapping_conflict')
        reserved_names.add(row['name'])


def publish_report(cur, *, content, source_account, day):
    if cur.connection.autocommit:
        raise ValueError('holding_source_requires_transaction')
    digest, parsed, complete = read_report(content, source_account, day)
    lock_holding_accounts(cur, [ACCOUNT])
    latest = last_publication(cur)
    if latest and latest['period_end'] >= day:
        if latest['period_end'] == day and latest['sha256'] == digest:
            return {'status': 'published', 'as_of': day.isoformat(), 'replayed': True}
        raise ReportValidationError('position_report_conflict')
    cur.execute("SELECT active FROM public.pams_accounts WHERE account_code=%s", (ACCOUNT,))
    account = cur.fetchone()
    if not account or not account['active']:
        raise ReportValidationError('position_account_inactive')
    cur.execute("""SELECT DISTINCT ON (h.name) h.name, h.symbol, h.currency, h.asset_id, h.asset_liability,
        h.major_category, h.minor_category, h.quantity, h.market_value, h.as_of,
        CASE WHEN r.source_name=%s AND r.status='published' AND i.review_status='confirmed'
             THEN i.source_uid END AS conid
        FROM public.pams_holdings_snapshot h
        LEFT JOIN public.source_items i ON i.source_item_id=h.source_item_id
        LEFT JOIN public.source_records r ON r.source_id=i.source_id AND r.account_code=h.account_code
        WHERE h.account_code=%s AND h.major_category <> '现金' ORDER BY h.name, h.as_of DESC""", (SOURCE, ACCOUNT))
    previous = cur.fetchall()
    if any(row['as_of'] >= day for row in previous):
        raise ReportValidationError('position_report_conflict')
    reported_positions = len(parsed)
    _bind_positions(cur, parsed, previous, complete)

    source_id = uuid4()
    source_records_v2.create_source(cur, source_id=source_id, account_code=ACCOUNT, source_name=SOURCE,
                                    acquisition_method='api', document_type='holding', filename='ibkr-positions.xml',
                                    file_format='xml', content_type='application/xml', content=content)
    cur.execute("UPDATE public.source_records SET period_start=%s,period_end=%s WHERE source_id=%s",
                (day, day, str(source_id)))
    holdings = []
    for uid, (raw, row) in parsed.items():
        for field in ('quantity', 'current_price', 'buy_price', 'cost', 'market_value'):
            scale = 4 if field in ('cost', 'market_value') else 6
            quantum = Decimal(1).scaleb(-scale)
            row[field] = row[field].quantize(quantum, rounding=ROUND_HALF_UP)
            if abs(row[field]) >= Decimal(10) ** (20 - scale):
                raise ReportValidationError('position_report_unverified')
        item_id = uuid4()
        fields = {**row, 'business_date': day.isoformat(), 'price': row['current_price'],
                  'cost_basis_type': 'holding_cost'}
        fields = {key: str(value) if isinstance(value, Decimal) else value for key, value in fields.items()}
        source_records_v2.add_item(cur, source_id=source_id, item_id=item_id, parse_method='api',
                                   raw_fields=raw, parsed_fields=fields, source_uid=uid)
        holdings.append(dict(row, source_item_id=str(item_id), account_code=ACCOUNT, as_of=day))
    cur.execute("""UPDATE public.source_items SET review_status='confirmed',review_note='Validated broker position report'
        WHERE source_id=%s""", (str(source_id),))
    result = publish_validated_holdings(
        cur, source_id=source_id, as_of=day, holdings=holdings,
        partial_attestation={'securities_coverage': 'verified' if complete else 'observed',
                             'method': 'ibkr-flex-securities-v1', 'reported_positions': reported_positions},
    )
    from domain.brokers.ibkr.ibkr_history_backfill import publish_daily_facts

    facts = publish_daily_facts(cur, content=content, account=source_account, digest=digest, as_of=day)
    return {**result, 'status': 'published', 'as_of': day.isoformat(), 'replayed': False, 'facts': facts}
