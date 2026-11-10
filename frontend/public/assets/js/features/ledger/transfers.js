import { isReadCancelled } from '../../shared/read_scope.js';
import { escapeHtml as escape, formatMoneyFixed2 as money } from '../../utils.js';
import { confirmTransferPair, readLedgerTransfers, readTransferCandidates } from './requests.js';

const TYPES = {
  internal_transfer: '普通转账',
  credit_card_payment: '信用卡还款',
  investment_funding: '投资入金',
  investment_withdrawal: '投资出金',
};
const FIRST_PAGE = 10;
const moneyText = (value, currency) => value == null ? '—' : money(value, currency);
const byDate = (left, right) => right.match_date.localeCompare(left.match_date);

export function createLedgerTransfers({ root, state, model, dom, events, reads, current, memory, variant, showRefresh = () => {}, reloadWorkbench }) {
  const { $ } = dom;
  let data = null;
  let candidates = null;
  let loadingCandidates = false;
  let submitting = false;
  let confirmedLimit = FIRST_PAGE;
  let pendingLimit = FIRST_PAGE;

  function message(id, text) {
    const element = $(id);
    element.textContent = text;
    element.hidden = !text;
  }

  function renderPair(pair, pending) {
    const from = escape(model.accountDisplayName(pair.from_account_code));
    const to = escape(model.accountDisplayName(pair.to_account_code));
    const currency = pair.currency_norm || pair.currency;
    const amount = escape(moneyText(pair.amount, currency));
    const label = escape(TYPES[pair.match_type] || '内部划转');
    const date = escape(pair.match_date);
    const fx = !pending && currency !== 'CNY'
      ? `<small>${pair.amount_cny == null ? '人民币折算待补' : escape(moneyText(pair.amount_cny))}</small>` : '';
    const ambiguous = pending && pair.ambiguity_status === 'ambiguous';
    const leg = (title, name, day, fact, summary, sign) => `
      <div class="ledgerPairLeg">
        <strong>${title} · ${name}</strong><span>${sign}${amount}</span>
        <small>${escape(day)}</small>
        <div>${escape(fact?.summary || summary || '暂无摘要')}</div>
        ${fact?.description && fact.description !== fact.summary ? `<small>${escape(fact.description)}</small>` : ''}
        ${fact?.source_item_id ? `<small>来源明细 ${escape(fact.source_item_id)}</small>` : ''}
      </div>`;
    return `
      <details class="ledgerPair">
        <summary>
          <div><strong>${from} <span aria-hidden="true">→</span> ${to}</strong>
            <small>${date} · ${label}${ambiguous ? ' · 有多个候选' : ''}</small></div>
          <div class="ledgerPairAmount"><strong>${amount}</strong>${fx}</div>
        </summary>
        <div class="ledgerPairDetails">
          ${leg('转出', from, pair.source_date, pair.source, pair.source_summary, '−')}
          ${leg('转入', to, pair.target_date, pair.target, pair.target_summary, '+')}
          ${pending ? `<p class="muted">${escape(pair.note || '')}</p>
            <button class="btn" type="button" data-confirm-pair="${escape(pair.match_key)}">确认这两笔配对</button>` : ''}
        </div>
      </details>`;
  }

  function renderList(element, rows, pending) {
    const limit = pending ? pendingLimit : confirmedLimit;
    const empty = pending ? '本期暂无可核对的配对候选。' : '本期暂无已确认的划转。';
    element.innerHTML = rows.length ? rows.slice(0, limit).map(row => renderPair(row, pending)).join('') : `<p class="muted">${empty}</p>`;
    if (rows.length > limit) {
      element.insertAdjacentHTML('beforeend', `<button class="btn ledgerPairMore" type="button"
        data-expand-pairs="${pending ? 'pending' : 'confirmed'}">展开更多</button>`);
    }
  }

  function render({includeCandidates = true} = {}) {
    if (!data) return;
    const isYear = state.transferPeriod === 'ytd';
    const inPeriod = row => isYear
      ? row.match_date.startsWith(data.month.slice(0, 4)) && row.match_date.slice(0, 7) <= data.month
      : row.match_date.startsWith(data.month);
    const confirmed = [...(data.transfer_pairs || [])].filter(inPeriod).sort(byDate);
    const summary = isYear ? data.transfer_summary_ytd : data.transfer_summary_month;
    $('transferSummary').innerHTML = Object.entries(TYPES).map(([kind, label]) => {
      const item = (summary || []).find(row => row.match_type === kind);
      return `<div><span>${label}</span><strong>${moneyText(item ? item.amount : 0)}</strong><small>${item?.count || 0} 对</small></div>`;
    }).join('');
    $('transferConfirmedCount').textContent = `${confirmed.length} 对`;
    renderList($('transferConfirmed'), confirmed, false);
    if (!includeCandidates || !candidates) return;
    const pending = candidates.filter(inPeriod).sort(byDate);
    $('transferPendingCount').textContent = `${pending.length} 个候选`;
    renderList($('transferPending'), pending, true);
  }

  async function loadCandidates() {
    if (loadingCandidates) return;
    loadingCandidates = true;
    candidates = null;
    $('transferPendingCount').textContent = '';
    $('transferPending').innerHTML = '<p class="muted">正在查找候选…</p>';
    try {
      const result = await readTransferCandidates();
      if (!current()) return;
      candidates = (result.matches || []).filter(row => row.status === 'review_required'
        && row.confirmation_supported && Object.hasOwn(TYPES, row.match_type) && row.match_key?.startsWith('candidate:'));
      render();
    } catch {
      if (current()) $('transferPending').innerHTML = '<button class="btn" type="button" data-retry-candidates>候选加载失败，重试</button>';
    } finally {
      loadingCandidates = false;
    }
  }

  async function load({replay = false, onReplay} = {}) {
    if (!$('transferSummary')) return;
    data = null;
    showRefresh();
    confirmedLimit = pendingLimit = FIRST_PAGE;
    $('transferConfirmedCount').textContent = '';
    $('transferConfirmed').textContent = '';
    $('transferPendingCount').textContent = '';
    $('transferPending').textContent = '';
    const history = state.transferPeriod === 'history';
    $('transferMonthPicker').hidden = !history;
    root.querySelectorAll('[data-transfer-period]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.transferPeriod === state.transferPeriod));
    });
    const month = history ? state.transferMonth : '';
    if (history && !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
      reads.cancel('transfers');
      $('transferSummary').textContent = '请选择要查看的月份。';
      return;
    }
    const cacheKey = variant + '|' + month;
    const remembered = replay && memory?.read('transfers', cacheKey);
    showRefresh(remembered ? 'refreshing' : '');
    if (remembered) {
      data = remembered;
      render({includeCandidates: false});
      onReplay?.();
    } else {
      $('transferSummary').textContent = '正在加载划转…';
    }
    try {
      const result = await reads.run('transfers', signal => readLedgerTransfers({ month, signal }));
      if (!current()) return;
      data = result;
      render();
      memory?.write('transfers', cacheKey, result);
      showRefresh();
      if (!candidates && !loadingCandidates) loadCandidates();
      else if (!candidates) $('transferPending').innerHTML = '<p class="muted">正在查找候选…</p>';
    } catch (error) {
      if (current() && !isReadCancelled(error)) {
        if (remembered) {
          showRefresh('failed');
          return;
        }
        $('transferSummary').innerHTML = '<button class="btn" type="button" data-retry-transfers>划转加载失败，重试</button>';
      }
    }
  }

  function bind() {
    const panel = $('transfers')?.parentElement;
    if (!panel) return;
    $('transferMonth').value = state.transferMonth;
    events.listen('page', $('transferMonth'), 'change', event => {
      state.transferMonth = event.target.value;
      load();
    });
    events.listen('page', panel, 'click', async event => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.dataset.transferPeriod) {
        state.transferPeriod = button.dataset.transferPeriod;
        load();
      } else if (button.hasAttribute('data-retry-transfers')) {
        load();
      } else if (button.hasAttribute('data-retry-candidates')) {
        loadCandidates();
      } else if (button.dataset.expandPairs) {
        if (button.dataset.expandPairs === 'pending') pendingLimit = Infinity;
        else confirmedLimit = Infinity;
        render();
      } else if (button.dataset.confirmPair && !submitting) {
        const pair = candidates?.find(row => row.match_key === button.dataset.confirmPair);
        if (!pair) return;
        submitting = true;
        button.disabled = true;
        message('transferPairNotice', '正在确认配对…');
        try {
          await confirmTransferPair(pair);
          if (!current()) return;
          message('transferPairNotice', '配对已确认，正在更新划转和收支总览。');
          candidates = null;
          await Promise.all([load(), reloadWorkbench().catch(() => {
            if (current()) message('transferPairNotice', '配对已确认。收支总览刷新失败，请刷新页面查看。');
          })]);
        } catch (error) {
          if (current()) message('transferPairNotice', error.message || '操作未完成，请重试。');
        } finally {
          submitting = false;
          if (current()) button.disabled = false;
        }
      }
    });
  }

  return { bind, load, render };
}
