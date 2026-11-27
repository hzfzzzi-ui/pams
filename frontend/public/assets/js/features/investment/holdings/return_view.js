import { esc, formatCompactCurrency, formatNumber, formatSignedCurrency, shortHoldingName } from './list_legacy_model.js';

const money = value => value == null ? '—' : formatCompactCurrency(value);
const percent = ratio => ratio == null ? '—' : formatNumber(Number(ratio) * 100) + '%';
const signedPct = ratio => ratio == null ? '' : `${Number(ratio) >= 0 ? '+' : ''}${formatNumber(Number(ratio) * 100)}%`;
const pnlTone = value => value == null || Number(value) === 0 ? '' : Number(value) > 0 ? 'up' : 'down';
const field = (label, value, { tone = '', sub = '' } = {}) =>
  `<div><small>${esc(label)}</small><strong${tone ? ` class="${tone}"` : ''}>${esc(value)}</strong>${sub ? `<span class="returnStatSub ${tone}">${esc(sub)}</span>` : ''}</div>`;
const kind = account => account.cash_only ? '现金管理' : account.shared ? '共同出资' : account.dedicated_loan ? '专属借款'
  : account.official_close ? '券商日结' : account.owner_ratio != null && Number(account.owner_ratio) === 1 ? '个人全资' : '投资账户';
function metricMoney(metric) {
  if (metric?.value == null) return '—';
  const prefix = ({estimated:'约 ', partial:'已知 ', carried_forward:'参考 '})[metric.status] || '';
  return prefix + money(metric.value);
}

