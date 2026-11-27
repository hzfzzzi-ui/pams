import assert from 'node:assert/strict';
import test from 'node:test';
import { createSummaryView } from '../../frontend/public/assets/js/features/investment/holdings/summary_view.js';

test('股票小计跟随当前市场，区分零收益与缺少行情', () => {
  const filter = { value: '股票', dataset: { heroGroup: '全部', segment: '' } };
  const hero = { classList: { toggle() {} } };
  const elements = { holdingGroupFilter: filter, mobilePortfolioHero: hero };
  const view = createSummaryView({
    document: { getElementById: id => elements[id] },
    host: {},
  });
  const stock = { daily_as_of: '2026-05-08', daily_pnl_cny: 7, daily_pnl_valued_count: 2, daily_pnl_total_count: 3 };
  view.renderInvestmentDashboard({
    mobileProfiles: { '全部': { dailyPnl: 100, hideHeader: true }, '美股/海外': { dailyPnl: 90 } },
    filterSummaries: { '股票': { '': stock, '美股': { ...stock, daily_pnl_cny: 0, daily_pnl_total_count: 2 } } },
  });
  assert.match(hero.innerHTML, /今日盈亏 · 05\/08 · 已知 2\/3<\/span><strong[^>]*>\+¥7\.00</);
  filter.dataset.segment = '美股';
  view.updateMobilePortfolioHero('美股');
  assert.match(hero.innerHTML, /今日盈亏 · 05\/08<\/span><strong[^>]*>\+?¥0\.00</);
  filter.dataset.segment = '韩股';
  view.updateMobilePortfolioHero('韩股');
  assert.match(hero.innerHTML, /今日盈亏<\/span><strong[^>]*>—</);
});
