import { createAssetOverview } from './overview.js';
import { createReadScope, isReadCancelled } from '../../shared/read_scope.js';
import { createAccountSyncLoader } from '../accounts/account_sync_controller.js';
import { accountSyncMetadata } from '../accounts/account_sync_view.js';
import { showAccountReview } from '../accounts/account_review_sheet.js';
import { mountAccountHistory } from '../accounts/account_history.js';
import { mountImportInbox } from '../accounts/import_inbox_widget.js';
import { mountNetworthDebtsPanel } from '../accounts/networth_debts_panel.js';
import { accountCenterHref } from '../../app/routes.js';

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
  const panel = find('moreNavGrowthExplanationPanel');
  const details = find('moreNavGrowthDetails');
  const accountCenter = find('account-center');
  const accountRetry = find('retryAccountSync');
  const location = context.location || win?.location;
  const query = new URLSearchParams(location?.search);
  const targetAccount = query.get('account');
  const targetInstitution = query.get('institution');
  let alive = true;
  let navGrowthLoaded = false;
  let navGrowthPending;
  let reviewPanel;
  let accountsReady = null;
  let renderedGroups = null;
  let inboxWidget = null;
  const overview = createAssetOverview(root, {
    win, isActive: () => alive, readAccounts: ensureAccounts,
    read: () => reads.get('monthly-overview', '/api/assets/monthly-overview'),
  });
  mountNetworthDebtsPanel(root, get);
  function ensureAccounts() {
    if (renderedGroups) return Promise.resolve(renderedGroups);
    accountsReady ??= loadAccountSync().then(() => {
      if (accountRetry && !accountRetry.hidden) accountsReady = null;
      return renderedGroups;
    });
    return accountsReady;
  }
  function openAccount(account, review = false, balanceRequested = false) {
    reviewPanel = openSheet(review ? account.label + ' · 待核查' : account.label);
    if (!reviewPanel) return;
    if (review) {
      showAccountReview(reviewPanel, account, (kind, offset) =>
        reads.get('account-review:' + account.account_code + ':' + kind,
          '/api/sync/accounts/' + encodeURIComponent(account.account_code) +
          '/source-anomalies?' + new URLSearchParams({kind, offset}), {cache: false}), {balanceRequested});
    }
    reviewPanel.prepend(accountSyncMetadata(reviewPanel.ownerDocument, account.summary));
    mountAccountHistory(reviewPanel, account.account_code, get);
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
    renderedGroups = groups;
    overview.renderCreditCards(groups);
    if (!targetAccount) return;
    const item = [...container.querySelectorAll('[data-account-code]')].find(el => el.dataset.accountCode === targetAccount);
    const account = groups.flatMap(group => group.rows).find(row => row.account_code === targetAccount);
    if (!item || !account) {
      find('accountSyncStatus').textContent = '未找到指定账户。';
      return;
    }
    item.scrollIntoView({block: 'center'});
    item.querySelector('h4 button')?.focus({preventScroll: true});
    if (query.get('review') === 'balance' && openSheet) openAccount(account, true, true);
  }
  const loadAccountSync = createAccountSyncLoader(root, { reads, isActive: () => alive, compact: true, targetAccount,
    openAccount: openSheet ? openAccount : undefined,
    openReview: openSheet ? account => openAccount(account, true) : undefined, targetInstitution, onRendered: locateAccount });

  function onAccountRetry() { accountsReady = null; void ensureAccounts(); }

  function openRequestedAccounts() {
    const hash = location?.hash;
    const toolsRequested = ['#moreAdvanced', '#moreNavGrowthDetails'].includes(hash);
    const id = toolsRequested || hash === '#account-center' || (!hash && (targetAccount || targetInstitution))
      ? 'account-center' : 'asset-overview';
    if (toolsRequested) find('moreAdvanced').open = true;
    if (hash === '#moreNavGrowthDetails') details.open = true;
    for (const section of [accountCenter, find('asset-overview')]) {
      if (section) section.hidden = section.id !== id;
    }
    for (const link of root.querySelectorAll('.assetTabs a')) {
      if (link.hash === '#' + id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    if (id === 'account-center') {
      if (!inboxWidget) inboxWidget = mountImportInbox(find('accountImportInbox'), { get, win });
      else inboxWidget.refresh();
      return ensureAccounts();
    }
    if (id === 'asset-overview') return overview.load();
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
    overview.dispose();
    inboxWidget?.dispose();
    reads.dispose();
    if (reviewPanel?.isConnected) closeSheet?.();
    details?.removeEventListener('toggle', onToggle);
    accountRetry?.removeEventListener('click', onAccountRetry);
    win?.removeEventListener('hashchange', openRequestedAccounts);
    win?.removeEventListener('pagehide', dispose);
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  const controller = {ready: null, dispose, get disposed() { return !alive; }};
  mounted.set(root, controller);
  bootstrap();
  const origin = find('moreOrigin');
  if (origin) origin.textContent = location?.origin || '';
  details?.addEventListener('toggle', onToggle);
  accountRetry?.addEventListener('click', onAccountRetry);
  win?.addEventListener('hashchange', openRequestedAccounts);
  win?.addEventListener('pagehide', dispose);
  controller.ready = openRequestedAccounts();
  if (details?.open) void loadNavGrowthExplanationOnce();

  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
