import assert from 'node:assert/strict';
import {dilutedAveragePrice} from '../../frontend/public/assets/js/features/investment/holdings/list_legacy_model.js';

assert.equal(dilutedAveragePrice({quantity: 100, cost: 1000, realized_trade_pnl: 200,
  diluted_cost: '775', diluted_cost_source: 'verified_trade_snapshot'}), 7.75);
assert.equal(dilutedAveragePrice({quantity: 100, cost: 1000, realized_trade_pnl: 200}), 8);
assert.equal(dilutedAveragePrice({quantity: 100, diluted_cost: '-100',
  diluted_cost_source: 'verified_trade_snapshot'}), -1);
