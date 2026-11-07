"""Canonical anchors for bank accounts with a verified cash-only snapshot."""

from domain.investments.snapshot_coverage import load_account_snapshot_coverage


SOURCE = 'verified_bank_balance_snapshot'


def load_verified_bank_balances(cur, account_cfg, as_of):
    accounts = {
        code: config.get('base_currency', 'CNY').upper()
        for code, config in (account_cfg.get('accounts') or {}).items()
        if config.get('account_type') == 'funding'
        and config.get('truth_source') in {'bank_statement', 'official_statement'}
        and config.get('active', True) and not config.get('exclude_from_reconcile')
    }
    if not accounts:
        return {}
    cur.execute('''WITH latest AS (
        SELECT h.account_code, MAX(h.as_of) AS as_of
        FROM public.pams_holdings_snapshot h JOIN public.pams_accounts a USING (account_code)
        WHERE a.active AND h.account_code=ANY(%s::text[]) AND h.as_of<=%s
        GROUP BY h.account_code
    ), snapshot AS (
        SELECT h.id, h.account_code, h.as_of, h.currency, h.market_value, h.source_item_id,
               h.major_category, h.asset_liability, COUNT(*) OVER (PARTITION BY h.account_code) AS members
        FROM latest JOIN public.pams_holdings_snapshot h USING (account_code, as_of)
    )
    SELECT * FROM snapshot WHERE members=1 AND major_category='现金' AND asset_liability='资产'
        AND source_item_id IS NOT NULL''', (list(accounts), as_of))
    rows = [row for row in cur.fetchall() if row['currency'] == accounts[row['account_code']]]
    coverage = load_account_snapshot_coverage(cur, {(row['account_code'], row['as_of']) for row in rows})
    balances = {}
    for row in rows:
        proof = coverage[(row['account_code'], row['as_of'])]
        if proof['status'] == 'complete' and proof['member_ids'] == {row['id']}:
            balances[row['account_code']] = {
                'amount': row['market_value'], 'currency': row['currency'], 'as_of': row['as_of'], 'source': SOURCE,
            }
    return balances


def prefer_verified_bank_balance(snapshot, selected):
    return snapshot is not None and (
        selected is None or str(snapshot['as_of']) >= str(selected.get('as_of') or '')
    )
