"""Current, consistent inputs for the investment risk and allocation panels."""
from functools import lru_cache

import psycopg2.extras

from domain.assets.lookthrough_state import build_lookthrough_payload
from domain.investments.portfolio_state import current_risk_position_state, get_conn, load_current_portfolio_inputs
from domain.investments.portfolio_valuation_state import latest_published_portfolio_valuation_state
from domain.risk.portfolio_config import _asset_label_lookup, _asset_lookup_candidates, build_portfolio_config_payload_from_inputs
from domain.risk.portfolio_risk_disclosure import build_portfolio_risk_disclosure_payload_from_inputs
from domain.risk.risk_state import build_risk_state


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
            result = {}
            if include_risk:
                result['risk'] = build_portfolio_risk_disclosure_payload_from_inputs(
                    risk, lookthrough, base_currency=base_currency)
            if include_config:
                lookup = _asset_label_lookup(cur, _asset_lookup_candidates(lookthrough))
                result['config'] = build_portfolio_config_payload_from_inputs(
                    risk, lookthrough, base_currency=base_currency, asset_label_lookup=lookup)
            return result
