"""Account read adapters."""

from __future__ import annotations

from decimal import Decimal, localcontext
from datetime import date as Date
from typing import Any

from domain.investments.holdings_view import COMPLETE_ACCOUNT_SNAPSHOT_CTE


def _norm(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_accounts_payload(cur, *, active: bool) -> dict[str, list[dict[str, Any]]]:
    """Return the legacy /api/accounts payload."""
    if active:
        cur.execute(
            """
            SELECT account_code, display_name, base_currency, is_asset
            FROM pams_accounts
            WHERE active = true
            ORDER BY display_name
            """
        )
    else:
        cur.execute(
            """
            SELECT account_code, display_name, base_currency, is_asset, active
            FROM pams_accounts
            ORDER BY display_name
            """
        )
    rows = cur.fetchall()
    return {"accounts": [{key: _norm(value) for key, value in dict(row).items()} for row in rows]}


def build_account_detail_payload(cur, *, account_code: str) -> dict[str, Any] | None:
    """Return the legacy /api/accounts/{account_code} payload, or None when missing."""
    cur.execute(
        """
        SELECT account_code, display_name, base_currency, is_asset, active, created_at
        FROM pams_accounts
        WHERE account_code = %s
        """,
        (account_code,),
    )
    row = cur.fetchone()

    if not row:
        return None

    cur.execute(
        f"""WITH {COMPLETE_ACCOUNT_SNAPSHOT_CTE}
        SELECT la.account_as_of AS as_of, hs.currency,
               COUNT(*) AS holdings_count,
               COUNT(*) FILTER (WHERE hs.market_value IS NULL) AS missing_values,
               SUM(CASE WHEN hs.asset_liability='负债' THEN -ABS(hs.market_value)
                        ELSE hs.market_value END) AS market_value
        FROM pams_holdings_snapshot hs JOIN latest_account la
          ON la.account_code=hs.account_code AND la.account_as_of=hs.as_of
        WHERE hs.account_code=%s GROUP BY la.account_as_of,hs.currency""",
        (Date.today(), account_code),
    )
    holdings = [dict(item) for item in cur.fetchall()]
    account_data = {key: _norm(value) for key, value in dict(row).items()}
    account_data["holdings_count"] = sum(item['holdings_count'] for item in holdings)
    account_data["currency_values"] = [{"currency": item.get('currency'),
        "amount": None if item['missing_values'] or item.get('market_value') is None else str(item['market_value']),
        "as_of": _norm(item.get('as_of')), "source_evidence": "unknown"} for item in holdings]
    account_data["total_value"] = None
    account_data["total_value_status"] = "unknown"
    if not holdings or any(item['missing_values'] or not item.get('currency') or not item.get('as_of') for item in holdings):
        return account_data
    dates = {item['as_of'] for item in holdings}
    base = str(row.get('base_currency') or '').upper()
    if len(dates)!=1 or len(base)!=3 or not base.isascii() or not base.isalpha():
        return account_data
    day=next(iter(dates))
    from domain.investments.snapshot_coverage import load_account_snapshot_coverage
    proof=load_account_snapshot_coverage(cur,{(account_code,day)}).get((account_code,day),{})
    if proof.get('status')!='complete':
        return account_data
    for value in account_data["currency_values"]:
        value["source_evidence"] = "recorded"
    currencies={str(item['currency']).upper() for item in holdings}
    if any(len(c)!=3 or not c.isascii() or not c.isalpha() for c in currencies):
        return account_data
    try:
        with localcontext() as context:
            context.prec = 80
            amounts={str(item['currency']).upper():Decimal(str(item['market_value'])) for item in holdings}
            if any(not value.is_finite() for value in amounts.values()):
                return account_data
            nonzero={currency for currency,value in amounts.items() if value!=0}
            if not nonzero:
                total=Decimal(0)
            elif nonzero=={base}:
                total=amounts[base]
            else:
                needed=(nonzero|{base})-{'CNY'}
                cur.execute("SELECT to_regclass('public.pams_fx_rates_daily') IS NOT NULL AS present")
                available=cur.fetchone()
                if not available or not available.get('present'):
                    return account_data
                cur.execute("""SELECT quote_currency,rate FROM pams_fx_rates_daily WHERE base_currency='CNY'
                    AND quote_currency=ANY(%s::text[]) AND as_of=%s AND rate>0
                    AND rate::text NOT IN ('NaN','Infinity','-Infinity') AND NULLIF(BTRIM(source),'') IS NOT NULL""",
                    (sorted(needed),day))
                rates={str(item['quote_currency']).upper():Decimal(str(item['rate'])) for item in cur.fetchall()}
                rates['CNY']=Decimal(1)
                if any(c not in rates or not rates[c].is_finite() or rates[c]<=0 for c in needed):
                    return account_data
                total=sum((amounts[c]*rates[c] for c in nonzero),Decimal(0))/rates[base]
            if not total.is_finite():
                return account_data
            account_data["total_value"] = str(total.quantize(Decimal('0.01')))
    except (ValueError,TypeError,ArithmeticError):
        return account_data
    account_data["total_value_status"] = "complete"
    account_data["total_value_as_of"] = _norm(day)
    return account_data
