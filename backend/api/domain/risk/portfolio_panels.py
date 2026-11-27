"""Current, consistent inputs for the investment risk and allocation panels."""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import psycopg2.extras

from domain.assets.lookthrough_state import build_lookthrough_payload
from domain.investments.valuation.investment_overview import holdings_overview_card
from domain.investments.valuation.page_projection import read_page
from domain.investments.valuation.portfolio_state import (
    _load_accounts_config,
    current_risk_position_state,
    get_conn,
    load_current_portfolio_inputs,
)
from domain.investments.valuation.portfolio_valuation_state import latest_published_portfolio_valuation_state
from domain.risk.portfolio_config import _asset_label_lookup, _asset_lookup_candidates, build_portfolio_config_payload_from_inputs
from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload_from_inputs
from domain.risk.risk_state import build_risk_state

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _read_account_cards(cur):
    try:
        today = datetime.now(_SHANGHAI).date()
        projected = read_page(cur, 'holdings', business_date=today, member='account_cards')
        data = (projected or {}).get('data')
        if not isinstance(data, dict):
            return {}
        if 'investment_overview' not in data:
            full = read_page(cur, 'holdings', business_date=today)
            page_data = (full or {}).get('data')
            if isinstance(page_data, dict) and page_data.get('holdings') and page_data.get('accounts'):
                data = {**data, 'investment_overview': holdings_overview_card(page_data, _load_accounts_config())}
        return data
    except Exception:
        return {}


def build_portfolio_panels(*, base_currency='CNY', include_risk=True, include_config=True):
    base = str(base_currency or 'CNY').upper()
    with get_conn() as conn:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            @lru_cache(maxsize=2)
            def inputs(currency):
                return load_current_portfolio_inputs(cur, base_currency=currency, lookthrough=True)

            @lru_cache(maxsize=2)
            def exposure(currency):
                return build_lookthrough_payload(cur, base_currency=currency, current_inputs=inputs(currency))

            risk = build_risk_state(
                position_state=current_risk_position_state(cur, inputs=inputs('CNY')),
                exposure_loader=exposure,
                valuation_loader=lambda currency: latest_published_portfolio_valuation_state(cur, base_currency=currency),
            )
            lookthrough = exposure(base)
            account_cards = _read_account_cards(cur)
            result = {}
            if include_risk:
                result['risk'] = build_portfolio_risk_disclosure_payload_from_inputs(
                    risk, lookthrough, base_currency=base_currency, account_cards=account_cards)
            if include_config:
                lookup = _asset_label_lookup(cur, _asset_lookup_candidates(lookthrough))
                result['config'] = build_portfolio_config_payload_from_inputs(
                    risk, lookthrough, base_currency=base_currency, asset_label_lookup=lookup, account_cards=account_cards)
            return result
