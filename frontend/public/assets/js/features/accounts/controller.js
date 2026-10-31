import { createView } from './view.js?v=20261031-accounts-v1';
import { createActions } from './actions.js?v=20261031-accounts-v1';

const mounted = new WeakMap();
const filterValues = {'缺数据': 'missing', '金额差异': 'diff', '日期过期': 'stale', '负余额': 'negative', '在途资金': 'transit'};

export function mount(root, context) {
  if (!root?.querySelector) throw new TypeError('Accounts page root is required');
  const current = mounted.get(root);
  if (current) return current;
  const {get, post, replaceAddress, openSheet, closeSheet, bootstrap = () => {}, win = root.ownerDocument?.defaultView} = context;
  const location = context.location || win?.location;
  const find = id => root.querySelector(`#${id}`);
  const restore = context.restoreState;
  const state = {config: null, accountOverview: null, collapsedAccountGroups: new Set(restore?.collapsedGroups || [])};
  if (restore) root.dataset.accountFilter = restore.accountFilter || '';
  const view = createView(root, state);
  const generations = new Map();
  const pendingActions = new Set();
  const disabledButtons = new Map();
  let alive = true;
  let ready = false;
  let releaseSheet = () => {};
  let activeTab = 'overview';
  let restoreDetails = Boolean(restore);

  function renderOverview(data) {
    view.renderAccountCenter(data);
    if (restoreDetails) {
      root.querySelectorAll('[data-account-detail]').forEach(details => {
        details.open = Boolean(restore.openDetails?.includes(details.dataset.accountDetail));
      });
      const truthNote = root.querySelector('.truthNote');
      if (truthNote) truthNote.open = Boolean(restore.truthNoteOpen);
      root.querySelectorAll('[data-account-filter]').forEach(button => {
        button.classList.toggle('active', Boolean(restore.accountFilter) && filterValues[button.dataset.accountFilter] === restore.accountFilter);
      });
      restoreDetails = false;
    }
  }

  function showError(error) {
    if (alive) view.showError(String(error?.message || error));
  }

  async function readInto(key, url, render) {
    if (!alive) return null;
    const generation = (generations.get(key) || 0) + 1;
    generations.set(key, generation);
    const data = await get(url);
    if (alive && generations.get(key) === generation) render(data);
    return data;
  }

  function acceptOverview(data) {
    if (!alive) return;
    generations.set('overview', (generations.get('overview') || 0) + 1);
    renderOverview(data);
  }

  const loadAccountOverview = () => readInto('overview', '/api/accounts/overview', renderOverview);
  const loadTransferMatches = () => readInto('matches', '/api/bank-reconcile/transfer-matches', view.renderTransferMatches);
  const loadUnmatchedRows = () => {
    const since = find('since')?.value || state.config?.defaults?.since || '2026-01-01';
    return readInto('unmatched', `/api/bank-reconcile/unmatched-rows?since=${encodeURIComponent(since)}&limit=500`, view.renderUnmatchedRows);
  };
  const loadImports = () => readInto('imports', '/api/bank-reconcile/imports', view.renderImports);
  const actions = createActions({post, isActive: () => alive, view, acceptOverview, loadUnmatchedRows, loadImports, loadAccountOverview});

  function runAction(key, button, action) {
    if (!alive || button.disabled || pendingActions.has(key)) return;
    pendingActions.add(key);
    disabledButtons.set(button, button.disabled);
    button.disabled = true;
    Promise.resolve().then(() => alive ? action() : undefined).catch(showError).finally(() => {
      pendingActions.delete(key);
      if (alive && disabledButtons.has(button)) button.disabled = disabledButtons.get(button);
      disabledButtons.delete(button);
    });
  }

  function review(button) {
    if (!state.accountOverview) return;
    const code = button.dataset.accountReview;
    runAction(`review:${code}`, button, () => actions.reviewAccount(code, button.dataset.reviewAction));
  }

  function showDetail(title, html) {
    releaseSheet();
    const body = openSheet(title, html);
    if (!body) return;
    const anchor = body.firstElementChild;
    const onClick = event => {
      const button = event.target?.closest?.('[data-account-review]');
      if (!button || !body.contains(button)) return;
      event.stopPropagation();
      review(button);
    };
    body.addEventListener('click', onClick);
    releaseSheet = () => {
      body.removeEventListener('click', onClick);
      if (body.isConnected && anchor && body.contains(anchor)) closeSheet?.();
      releaseSheet = () => {};
    };
  }

  function showTab(tab) {
    activeTab = tab;
    root.querySelectorAll('[data-account-tab]').forEach(button => button.classList.toggle('active', button.dataset.accountTab === tab));
    root.querySelectorAll('[data-account-panel]').forEach(panel => {panel.hidden = panel.dataset.accountPanel !== tab;});
    if (location.hash !== `#${tab}`) replaceAddress(`${location.pathname}${location.search}#${tab}`);
  }

  function onClick(event) {
    const target = event.target?.closest ? event.target : event.target?.parentElement;
    if (!alive || !target || !root.contains(target)) return;
    const tab = target.closest('[data-account-tab]');
    if (tab && root.contains(tab)) {showTab(tab.dataset.accountTab); return;}
    // The root also stores data-account-filter; only a filter button is an action.
    const filterChip = target.closest('button[data-account-filter]');
    if (filterChip && root.contains(filterChip)) {
      const label = filterChip.dataset.accountFilter;
      const next = root.dataset.accountFilter === filterValues[label] ? '' : filterValues[label];
      root.dataset.accountFilter = next || '';
      root.querySelectorAll('[data-account-filter]').forEach(el => el.classList.toggle('active', next && el === filterChip));
      view.renderMobileAccountList(state.accountOverview?.accounts || []);
      return;
    }
    const groupRow = target.closest('.accountGroupRow[data-group-key]');
    if (groupRow && root.contains(groupRow)) {
      const key = groupRow.dataset.groupKey;
      if (state.collapsedAccountGroups.has(key)) state.collapsedAccountGroups.delete(key);
      else state.collapsedAccountGroups.add(key);
      view.renderAccountOverviewRows(state.accountOverview?.accounts || []);
      view.renderAccountBalanceRows(state.accountOverview?.accounts || []);
      return;
    }
    const mobileRow = target.closest('[data-mobile-account-index]');
    if (mobileRow && root.contains(mobileRow)) {
      const account = view.accountMobileRows(state.accountOverview?.accounts || [])[Number(mobileRow.dataset.mobileAccountIndex)];
      if (account) showDetail(account.subaccount_label || account.display_name || '账户详情', view.accountDetailGrid(account));
      return;
    }
    const overviewRow = target.closest('#accountOverviewRows tr[data-account-issue]');
    if (overviewRow && root.contains(overviewRow) && !target.closest('details')) {
      const details = overviewRow.querySelector('.accountRowDetails');
      if (details) {
        if (win.matchMedia('(max-width: 760px)').matches) {
          const title = overviewRow.querySelector('.accountCell div')?.innerText || '账户详情';
          showDetail(title, details.querySelector('.accountDetailGrid')?.outerHTML || details.innerHTML);
        } else details.open = !details.open;
      }
      return;
    }
    const button = target.closest('[data-account-review]');
    if (button && root.contains(button)) {review(button); return;}
    const profile = target.closest('.profileItem[data-profile]');
    if (profile && root.contains(profile) && state.config) {view.selectProfile(profile.dataset.profile); return;}
    const match = target.closest('#btnMatchTransfers');
    if (match && root.contains(match) && ready) runAction('matches', match, actions.runTransferMatching);
  }

  async function load() {
    state.config = await get('/api/bank-reconcile/config');
    if (!alive) return;
    await loadAccountOverview();
    if (!alive) return;
    find('since').value = state.config?.defaults?.since || '2026-01-01';
    view.renderProfiles();
    if (restore && Object.hasOwn(state.config?.profiles || {}, restore.profileCode)) view.selectProfile(restore.profileCode);
    if (restore && typeof restore.since === 'string') find('since').value = restore.since;
    view.renderRelationships();
    await loadTransferMatches();
    if (!alive) return;
    await loadUnmatchedRows();
    if (!alive) return;
    await loadImports();
    if (alive) ready = true;
  }

  function dispose() {
    if (!alive) return;
    alive = false;
    root.removeEventListener('click', onClick);
    win?.removeEventListener('pagehide', dispose);
    releaseSheet();
    for (const [button, disabled] of disabledButtons) button.disabled = disabled;
    disabledButtons.clear();
    pendingActions.clear();
    if (mounted.get(root) === controller) mounted.delete(root);
  }

  function captureUiState() {
    return {
      tab: activeTab, since: find('since')?.value || '', profileCode: find('profileCode')?.value || '',
      accountFilter: root.dataset.accountFilter || '', collapsedGroups: [...state.collapsedAccountGroups],
      openDetails: [...root.querySelectorAll('[data-account-detail]')].filter(details => details.open).map(details => details.dataset.accountDetail),
      truthNoteOpen: Boolean(root.querySelector('.truthNote')?.open),
    };
  }

  const controller = {ready: null, dispose, captureUiState, get disposed() {return !alive;}};
  mounted.set(root, controller);
  bootstrap();
  root.addEventListener('click', onClick);
  win?.addEventListener('pagehide', dispose);
  const initial = restore?.tab || (location.hash ? location.hash.slice(1) : 'overview');
  showTab(['overview', 'balance', 'transfers', 'sources'].includes(initial) ? initial : 'overview');
  controller.ready = load().catch(showError);
  return controller;
}

export function dispose(root) {
  mounted.get(root)?.dispose();
}