export function createReturnView({document, scope, details, openPosition, showFinancing}) {
  const panel = document.getElementById('holdings-smart-table');
  const tabs = document.getElementById('holdingReturnTabs');
  const hero = document.getElementById('holdingReturnHero');
  const accounts = document.getElementById('holdingReturnAccounts');
  let overview, summary, rows = [], group = '全部', mode = 'positions';
  function render() {
    panel.dataset.returnMode = mode;
    document.body.dataset.positionOverview = String(mode === 'accounts' || group === '全部');
    for (const b of tabs.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.returnMode === mode));
    const accountMode = mode === 'accounts';
    accounts.hidden = !accountMode;
    hero.hidden = !accountMode && group !== '全部';
    const amount = accountMode ? metricMoney(overview?.net_value)
      : (summary?.missing_values ? '已知 ' : '') + money(summary?.market_value_cny);
    const recentDaily = overview?.recent_daily_pnl;
    const todayValue = summary?.daily_as_of ? summary.daily_pnl_cny : null;
    const dailyVal = todayValue ?? (recentDaily?.value != null ? Number(recentDaily.value) : null);
    const dailyValued = Number(summary?.daily_pnl_valued_count || 0);
    const dailyTotal = Number(summary?.daily_pnl_total_count || 0);
    const todayLabel = summary?.daily_pnl_estimated_count ? '今日盈亏（含估算）' : '今日盈亏';
    const dailyLabel = todayValue != null
      ? (dailyTotal > 0 && dailyValued < dailyTotal ? `${todayLabel} · 已知 ${dailyValued}/${dailyTotal}` : todayLabel)
      : (recentDaily?.value != null
        ? `最近一日盈亏${recentDaily.as_of ? ` · ${String(recentDaily.as_of).slice(5)}` : ''}`
        : '今日盈亏');
    const dailyText = dailyVal == null ? '—'
      : (todayValue == null && recentDaily?.status === 'partial' ? '已知 ' : '') + formatSignedCurrency(dailyVal);
    const dailySub = todayValue != null ? signedPct(summary?.daily_pnl_ratio) : '';
    const floatVal = summary?.floating_pnl_cny;
    const floatLabel = floatVal != null && Number(summary?.floating_pnl_missing || 0) > 0 ? '持仓浮盈 · 已知' : '持仓浮盈';
    const floatText = floatVal == null ? '—' : formatSignedCurrency(floatVal);
    const floatSub = signedPct(summary?.floating_pnl_ratio);
    hero.innerHTML = `<small>${accountMode ? '投资账户净值' : '个人持仓市值'}</small><strong>${esc(amount)}</strong>
      <div class="returnStats">${accountMode
        ? field('投资账户现金', metricMoney(overview?.cash))
        : field(dailyLabel, dailyText, { tone: pnlTone(dailyVal), sub: dailySub })
          + field(floatLabel, floatText, { tone: pnlTone(floatVal), sub: floatSub })
          + field('投资账户净值', metricMoney(overview?.net_value))}
      <button type="button" data-financing><small>融资负债 ›</small><b>${esc(metricMoney(overview?.financing))}</b></button></div>`;
    if (accountMode) {
      accounts.innerHTML = '<h3>投资账户</h3>' + (overview?.account_returns?.map(a =>
        `<button type="button" class="returnLink" data-account="${esc(a.account_code)}"><strong>${esc(a.name)}</strong><small>${kind(a)} ›</small></button>`).join('')
        || '<div class="mobileEmpty">暂无可核对账户</div>');
    }
  }
  function openAccount(code) {
    const a = overview?.account_returns?.find(item => item.account_code === code);
    if (!a) { details.show('账户收益', '<p>账户收益资料暂未取得</p>'); return; }
    const positions = rows.filter(r => r.account === code && !r.is_closed && !r.reference_as_of && !['现金/活期','负债'].includes(r.position_group));
    const metrics = [];
    let label = '个人持仓市值', value = money(a.market_value_cny);
    if (a.shared) {
      label = '个人净出资'; value = money(a.personal_capital);
      metrics.push(['整户净出资', money(a.account_capital)], ['净出资占比', percent(a.owner_ratio)],
        ['个人持仓市值', money(a.market_value_cny)], ['个人累计盈亏', '待按历史份额核对']);
    } else if (a.dedicated_loan) {
      label = '扣借款后权益'; value = money(a.position_equity_cny);
      metrics.push(['股权市值', money(a.market_value_cny)], ['专属借款', money(a.dedicated_debt_cny)],
        ['年内已入账分红', money(a.dividends_cny)], ['期间权益收益率', '待核对']);
    } else if (a.official_close) {
      label = '账户净清算价值'; value = metricMoney(a.net_value);
      metrics.push(['证券市值', money(a.market_value_cny)], ['杠杆倍数', a.leverage?.value == null ? '—' : formatNumber(a.leverage.value) + '×'],
        ['净现金', metricMoney(a.cash)], ['年内盈亏', a.reported_pnl?.basis === 'official_year_to_date' && a.reported_pnl.value != null ? formatSignedCurrency(a.reported_pnl.value) : '—'],
        ['人民币时间加权收益率','待核对']);
    } else if (a.cash_only) {
      label = '账户现金余额'; value = metricMoney(a.cash);
    } else {
      metrics.push(['个人归属', percent(a.owner_ratio)], ['剩余持仓成本', money(a.remaining_cost_cny)]);
    }
    const note = a.cash_only ? '按最近可核对的账户现金余额展示。' : a.shared ? '当前持仓按净出资占比折算。历史盈亏需核对每次出入金时的账户净值及分配约定。'
      : a.dedicated_loan ? '股权市值扣除专属借款后为个人权益。期间回报需要核对增持、自有资金、分红和借款利息。'
      : a.official_close ? '净清算价值已含融资负现金及应计项目。账户盈亏计入利息、费用与汇兑，出入金单列。'
      : '浮动盈亏以剩余持仓成本为基数。点开标的可查看累计盈亏与分红明细。';
    const body = details.show(a.name, `<section class="returnDetail"><small>${esc(label)}</small><strong>${esc(value)}</strong>
      ${a.as_of ? '<small>估值日 · '+esc(a.as_of)+'</small>' : ''}
      ${metrics.length ? '<div class="returnStats">' + metrics.map(pair => field(...pair)).join('') + '</div>' : ''}
      ${a.shared ? '<details data-funding><summary>出资记录</summary><div aria-live="polite"></div></details>' : ''}
      <details><summary>计算依据</summary><p>${esc(note)}</p></details>
      ${positions.length ? '<h3>持仓明细</h3>' : ''}
      ${positions.map((r,i) => `<button class="returnLink" type="button" data-position="${i}"><span>${esc(shortHoldingName(r))}</span><b>${money(r.market_value_cny)} ›</b></button>`).join('')}</section>`);
    const funding = body?.querySelector('[data-funding]');
    if (funding) funding.ontoggle = async () => {
      if (!funding.open || funding.dataset.loaded) return;
      const target = funding.querySelector('div');
      funding.dataset.loaded = 'true';
      target.textContent = '正在读取…';
      try {
        const result = await scope.get('/api/investment/account-funding-history?account=' + encodeURIComponent(code));
        target.innerHTML = result.rows?.length ? result.rows.slice().reverse().map(day => {
          const amounts = [['个人',day.personal_change],['其他出资人',day.other_change]]
            .filter(([,v]) => Number(v) !== 0).map(([who,v]) => who + ' ' + formatSignedCurrency(v)).join(' / ');
          return field(day.date, amounts || '净变动 ¥0.00');
        }).join('') : '暂无已核对出资记录';
      } catch { delete funding.dataset.loaded; target.textContent = '读取失败，收起后可重试'; }
    };
    for (const b of body?.querySelectorAll('[data-position]') || []) b.onclick = () => openPosition(positions[Number(b.dataset.position)], summary);
  }
  scope.listen(accounts,'click', e => { const b = e.target.closest('[data-account]'); if (b) openAccount(b.dataset.account); });
  scope.listen(tabs,'click', e => { const b = e.target.closest('[data-return-mode]'); if (b) {mode = b.dataset.returnMode; render();} });
  scope.listen(hero,'click', e => { if (e.target.closest('[data-financing]')) {mode = 'positions'; showFinancing(); render();} });
  scope.listen(document.getElementById('holdingBasis'),'click', () => details.show('数字口径',
    '<div class="returnDetail"><div class="returnStats">'+[
      ['第一行','个人市值 / 市值占比'],['第二行','浮动盈亏 / 浮动盈亏率'],['员工持股第二行','年内分红 / 年内分红占持仓成本'],['第三行','最近一日盈亏 / 标的涨跌幅'],
      ['占比分母','全部个人持仓市值'],['缺少可核对数据','—'],
    ].map(pair=>field(...pair)).join('')+'</div></div>'));
  return { openAccount, update(data) { ({overview, summary, rows, group} = data); render(); } };
}
