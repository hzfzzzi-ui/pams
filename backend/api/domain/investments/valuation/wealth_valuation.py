"""Dated wealth position observations from reviewed trade and NAV evidence."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from domain.imports.sources.source_original_read import SourceOriginals
from domain.imports.publication.trade_supplemental_evidence import read as read_trade_evidence
from domain.investments.returns.fund_closed_sessions import surrounding_sessions
from domain.investments.holdings.position_evidence_replay import replay_reviewed_cycle
from domain.investments.trades.trade_read_v2 import load_trade_rows


def load_reviewed_wealth_anchors(cur, *, accounts, dates):
    """Estimate held units at reviewed marks, retaining their actual disclosure date."""
    if not accounts or not dates:
        return []
    cur.execute("""SELECT DISTINCT f.account_code FROM pams_investment_trade_fact f
        JOIN pams_asset_master a ON a.asset_id=f.asset_id
        WHERE f.account_code=ANY(%s::text[]) AND a.asset_type='wealth_management'
          AND f.trade_date<=%s""", (sorted(accounts), max(dates)))
    wealth_accounts = [row['account_code'] for row in cur.fetchall()]
    originals = SourceOriginals(cur)
    groups = defaultdict(list)
    for account in wealth_accounts:
        for row in load_trade_rows(cur, account_code=account, start=date.min, end=date.max,
                                   originals=originals, include_instrument_identity=True):
            if row.get('asset_type') == 'wealth_management' and row.get('asset_id'):
                groups[(account, row['asset_id'], row['currency'])].append(row)
    results = []
    for (account, asset_id, currency), trades in groups.items():
        evidence = {row['trade_id']: read_trade_evidence(cur, trade_id=row['trade_id']) for row in trades}
        replay = replay_reviewed_cycle(trades, evidence)
        if not replay:
            continue
        buy = next(row for row in trades if row['trade_type'] in {'buy', 'fund_subscription'})
        for day in dates:
            if not buy['trade_date'] <= day < replay['exit_date']:
                continue
            price_date = day
            if day not in replay['prices']:
                sessions = surrounding_sessions(day) if currency == 'CNY' else None
                if not sessions or not all(mark in replay['prices'] for mark in sessions):
                    continue
                # Some products disclose over holidays. Use their latest actual
                # mark within the bounded gap between observed trading sessions.
                price_date = max(mark for mark in replay['prices'] if sessions[0] <= mark <= day)
            amount = (replay['buy_quantity'] * replay['prices'][price_date]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            results.append({'date':day, 'price_date':price_date, 'account_code':account, 'asset_id':asset_id,
                            'symbol':buy.get('symbol'), 'currency':currency, 'amount':amount,
                            'quantity':replay['buy_quantity'], 'price':replay['prices'][price_date],
                            'direct_unit_mark':any(entry.get('status') == 'verified'
                                and entry['fields'].get('valuation_date') == price_date.isoformat()
                                and entry['fields'].get('valuation_unit_price') is not None
                                for entry in evidence[buy['trade_id']]),
                            'source_refs':sorted(item['confirmation_id'] for items in evidence.values()
                                                for item in items if item.get('status') == 'verified')})
    return results
