import { isReadCancelled } from '../../shared/read_scope.js';
import { escapeHtml as escape, formatMoneyFixed2 as money } from '../../utils.js';
import { renderLedgerDays } from './days.js';
import { confirmTransferPair, readLedgerTransfers, readManualTransferOptions, readTransferCandidates } from './requests.js';

const TYPES = {
  internal_transfer: '普通转账',
  credit_card_payment: '信用卡还款',
  investment_funding: '投资入金',
  investment_withdrawal: '投资出金',
  fx_conversion: '换汇',
};
const FIRST_PAGE = 10;
const moneyText = (value, currency) => value == null ? '—' : money(value, currency);
const byDate = (left, right) => right.match_date.localeCompare(left.match_date);

function transferParty({name, institution, logoUrl}, direction) {
  const title = institution || name;
  return `<span class="ledgerPairParty" data-direction="${direction}">
    <span class="ledgerPairAvatar" aria-hidden="true">${logoUrl
      ? `<img src="${escape(logoUrl)}" alt="" width="46" height="46" decoding="async">`
      : escape(title.slice(0, 1))}</span>
    ${institution ? `<small>${escape(institution)}</small>` : ''}
    <strong>${escape(name)}</strong>
  </span>`;
}

function currentBusinessMonth() {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit'}).formatToParts(new Date());
  return `${parts.find(part => part.type === 'year').value}-${parts.find(part => part.type === 'month').value}`;
}

