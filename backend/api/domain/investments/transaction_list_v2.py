"""Investment transaction list backed by verified redesigned source records."""
from datetime import date, datetime, time
from decimal import Decimal

from domain.investments.trade_cost_evidence import known_trade_charges
from domain.investments.trade_read_v2 import load_trade_rows


def list_rows(cur, *, start=None, end=None, account_code=None):
    cur.execute('SELECT DISTINCT account_code FROM public.pams_investment_trade_fact WHERE (%s IS NULL OR account_code=%s)',
                (account_code, account_code))
    accounts = [row['account_code'] for row in cur.fetchall()]
    facts = [fact for account in accounts for fact in load_trade_rows(
        cur, account_code=account, start=start or date.min, end=end or date.max)]
    if not facts:
        return []
    cur.execute('SELECT * FROM public.investment_trade_review WHERE trade_id=ANY(%s)',
                ([fact['trade_id'] for fact in facts],))
    reviews = {row['trade_id']: dict(row) for row in cur.fetchall()}
    cur.execute('SELECT account_code,display_name FROM public.pams_accounts')
    names = {row['account_code']: row['display_name'] for row in cur.fetchall()}
    asset_ids = [fact['asset_id'] for fact in facts if fact.get('asset_id')]
    cur.execute('SELECT asset_id,asset_type,asset_subtype FROM public.pams_asset_master WHERE asset_id=ANY(%s)', (asset_ids,))
    assets = {row['asset_id']: row for row in cur.fetchall()}
    rates = {}
    out = []
    for fact in facts:
        currency, day = fact['currency'], fact['trade_date']
        key = (currency, day)
        if key not in rates:
            if currency == 'CNY':
                rates[key] = Decimal(1)
            else:
                cur.execute("""SELECT rate FROM public.pams_fx_rates_daily WHERE base_currency='CNY'
                    AND quote_currency=%s AND as_of=%s AND rate>0
                    AND rate::text NOT IN ('NaN','Infinity','-Infinity') AND NULLIF(btrim(source),'') IS NOT NULL LIMIT 1""", key)
                match = cur.fetchone()
                rates[key] = match['rate'] if match else None
        rate = rates[key]
        verified = fact['source_evidence_status'] == 'verified'
        review = reviews.get(fact['trade_id'], {})
        kind = fact['trade_type']
        side = {'fund_subscription': 'buy', 'fund_redemption': 'sell', 'interest': 'dividend', 'coupon': 'dividend'}.get(kind, fact.get('side') or kind)
        gross = abs(fact['gross_amount']) if verified and fact['gross_amount'] is not None else None
        net = fact['net_cash'] if verified else None
        fees = known_trade_charges(fact) if verified else None
        def converted(value):
            if value == 0:
                return Decimal(0)
            return value * rate if value is not None and rate is not None else None
        asset = assets.get(fact.get('asset_id')) or {}
        row = {**fact, **{k: v for k, v in review.items() if k not in {'trade_id', 'created_at', 'updated_at'}},
            'row_id': 'fact-' + str(fact['trade_id']), 'native_id': fact['trade_id'],
            'source': fact.get('broker') or 'source_records', 'source_key': str(fact['source_item_id']),
            'account_name': names.get(fact['account_code']) or fact['account_code'],
            'trade_time': fact.get('trade_time') or datetime.combine(day, time.min),
            'major_category': asset.get('asset_type'), 'minor_category': asset.get('asset_subtype'),
            'side': fact.get('side') or kind, 'side_normalized': side,
            'status': review.get('review_status') or 'auto',
            'review_tags': review.get('review_tags') or [],
            'quantity': fact['quantity'] if verified else None, 'price': fact['price'] if verified else None,
            'gross_amount': gross, 'net_cash': net, 'tax_amount': fact['tax_amount'] if verified else None,
            'fees': fees, 'fx_rate': rate, 'gross_amount_cny': converted(gross),
            'net_cash_cny': converted(net), 'fees_cny': converted(fees)}
        out.append(row)
    return out


def list_options(cur):
    """Build filters using asset master and separate trade review records."""
    cur.execute("""SELECT major_category, minor_category FROM (
        SELECT major_category, minor_category FROM pams_holdings_snapshot
        UNION SELECT asset_type AS major_category, asset_subtype AS minor_category FROM pams_asset_master
    ) categories WHERE COALESCE(major_category, '') <> ''
    ORDER BY major_category, minor_category NULLS LAST""")
    categories = [dict(row) for row in cur.fetchall()]
    cur.execute("""SELECT DISTINCT COALESCE(NULLIF(broker,''),'source_records') AS source
        FROM pams_investment_trade_fact ORDER BY source""")
    sources = [row['source'] for row in cur.fetchall()]
    cur.execute("""WITH reviewable AS (
        SELECT LOWER(COALESCE(NULLIF(f.symbol,''),NULLIF(f.name,''),'')) AS asset_key,
            NULLIF(f.symbol,'') AS symbol, NULLIF(f.name,'') AS name, f.trade_type,
            r.buy_reason,r.sell_reason,r.initial_position_identity,r.current_position_identity
        FROM pams_investment_trade_fact f LEFT JOIN investment_trade_review r ON r.trade_id=f.trade_id
        WHERE f.trade_type IN ('buy','sell','fund_subscription','fund_redemption')
          AND COALESCE(NULLIF(f.symbol,''),NULLIF(f.name,'')) IS NOT NULL
    ) SELECT asset_key, MAX(symbol) AS symbol, MAX(name) AS name, COUNT(*) AS trade_count,
        COUNT(*) FILTER (WHERE (trade_type IN ('buy','fund_subscription') AND
          (buy_reason IS NULL OR initial_position_identity IS NULL OR current_position_identity IS NULL))
          OR (trade_type IN ('sell','fund_redemption') AND sell_reason IS NULL)) AS pending_count
        FROM reviewable GROUP BY asset_key
        ORDER BY pending_count DESC, trade_count DESC, COALESCE(MAX(name),MAX(symbol),asset_key) LIMIT 500""")
    return categories, sources, [dict(row) for row in cur.fetchall()]
