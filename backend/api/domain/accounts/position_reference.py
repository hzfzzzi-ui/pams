"""Read dated position references without publishing a holding or changing valuation."""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from domain.accounts.balance_reference import historical_balance
from domain.investments.snapshot_coverage import point_in_time_visible_sql


REFERENCE_CATEGORIES = {'fund': 'A股基金', 'precious_metal': '贵金属'}


def position_reference_config(cfg):
    return (cfg.get('account_type') == 'fund_platform'
            and cfg.get('truth_source') == 'manual'
            and cfg.get('reconcile_method') == 'position_nav'
            and cfg.get('base_currency', 'CNY') == 'CNY')


def historical_position_reference(cfg, today, owner_ratio):
    return historical_balance(cfg.get('manual_balance') or {}, today, owner_ratio,
                              base_currency=cfg.get('base_currency'), basis='manual_position_reference')


def _reference_row(row, cfg, day, owner_ratio):
    """Bind the display amount to the existing account reference and the named asset."""
    reference = historical_position_reference(cfg, day, owner_ratio)
    if reference is None:
        return None
    raw, fields = row['raw_fields'], row['parsed_fields']
    as_of = reference['balance_as_of'].isoformat()
    asset_type = row['asset_type']
    category = REFERENCE_CATEGORIES.get(asset_type)
    if not category or raw.get('snapshot_date') != as_of or fields.get('business_date') != as_of:
        return None
    digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != row['record_hash'] or raw.get('account_code') != row['account_code']:
        return None
    if raw.get('currency') != 'CNY' or fields.get('currency') != 'CNY' or fields.get('symbol') != row['symbol']:
        return None
    try:
        configured = Decimal(str(cfg['manual_balance']['amount']))
        if Decimal(str(raw.get('known_historical_amount'))) != configured or Decimal(str(fields.get('market_value'))) != configured:
            return None
    except InvalidOperation:
        return None
    return {
        'account_code': row['account_code'],
        'account_name': cfg.get('display_name') or row['account_code'],
        'symbol': row['symbol'],
        'name': row['name'],
        'category': category,
        'currency': reference['balance_currency'],
        'value': reference['balance_amount'],
        'as_of': as_of,
    }


def read_historical_position_references(cur, *, account_config, as_of, excluded_accounts, owner_ratio):
    configs = {code: cfg for code, cfg in account_config.get('accounts', {}).items()
               if code not in excluded_accounts and cfg.get('active', True)
               and cfg.get('manual_balance') and position_reference_config(cfg)}
    if not configs:
        return []
    cur.execute("SELECT to_regclass('public.source_records') IS NOT NULL AS enabled")
    if not cur.fetchone()['enabled']:
        return []
    cur.execute(f"""
        SELECT s.account_code, i.raw_fields, i.parsed_fields, i.record_hash,
               m.primary_symbol AS symbol, m.canonical_name AS name, m.asset_type
        FROM source_records s
        JOIN pams_accounts a ON a.account_code=s.account_code
        JOIN source_items i USING(source_id)
        JOIN pams_asset_master m ON m.asset_id=i.parsed_fields->>'asset_id'
        WHERE s.account_code=ANY(%s) AND a.active AND s.document_type='holding'
          AND s.acquisition_method='manual' AND s.status='draft' AND i.review_status <> 'excluded'
          AND i.source_uid=concat('historical-user-confirmed:', i.raw_fields->>'snapshot_date', ':', s.account_code)
          AND m.status='active' AND m.currency='CNY'
          AND NOT EXISTS (
              SELECT 1 FROM pams_holdings_snapshot h
              WHERE h.account_code=s.account_code AND h.currency='CNY' AND h.as_of<=%s
                AND h.as_of::text >= i.raw_fields->>'snapshot_date'
                AND {point_in_time_visible_sql('h', '%s::date')}
                AND (h.asset_id=m.asset_id OR h.symbol=m.primary_symbol)
          )
        ORDER BY s.account_code, m.primary_symbol
    """, (list(configs), as_of, as_of))
    candidates = {}
    duplicates = set()
    for raw in cur.fetchall():
        code = raw['account_code']
        row = _reference_row(raw, configs[code], as_of, owner_ratio(code))
        if row is None:
            continue
        # A single account-level amount cannot be assigned to multiple instruments.
        if code in candidates:
            duplicates.add(code)
        candidates[code] = row
    return [row for code, row in candidates.items() if code not in duplicates]


def supports_dated_position_reference(row, mode):
    """Qualify an observed position for a dated display estimate, not a formal NAV."""
    if mode == "recorded_equity_and_debt":
        return (row.get("asset_type") in {"esop", "liability"}
                and row.get("snapshot_scope_member") in {"recorded", "verified"})
    if mode == "fund_snapshot_without_cash":
        # Older snapshots of the fund platform used the generic A-share category.
        return (row.get("source_type") == "holdings_snapshot"
                and row.get("asset_type") in {"a_share_fund", "fund", "a_share_stock"})
    return False
