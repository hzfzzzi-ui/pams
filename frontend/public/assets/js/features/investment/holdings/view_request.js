import {createPageRead} from '../../../shared/page_read.js';

export const loadHoldingView = createPageRead(() => {
  const closed = new URLSearchParams(location.search).get('show_closed') === '1';
  return `/api/investment/holdings-view?show_closed=${closed}`;
});

export const loadAccountView = createPageRead(() => '/api/investment/accounts-view');
export const accountPositionsPath = '/api/holdings/by-account?base_currency=CNY&include_portfolio_cash=true';
export const loadAccountPositions = createPageRead(() => accountPositionsPath);