export function createLedgerTransfers({ root, state, model, dom, events, reads, current, memory, variant, showRefresh = () => {}, reloadWorkbench }) {
  const { $ } = dom;
  let data = null;
  let review = '';
  let candidates = null;
  let loadingCandidates = false;
  let submitting = false;
  let confirmedLimit = FIRST_PAGE;
  let pendingLimit = FIRST_PAGE;
  let manualLimit = FIRST_PAGE;
  const manualOptions = new WeakMap();

  function accountName(code) {
    const account = model.ledgerAccountIdentity(code);
    return [account.institution, account.name].filter(Boolean).join(' ');
  }

  function manualLeg(row) {
    return `<span class="ledgerPairLeg">
      <strong>${escape(accountName(row.account_code))}</strong>
      <span>${row.amount > 0 ? '+' : ''}${escape(moneyText(row.amount, row.currency))}</span>
      <small>${escape(row.transaction_date)} · ${escape(row.description || row.summary || '暂无摘要')}</small>
    </span>`;
  }

  function renderManualPending() {
    const host = $('transferManualPending');
    if (!data || review !== 'pending') return;
    const rows = (data.manual_pending || []).filter(row => state.transferPeriod === 'ytd'
      || row.transaction_date.startsWith(data.month));
    host.innerHTML = rows.length ? `<h4>单边待配对 <small>${rows.length} 笔</small></h4>` +
      rows.slice(0, manualLimit).map(row => `<details class="ledgerPair" data-manual-anchor="${row.id}">
        <summary>${manualLeg(row)}<small>${escape(row.subcategory || '内部转账')} · 待选${row.amount > 0 ? '转出' : '转入'}流水</small></summary>
        <div class="ledgerPairDetails">
          ${row.source_evidence_status === 'verified' ? `
            <div class="ledgerManualPairSearch">
              <label>查找月份 <input type="month" class="formInput" data-manual-month max="${currentBusinessMonth()}"
                placeholder="YYYY-MM" pattern="[0-9]{4}-(0[1-9]|1[0-2])"></label>
              <button class="btn" type="button" data-find-manual>查找另一笔</button>
            </div>
            <div data-manual-options aria-live="polite"></div>`
            : '<p class="muted">来源凭证待核对。</p>'}
        </div>
      </details>`).join('') + (rows.length > manualLimit
        ? '<button class="btn ledgerPairMore" type="button" data-expand-manual>展开更多单边流水</button>' : '') : '';
  }

  async function findManualOptions(button) {
    const card = button.closest('[data-manual-anchor]');
    const list = card.querySelector('[data-manual-options]');
    const find = card.querySelector('[data-find-manual]');
    const anchorId = Number(card.dataset.manualAnchor);
    const monthInput = card.querySelector('[data-manual-month]');
    const month = monthInput.value;
    if (!monthInput.reportValidity()) return;
    const previous = manualOptions.get(card);
    const offset = previous?.month === month ? Number(button.dataset.manualOffset || 0) : 0;
    const selection = offset ? previous.choices : new Map();
    if (find.disabled) return;
    if (!offset) list.textContent = '正在查找…';
    find.disabled = button.disabled = monthInput.disabled = true;
    try {
      const result = await readManualTransferOptions(anchorId, {month, offset});
      if (!current() || !card.isConnected) return;
      list.querySelector('[data-manual-offset]')?.remove();
      const anchor = data.manual_pending.find(row => row.id === anchorId);
      if (!anchor) return;
      for (const row of result.rows) {
        const [source, target] = anchor.amount < 0 ? [anchor, row] : [row, anchor];
        selection.set(row.id, {source_row_id: source.id, target_row_id: target.id, manual_anchor_id: anchorId});
      }
      manualOptions.set(card, {month, choices: selection});
      if (!offset) list.textContent = result.rows.length ? '' : '这个范围没有可选流水。';
      list.insertAdjacentHTML('beforeend', result.rows.map(row => `<details class="ledgerPair">
        <summary>${manualLeg(row)}</summary>
        <div class="ledgerPairDetails">
          <p class="muted">${row.match_type === 'fx_conversion' ? '换汇 · ' : ''}请核对两笔流水是否为同一笔资金。</p>
          <button class="btn" type="button" data-confirm-manual="${row.id}">确认这两笔配对</button>
          <p class="muted" data-pair-status role="status"></p>
        </div>
      </details>`).join(''));
      if (result.next_offset != null) list.insertAdjacentHTML('beforeend',
        `<button class="btn ledgerPairMore" type="button" data-manual-offset="${result.next_offset}">查看更多流水</button>`);
    } catch (error) {
      if (current() && card.isConnected) {
        list.textContent = error.status === 409
          ? '流水状态已变更或查询月份无效，请刷新待配对列表后重试。' : '读取失败，请重新查找。';
      }
    } finally {
      find.disabled = button.disabled = monthInput.disabled = false;
    }
  }

  function message(id, text) {
    const element = $(id);
    element.textContent = text;
    element.hidden = !text;
  }

  function renderPair(pair, pending = false) {
    const sender = model.ledgerAccountIdentity(pair.from_account_code);
    const receiver = model.ledgerAccountIdentity(pair.to_account_code);
    const name = account => [account.institution, account.name].filter(Boolean).join(' ');
    const from = name(sender);
    const to = name(receiver);
    const currency = pair.currency_norm || pair.currency;
    const value = moneyText(pair.amount, currency);
    const amount = escape(value);
    const differentCurrency = pair.target_currency && pair.target_currency !== currency;
    const targetValue = differentCurrency ? moneyText(pair.target_amount, pair.target_currency) : value;
    const label = TYPES[pair.match_type] || '内部划转';
    const date = escape(pair.match_date);
    const conversion = !pending && currency !== 'CNY'
      ? pair.amount_cny == null ? '人民币折算待补' : `≈ ${moneyText(pair.amount_cny)}` : '';
    const ambiguous = pending && pair.ambiguity_status === 'ambiguous';
    const accessible = ['转出 ' + from + ' ' + value, '转入 ' + to + ' ' + targetValue, label, pair.match_date, conversion,
      ambiguous ? '有多个候选' : ''].filter(Boolean).join('，');
    const leg = (title, account, day, fact, summary, legAmount) => `
      <div class="ledgerPairLeg">
        <strong>${title} · ${escape(account)}</strong><span>${escape(legAmount)}</span>
        <small>${escape(day)}</small>
        <div>${escape(fact?.summary || summary || '暂无摘要')}</div>
        ${fact?.description && fact.description !== fact.summary ? `<small>${escape(fact.description)}</small>` : ''}
        ${fact?.source_item_id ? `<small>来源明细 ${escape(fact.source_item_id)}</small>` : ''}
      </div>`;
    return `
      <details class="ledgerPair">
        <summary class="ledgerPairRoute" aria-label="${escape(accessible)}">
          ${transferParty(sender, 'out')}
          <span class="ledgerPairCenter">
            <strong class="ledgerPairAmount">${differentCurrency ? '−' : ''}${amount}</strong>
            ${differentCurrency ? `<strong class="ledgerPairAmount">+${escape(targetValue)}</strong>` : ''}
            <span class="ledgerPairArrow" aria-hidden="true"></span>
            <small>${escape(label)}</small>
            <time datetime="${date}">${date}</time>
            ${ambiguous ? '<small>有多个候选</small>' : ''}
            ${conversion ? `<small>${escape(conversion)}</small>` : ''}
          </span>
          ${transferParty(receiver, 'in')}
        </summary>
        <div class="ledgerPairDetails">
          ${leg('转出', from, pair.source_date, pair.source, pair.source_summary, '−' + value)}
          ${leg('转入', to, pair.target_date, pair.target, pair.target_summary, '+' + targetValue)}
          ${differentCurrency && pair.settlement_rate != null
            ? `<small>两端金额折算率 ${Number(pair.settlement_rate).toFixed(5)} ${escape(currency)}/${escape(pair.target_currency)}</small>` : ''}
          ${pending ? `<p class="muted">${escape(pair.note || '')}</p>
            <button class="btn" type="button" data-confirm-pair="${escape(pair.match_key)}">确认这两笔配对</button>
            <p class="muted" data-pair-status role="status"></p>` : ''}
        </div>
      </details>`;
  }

  function renderList(element, rows, pending) {
    const limit = pending ? pendingLimit : confirmedLimit;
    const empty = pending ? '本期暂无可核对的配对候选。' : '本期暂无已确认的划转。';
    element.innerHTML = rows.length
      ? renderLedgerDays(rows.slice(0, limit), row => row.match_date, row => renderPair(row, pending), {
        countUnit: pending ? '个候选' : '对',
        partialLastDay: limit < rows.length && rows[limit - 1].match_date === rows[limit].match_date,
      })
      : `<p class="muted">${empty}</p>`;
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
      if (kind === 'fx_conversion' && !item) return '';
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

  async function load({replay = false} = {}) {
    if (!review) return;
    data = null;
    showRefresh();
    confirmedLimit = pendingLimit = manualLimit = FIRST_PAGE;
    $('transferConfirmedCount').textContent = '';
    $('transferConfirmed').textContent = '';
    $('transferPendingCount').textContent = '';
    $('transferPending').textContent = '';
    $('transferManualPending').textContent = '';
    const history = state.transferPeriod === 'history';
    $('transferMonthPicker').hidden = !history;
    root.querySelectorAll('[data-transfer-period]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.transferPeriod === state.transferPeriod));
    });
    const month = history ? state.transferMonth : '';
    $('transferMonth').max = currentBusinessMonth();
    if (history && month > $('transferMonth').max) {
      reads.cancel('transfers');
      $('transferSummary').textContent = '历史月份不能晚于当前业务月。';
      return;
    }
    if (history && !/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
      reads.cancel('transfers');
      $('transferSummary').textContent = '请选择要查看的月份。';
      return;
    }
    const manualPending = review === 'pending';
    const cacheKey = variant + '|' + month + (manualPending ? '|pending' : '');
    const remembered = replay && memory?.read('transfers', cacheKey);
    showRefresh(remembered ? 'refreshing' : '');
    if (remembered) {
      data = remembered;
      render({includeCandidates: false});
    } else {
      $('transferSummary').textContent = '正在加载划转…';
    }
    try {
      const result = await reads.run('transfers', signal => readLedgerTransfers({ month, manualPending, signal }));
      if (!current()) return;
      data = result;
      render();
      renderManualPending();
      memory?.write('transfers', cacheKey, result);
      showRefresh();
      if (review === 'pending' && !candidates) {
        if (!loadingCandidates) loadCandidates();
        else $('transferPending').innerHTML = '<p class="muted">正在查找候选…</p>';
      }
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

  function selectReview(next = '') {
    if (review === next) return;
    review = next;
    const open = Boolean(review);
    $('transferReviewPanel').hidden = !open;
    $('recentRows').hidden = open;
    $('recentStreamToolbar').hidden = open;
    $('recentStreamScope').hidden = open;
    $('transferConfirmedSection').hidden = review !== 'confirmed';
    $('transferPendingSection').hidden = review !== 'pending';
    $('transferReviewActions').querySelectorAll('[data-transfer-review]').forEach(button => {
      button.setAttribute('aria-expanded', String(button.dataset.transferReview === review));
    });
    if (open) load({ replay: true });
    else {
      reads.cancel('transfers');
      showRefresh();
    }
  }

  function bind() {
    const panel = $('transferReviewPanel');
    if (!panel) return;
    events.listen('page', $('transferReviewActions'), 'click', event => {
      const choice = event.target.closest('[data-transfer-review]')?.dataset.transferReview;
      if (choice) selectReview(review === choice ? '' : choice);
    });
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
      } else if (button.hasAttribute('data-expand-manual')) {
        manualLimit = Infinity;
        renderManualPending();
      } else if (button.hasAttribute('data-find-manual') || button.hasAttribute('data-manual-offset')) {
        await findManualOptions(button);
      } else if (button.dataset.expandPairs) {
        if (button.dataset.expandPairs === 'pending') pendingLimit = Infinity;
        else confirmedLimit = Infinity;
        render();
      } else if ((button.dataset.confirmPair || button.dataset.confirmManual) && !submitting) {
        const pair = button.dataset.confirmManual
          ? manualOptions.get(button.closest('[data-manual-anchor]'))?.choices.get(Number(button.dataset.confirmManual))
          : candidates?.find(row => row.match_key === button.dataset.confirmPair);
        if (!pair) return;
        if ((pair.source_kind === 'trade' || pair.target_currency) && pair.ambiguity_status === 'ambiguous' &&
            !window.confirm('这笔流水有多个可能的配对。请核对两侧凭证，确认是同一笔资金后再继续。')) return;
        submitting = true;
        button.disabled = true;
        const status = button.parentElement.querySelector('[data-pair-status]');
        status.textContent = '正在确认配对…';
        message('transferPairNotice', '');
        try {
          await confirmTransferPair(pair);
          if (!current()) return;
          message('transferPairNotice', '配对已确认，正在更新划转和收支总览。');
          candidates = null;
          await Promise.all([load(), reloadWorkbench().catch(() => {
            if (current()) message('transferPairNotice', '配对已确认。收支总览刷新失败，请刷新页面查看。');
          })]);
        } catch (error) {
          const reason = typeof error.detail === 'string' ? error.detail : error.detail?.message;
          if (current()) status.textContent = `确认失败。 ${reason || error.message || '请重试。'}`;
        } finally {
          submitting = false;
          if (current()) button.disabled = false;
        }
      }
    });
  }

  return { bind, selectReview, renderPair, render };
}
