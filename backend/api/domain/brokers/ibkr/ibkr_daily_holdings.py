"""Normalize a single business day's Flex position summary for the common publisher."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
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
    names = set()
    for node in _rows(sections[0], 'OpenPosition'):
        evidence.scope(node, statement, base, day, day, currency=False)
        fields = dict(node.attrib)
        if (_day(fields.get('reportDate')) != day or fields.get('levelOfDetail') != 'SUMMARY'
                or not re.fullmatch(r'[1-9][0-9]{0,17}', fields.get('conid', ''))
                or not re.fullmatch(r'[A-Z]{3}', fields.get('currency', ''))
                or fields.get('assetCategory') not in SECURITY_COMPONENTS):
            raise ValueError('position_identity_unverified')
        conid, symbol = fields['conid'], fields.get('symbol', '').strip()
        description = fields.get('description', '').strip()
        name = description if description.upper() == symbol.upper() else f'{symbol} - {description}'
        if (not symbol or not description or len(symbol) > 100 or len(name) > 180
                or conid in rows or name in names):
            raise ValueError('position_identity_ambiguous')
        names.add(name)
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
    return digest, rows, complete


def last_publication(cur):
    cur.execute("""SELECT source_id, period_end, sha256 FROM public.source_records
        WHERE account_code=%s AND source_name=%s AND document_type='holding' AND status='published'
          AND acquisition_method='api' AND period_start=period_end
          AND scope_note IN ('securities_verified','securities_observed')
        ORDER BY period_end DESC LIMIT 1""", (ACCOUNT, SOURCE))
    return cur.fetchone()


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
    cur.execute("""SELECT DISTINCT ON (name) name, symbol, currency, asset_id, asset_liability,
        major_category, minor_category, quantity, market_value, as_of
        FROM public.pams_holdings_snapshot
        WHERE account_code=%s AND major_category <> '现金' ORDER BY name, as_of DESC""", (ACCOUNT,))
    previous = cur.fetchall()
    if any(row['as_of'] >= day for row in previous):
        raise ReportValidationError('position_report_conflict')
    names = {row['name'] for _, row in parsed.values()}
    missing = [row for row in previous if row['name'] not in names and (row['quantity'] or row['market_value'])]
    if missing and not complete:
        raise ReportValidationError('position_exit_scope_unverified')
    # Preserve history and record a sourced zero on the new date so the normal
    # latest-known holding reader does not carry a verified exit forward.
    for index, old in enumerate(missing, 1):
        row = {key: value for key, value in old.items() if key != 'as_of'}
        row.update(asset_id=None, quantity=Decimal(0), market_value=Decimal(0), cost=Decimal(0),
                   current_price=Decimal(0), buy_price=Decimal(0))
        parsed[f'exit:{index}'] = ({'position_absent_from_verified_scope': True, 'name': old['name']}, row)

    source_id = uuid4()
    source_records_v2.create_source(cur, source_id=source_id, account_code=ACCOUNT, source_name=SOURCE,
                                    acquisition_method='api', document_type='holding', filename='ibkr-positions.xml',
                                    file_format='xml', content_type='application/xml', content=content)
    cur.execute("""UPDATE public.source_records SET period_start=%s,period_end=%s,scope_note=%s WHERE source_id=%s""",
                (day, day, 'securities_verified' if complete else 'securities_observed', str(source_id)))
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
    result = publish_validated_holdings(cur, source_id=source_id, as_of=day, holdings=holdings)
    return {**result, 'status': 'published', 'as_of': day.isoformat(), 'replayed': False}
