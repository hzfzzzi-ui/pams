"""Read account sync evidence separately for activity and balance."""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.accounts.account_identity import account_display
from domain.accounts.bank_source_status import BANK_DUPLICATE_REVIEW_SOURCE, summaries
from domain.accounts.bank_statement_balance import statement_balances
from domain.accounts.credit_card_debt import confirmed_table_net
from domain.imports.account_import_statement import profiles as statement_profiles
from domain.imports.account_import_store import supported_import_modes
from domain.imports.source_original_read import SourceOriginals
from domain.investments.account_snapshot_evidence import _complete_member_proven
from domain.investments.portfolio_state import _excluded_accounts
from domain.investments.trade_read_v2 import load_trade_rows
from domain.ledger.cashflow_read_v2 import load_cash_rows


_LOCAL = ZoneInfo('Asia/Shanghai')


def _activity(rows, date_key):
    verified = [row for row in rows if row['source_evidence_status'] == 'verified']
    return {
        'status': 'recorded' if verified else 'review' if rows else 'uninitialized',
        'count': len(verified),
        'as_of': max((row[date_key] for row in verified), default=None),
        'synced_at': max((row['created_at'] for row in verified if row.get('created_at')), default=None),
        'has_unverified': len(rows) > len(verified),
    }


def _balance(cur, candidates, config):
    history = (config.get('manual_balance') or {}).get('as_of')
    if len(candidates) == 1:
        candidate = candidates[0]
        if candidate['source_item_id'] and _complete_member_proven(cur, candidate['id'], candidate['source_item_id']):
            return {'status': 'recorded', 'as_of': candidate['as_of'],
                    'amount': str(candidate['market_value']),
                    'synced_at': candidate['published_at'], 'historical_as_of': history}
    return {'status': 'review' if candidates else 'uninitialized', 'as_of': None,
            'synced_at': None, 'historical_as_of': history}


def _compact_summary(row, published_at, *, liability, now, source_balance=None):
    source = row['source_data'] or {}
    activity = (source, row['cashflow'], row['investment'])
    balance = row['balance']
    transaction_day = max((str(item['as_of']) for item in activity if item.get('as_of')), default=None)
    synced_at = max((stamp for stamp in (published_at, *(item.get('synced_at') for item in (*activity, balance)))
                     if stamp is not None), default=None)
    amount = balance.get('amount')
    as_of, basis = balance.get('as_of'), 'confirmed_snapshot'
    if liability and amount is not None:
        amount = str(-abs(Decimal(amount)))
    if amount is None and source_balance:
        amount, as_of, basis = (source_balance[key] for key in ('amount', 'as_of', 'basis'))
    reasons = []
    if row['active']:
        if source.get('duplicate_count', 0) > 0:
            reasons.append({'kind': 'duplicates', 'count': source['duplicate_count']})
        for kind, evidence in (('cashflow', row['cashflow']), ('investment', row['investment']), ('balance', balance)):
            if evidence.get('status') == 'review' or evidence.get('has_unverified'):
                reasons.append({'kind': kind})
        if not synced_at:
            reasons.append({'kind': 'no_sync'})
    if not row['active']:
        status = 'inactive'
    elif reasons:
        status = 'review'
    else:
        local = synced_at.astimezone(_LOCAL)
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        deadline = local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))
        status = 'stale' if now > deadline else 'normal'
    result = {'status': status, 'balance_amount': amount, 'latest_trade_on': transaction_day,
              'last_synced_at': synced_at, 'review_reasons': reasons}
    if amount is not None:
        result.update(balance_as_of=as_of, balance_basis=basis)
        if basis in {'bank_statement', 'credit_card_statement'}:
            result['balance_label'] = '账单余额'
        elif basis == 'credit_card_flow_net':
            result['balance_label'] = '流水计算欠款'
    return result


def _latest_cash_snapshots(cur, codes):
    cur.execute('''WITH latest AS (
            SELECT account_code, currency, MAX(as_of) AS as_of FROM public.pams_holdings_snapshot
            WHERE account_code=ANY(%s::text[]) AND major_category='现金'
            GROUP BY account_code, currency
        )
        SELECT h.id, h.account_code, h.as_of, h.currency, h.market_value, h.source_item_id,
            CASE WHEN r.status='published' THEN r.updated_at END AS published_at
        FROM latest JOIN public.pams_holdings_snapshot h USING (account_code, currency, as_of)
        LEFT JOIN public.source_items i ON i.source_item_id=h.source_item_id
        LEFT JOIN public.source_records r ON r.source_id=i.source_id
        WHERE h.major_category='现金' ''', (codes,))
    return cur.fetchall()


