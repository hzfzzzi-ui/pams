import { createReadScope, isReadCancelled } from '../../shared/read_scope.js';
import { createAccountSyncLoader } from '../accounts/account_sync_controller.js';
import { showAccountReview } from '../accounts/account_review_sheet.js';
import { accountCenterHref } from '../../app/routes.js';

export function issueSummary(payload) {
  const summary = payload?.data_quality?.summary;
  if (!summary || !['ok', 'warning', 'error'].includes(summary.status) ||
      !Number.isInteger(summary.error_count) || !Number.isInteger(summary.warning_count) ||
      summary.error_count < 0 || summary.warning_count < 0) throw new Error('数据状态暂不可用');
  return summary.status === 'ok' ? null : `错误 ${summary.error_count} · 警告 ${summary.warning_count}`;
}


const mounted = new WeakMap();

// Each page instance owns its queries, listeners and pending response lifetime.
// Each GET is independently cancellable; late-response guards remain in place.
export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('More page root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, renderExplanation, openSheet, closeSheet, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const reads = createReadScope({get, signal: context.signal, AbortController: win?.AbortController});
  const find = id => root.querySelector(`#${id}`);
  const row = find('moreIssues');
  const label = find('moreIssueText');
  const panel = find('moreNavGrowthExplanationPanel');
  const details = find('moreNavGrowthDetails');
  const accountRetry = find('retryAccountSync');
  const query = new URLSearchParams((context.location || win?.location)?.search);
  const targetAccount = query.get('account');
  let alive = true;
  let navGrowthLoaded = false;
  let navGrowthPending;
  let reviewPanel;
  function openAccount(account, review = false) {
    reviewPanel = openSheet(review ? account.label + ' · 待核查' : account.label);
    if (!reviewPanel) return;
    if (review && account.summary?.review_reasons?.length) {
      showAccountReview(reviewPanel, account, (kind, offset) =>
        reads.get('account-review:' + account.account_code + ':' + kind,
          '/api/sync/accounts/' + encodeURIComponent(account.account_code) +
          '/source-anomalies?' + new URLSearchParams({kind, offset}), {cache: false}));
    }
    if (!account.active) return;
    const actions = reviewPanel.ownerDocument.createElement('div');
    actions.className = 'accountCenterActions';
    const add = (page, label, params = {}) => {
      const anchor = reviewPanel.ownerDocument.createElement('a');
      anchor.className = 'btn';
      anchor.textContent = label;
      anchor.href = page + '?' + new URLSearchParams({...params, account: account.account_code,
        return: accountCenterHref(account.account_code)});
      actions.append(anchor);
    };
    add('accounts.html', '余额核准', {task: 'balance'});
    if (account.can_import) add('account_import.html', '导入资料');
    reviewPanel.prepend(actions);
  }
  function locateAccount(container, groups) {
    if (!targetAccount) return;
    const item = [...container.querySelectorAll('[data-account-code]')].find(el => el.dataset.accountCode === targetAccount);
    const account = groups.flatMap(group => group.rows).find(row => row.account_code === targetAccount);
    if (!item || !account) {
      find('accountSyncStatus').textContent = '未找到指定账户。';
      return;
    }
    item.closest('details').open = true;
    item.scrollIntoView({block: 'center'});
    item.querySelector('h4 button')?.focus({preventScroll: true});
    if (query.get('review') === 'balance' && openSheet) openAccount(account, true);
  }
  const loadAccountSync = createAccountSyncLoader(root, { reads, isActive: () => alive, compact: true,
    openAccount: openSheet ? openAccount : undefined,
    openReview: openSheet ? account => openAccount(account, true) : undefined, onRendered: locateAccount });

  function onAccountRetry() { void loadAccountSync(); }

  async function loadIssues() {
    try {
      const payload = await reads.get('issues', '/api/portfolio/data-quality?baseCurrency=CNY', {cache: false});
      if (!alive) return;
      const text = issueSummary(payload);
      if (row) row.hidden = text === null;
      const title = row?.querySelector('strong');
      if (title) title.textContent = '待处理';
      if (label && text !== null) label.textContent = text;
    } catch (error) {
      if (!alive || isReadCancelled(error)) return;
      if (row) row.hidden = false;
      const title = row?.querySelector('strong');
      if (title) title.textContent = '数据状态暂不可用';
      if (label) label.textContent = '';
    }
  }

  function loadNavGrowthExplanationOnce() {
    if (!alive || !panel || navGrowthLoaded) return Promise.resolve();
    if (navGrowthPending) return navGrowthPending;
    panel.innerHTML = '<div class="muted">正在加载NAV增值解释...</div>';
    navGrowthPending = (async () => {
      try {
        const payload = await reads.get('explanation', '/api/portfolio/equity-bridge?start=2026-01-01&baseCurrency=CNY', {cache: false});
        if (!alive) return;
        renderExplanation(payload, panel);
        navGrowthLoaded = true;
      } catch (error) {
        if (!alive || isReadCancelled(error)) return;
        panel.innerHTML = '<div class="muted"></div>';
        panel.firstElementChild.textContent = `NAV增值解释加载失败：${error?.message || String(error)}`;
      } finally {
        navGrowthPending = null;
      }
    })();
    return navGrowthPending;
  }

  function onToggle() {
    if (details.open) void loadNavGrowthExplanationOnce();
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    reads.dispose();
    if (reviewPanel?.isConnected) closeSheet?.();
    details?.removeEventListener('toggle', onToggle);
    accountRetry?.removeEventListener('click', onAccountRetry);
    win?.removeEventListener('pagehide', dispose);
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {ready: null, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  bootstrap();
  const origin = find('moreOrigin');
  if (origin) origin.textContent = (context.location || win?.location)?.origin || '';
  details?.addEventListener('toggle', onToggle);
  accountRetry?.addEventListener('click', onAccountRetry);
  win?.addEventListener('pagehide', dispose);
  if (details?.open) void loadNavGrowthExplanationOnce();
  controller.ready = Promise.all([loadIssues(), loadAccountSync()]);
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
