"""Dated wealth position observations from reviewed trade and NAV evidence."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from domain.imports.source_original_read import SourceOriginals
from domain.imports.trade_supplemental_evidence import read as read_trade_evidence
from domain.investments.position_evidence_replay import replay_reviewed_cycle
from domain.investments.trade_read_v2 import load_trade_rows


def load_reviewed_wealth_anchors(cur, *, accounts, dates):
    """Return exact dated unit-NAV anchors; do not assert account completeness."""
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
        anchors = defaultdict(set)
        for item in evidence[buy['trade_id']]:
            fields = item.get('fields') or {}
            if item.get('status') != 'verified' or not fields.get('valuation_date') or not fields.get('valuation_unit_price'):
                continue
            try:
                day = date.fromisoformat(fields['valuation_date'])
                price = Decimal(str(fields['valuation_unit_price']))
            except (TypeError, ValueError, InvalidOperation):
                continue
            if price.is_finite() and price > 0:
                anchors[day].add(price)
        for day in dates:
            values = anchors.get(day, set())
            if (len(values) != 1 or not buy['trade_date'] <= day < replay['exit_date']
                    or replay['prices'].get(day) not in values):
                continue
            amount = (replay['buy_quantity'] * next(iter(values))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            results.append({'date':day, 'account_code':account, 'asset_id':asset_id,
                            'symbol':buy.get('symbol'), 'currency':currency, 'amount':amount})
    return results