def review_records(cur, config, account_code, kind, offset):
    """List the records behind an evidence warning using the same scope as the summary."""
    account = config.get('accounts', {}).get(account_code)
    if not account or account_code in _excluded_accounts(config):
        return []
    if kind == 'balance':
        currency = account.get('base_currency', 'CNY')
        snapshots = sorted((row for row in _latest_cash_snapshots(cur, [account_code])
                            if row['currency'] == currency), key=lambda row: row['id'])
        return [{'record_id': row['id'], 'day': row['as_of'], 'currency': currency,
                 'amount': str(row['market_value']) if row['market_value'] is not None else None}
                for row in snapshots[offset:offset + 100]]
    if kind == 'cashflow':
        rows = load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY', account_codes=[account_code])
        id_key, day_key, amount_key, label_key = 'fact_id', 'transaction_date', 'amount', 'summary'
    else:
        rows = load_trade_rows(cur, account_code=account_code, start=date.min, end=date.max)
        id_key, day_key, amount_key, label_key = 'trade_id', 'trade_date', 'net_cash', 'name'
    flagged = sorted((row for row in rows if row['source_evidence_status'] != 'verified'),
                     key=lambda row: (row[day_key], row[id_key]), reverse=True)
    return [{'record_id': row[id_key], 'day': row[day_key], 'currency': row['currency'],
             'amount': str(row[amount_key]) if row.get(amount_key) is not None else None,
             'summary': row.get(label_key)} for row in flagged[offset:offset + 100]]


def read_account_sync(cur, config):
    cur.execute('SELECT account_code, active FROM public.pams_accounts')
    registered = {row['account_code']: row['active'] for row in cur.fetchall()}
    statement_accounts = statement_profiles()
    excluded = _excluded_accounts(config) | {'ibkr_in_transit_mxn'}
    accounts = {code: cfg for code, cfg in config.get('accounts', {}).items() if code not in excluded}
    active = [code for code, cfg in accounts.items() if code != 'ccb_fund_subscription_pending'
              and cfg.get('active', True) and registered.get(code, True)]
    source_status = summaries(cur, active)
    cur.execute("""SELECT account_code, MAX(updated_at) AS synced_at FROM public.source_records
        WHERE account_code=ANY(%s::text[]) AND status='published'
          AND NOT (source_name=%s AND document_type='statement')
        GROUP BY account_code""", (active, BANK_DUPLICATE_REVIEW_SOURCE))
    published_at = {row['account_code']: row['synced_at'] for row in cur.fetchall()}
    originals = SourceOriginals(cur)
    flows = defaultdict(list)
    for row in load_cash_rows(cur, start=date.min, end=date.max, base_currency='CNY',
                              account_codes=active, originals=originals):
        flows[row['account_code']].append(row)

    cur.execute('SELECT DISTINCT account_code FROM public.pams_investment_trade_fact WHERE account_code=ANY(%s::text[])',
                (active,))
    trade_accounts = [row['account_code'] for row in cur.fetchall()]
    trades = {code: load_trade_rows(cur, account_code=code, start=date.min, end=date.max, originals=originals)
              for code in trade_accounts}
    balances = defaultdict(list)
    for row in _latest_cash_snapshots(cur, active):
        code = row['account_code']
        if row['currency'] == accounts[code].get('base_currency', 'CNY'):
            balances[code].append(row)

    roots = config.get('account_roots', {})
    groups = {}
    now = datetime.now(_LOCAL)
    statement_accounts = {}
    for code in active:
        cfg = accounts[code]
        liability = cfg.get('account_type') == 'liability'
        if (liability and code in source_status) or (
            cfg.get('account_type') == 'funding' and cfg.get('truth_source') in {'bank_statement', 'official_statement'}
        ):
            statement_accounts[code] = (cfg.get('base_currency', 'CNY'), liability)
    source_balances = statement_balances(cur, statement_accounts, now.date(), originals=originals)
    if 'ccb_credit_0453' in statement_accounts and 'ccb_credit_0453' not in source_balances:
        # The user approved table net for this card before an archived balance is available.
        source_balances['ccb_credit_0453'] = confirmed_table_net(
            cur, 'ccb_credit_0453', currency=statement_accounts['ccb_credit_0453'][0], today=now.date(),
            has_duplicate_alerts=bool(source_status['ccb_credit_0453']['duplicate_count']),
        )
    for code, cfg in accounts.items():
        parent, group_label, label = account_display(config, code, code)
        group = groups.setdefault(parent, {'code': parent, 'label': group_label or parent, 'rows': []})
        row = {
            'account_code': code, 'label': label,
            'currency': cfg.get('base_currency', 'CNY'), 'active': code in active,
            'can_import': bool(code in active and registered.get(code) and supported_import_modes(code, statement_accounts)),
            'show_investment': cfg.get('account_type') in {'brokerage', 'fund_platform', 'cash_management'} or bool(trades.get(code)),
            'cashflow': _activity(flows[code], 'transaction_date'),
            'source_data': source_status.get(code),
            'investment': _activity(trades.get(code, ()), 'trade_date'),
            'balance': _balance(cur, balances[code], cfg),
        }
        row['summary'] = _compact_summary(row, published_at.get(code), liability=cfg.get('account_type') == 'liability',
                                          now=now, source_balance=source_balances.get(code))
        group['rows'].append(row)
    return {'groups': sorted(groups.values(), key=lambda group: (
        40 if group['code'] == 'tencent' else roots.get(group['code'], {}).get('sort_order', 999), group['code']))}
