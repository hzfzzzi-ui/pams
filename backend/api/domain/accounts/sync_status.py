"""Read account sync evidence separately for activity and balance."""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from domain.accounts.bank_source_status import summaries
from domain.imports.source_original_read import SourceOriginals
from domain.investments.account_snapshot_evidence import _complete_member_proven
from domain.investments.portfolio_state import _excluded_accounts
from domain.investments.trade_read_v2 import load_trade_rows
from domain.ledger.cashflow_read_v2 import load_cash_rows


_GROUPS = {
    'santander': ('santander', 'Santander 4209 储蓄卡'),
    'alipay': ('alipay', '支付宝余额'),
    'ibkr': ('ibkr', 'IBKR 证券账户'),
    'tonghuashun': ('tonghuashun', '同花顺 A 股证券账户'),
    'wechat_balance': ('tencent', None),
    'wechat_lqt': ('tencent', None),
    'tencent_licaitong': ('tencent', None),
    'huawei_stock': ('ccb', None),
}
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


def _compact_summary(row, published_at, *, liability, now):
    source = row['source_data'] or {}
    activity = (source, row['cashflow'], row['investment'])
    balance = row['balance']
    transaction_day = max((str(item['as_of']) for item in activity if item.get('as_of')), default=None)
    synced_at = max((stamp for stamp in (published_at, *(item.get('synced_at') for item in (*activity, balance)))
                     if stamp is not None), default=None)
    amount = balance.get('amount')
    if liability and amount is not None:
        amount = str(-abs(Decimal(amount)))
    if not row['active']:
        status = 'inactive'
    elif (not synced_at or source.get('duplicate_count', 0) > 0 or
          any(item.get('status') == 'review' or item.get('has_unverified') for item in (*activity, balance))):
        status = 'review'
    else:
        local = synced_at.astimezone(_LOCAL)
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        deadline = local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))
        status = 'stale' if now > deadline else 'normal'
    return {'status': status, 'balance_amount': amount, 'latest_trade_on': transaction_day, 'last_synced_at': synced_at}


def read_account_sync(cur, config):
    cur.execute('SELECT account_code, active FROM public.pams_accounts')
    registered = {row['account_code']: row['active'] for row in cur.fetchall()}
    excluded = _excluded_accounts(config) | {'ibkr_in_transit_mxn'}
    accounts = {code: cfg for code, cfg in config.get('accounts', {}).items() if code not in excluded}
    active = [code for code, cfg in accounts.items() if code != 'ccb_fund_subscription_pending'
              and cfg.get('active', True) and registered.get(code, True)]
    source_status = summaries(cur, active)
    cur.execute("""SELECT account_code, MAX(updated_at) AS synced_at FROM public.source_records
        WHERE account_code=ANY(%s::text[]) AND status='published' GROUP BY account_code""", (active,))
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
        WHERE h.major_category='现金' ''', (active,))
    balances = defaultdict(list)
    for row in cur.fetchall():
        code = row['account_code']
        if row['currency'] == accounts[code].get('base_currency', 'CNY'):
            balances[code].append(row)

    roots = config.get('account_roots', {})
    groups = {}
    now = datetime.now(_LOCAL)
    for code, cfg in accounts.items():
        parent, label = _GROUPS.get(code, (cfg.get('parent_account_code') or code, None))
        root = roots.get(parent, {})
        group = groups.setdefault(parent, {'code': parent, 'label': '腾讯' if parent == 'tencent'
            else root.get('display_name', parent), 'rows': []})
        row = {
            'account_code': code, 'label': label or cfg.get('display_name', code),
            'currency': cfg.get('base_currency', 'CNY'), 'active': code in active,
            'can_import': bool(code in active and registered.get(code) and code != 'tencent_licaitong'),
            'show_investment': cfg.get('account_type') in {'brokerage', 'fund_platform', 'cash_management'} or bool(trades.get(code)),
            'cashflow': _activity(flows[code], 'transaction_date'),
            'source_data': source_status.get(code),
            'investment': _activity(trades.get(code, ()), 'trade_date'),
            'balance': _balance(cur, balances[code], cfg),
        }
        row['summary'] = _compact_summary(row, published_at.get(code), liability=cfg.get('account_type') == 'liability', now=now)
        group['rows'].append(row)
    return {'groups': sorted(groups.values(), key=lambda group: (
        40 if group['code'] == 'tencent' else roots.get(group['code'], {}).get('sort_order', 999), group['code']))}
