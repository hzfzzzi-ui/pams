import {configModelFromPayload, renderRuleGroups, rebalanceHtml, rebalanceDetailHtml, configErrorHtml} from './config_view.js?v=20261031-panels-v1';
import {loadPortfolioConfig} from './requests.js?v=20261031-panels-v1';
import {createReadSession} from './read_session.js?v=20261031-panels-v1';

const mounted = new WeakMap();

export function mount(root, context = {}) {
  if (!root?.ownerDocument || typeof context.get !== 'function' || typeof context.showDetails !== 'function') {
    throw new TypeError('Configuration panel requires a root, reader and detail presenter');
  }
  if (mounted.has(root)) return mounted.get(root);
  const win = root.ownerDocument.defaultView;
  let releaseDetails = () => {};
  const state = {
    ...configModelFromPayload(null),
    expandedRuleIds: new Set(context.state?.expandedRuleIds || []),
    expandedSubRuleIds: new Set(context.state?.expandedSubRuleIds || []),
  };
  const byId = id => root.querySelector(`[id="${id}"]`);
  const render = () => {
    const asOf = byId('portfolioConfigAsOf');
    const rules = byId('portfolioConfigRules');
    const queue = byId('portfolioRebalanceQueue');
    if (asOf) asOf.textContent = state.currentAsOf;
    if (rules) rules.innerHTML = renderRuleGroups(state.currentRules, state);
    if (queue) queue.innerHTML = rebalanceHtml(state.currentRebalance);
  };
  const session = createReadSession({
    win, tab: 'allocation', afterStartup: context.afterStartup || (task => task()),
    read: signal => loadPortfolioConfig(context.get, signal),
    apply(payload) { Object.assign(state, configModelFromPayload(payload)); render(); },
    onError(error) {
      const rules = byId('portfolioConfigRules');
      if (rules) rules.innerHTML = configErrorHtml(error);
    },
  });
  const controller = {
    ready: null,
    reload: session.reload,
    snapshot: () => ({expandedRuleIds: [...state.expandedRuleIds], expandedSubRuleIds: [...state.expandedSubRuleIds]}),
    dispose() {
      session.dispose();
      releaseDetails();
      if (mounted.get(root) === controller) mounted.delete(root);
    },
    get disposed() { return session.disposed; },
  };
  mounted.set(root, controller);
  root.addEventListener('click', event => {
    const subRule = event.target?.closest?.('[data-portfolio-subrule]');
    if (subRule && root.contains(subRule)) {
      const id = subRule.dataset.portfolioSubrule;
      if (state.expandedSubRuleIds.has(id)) state.expandedSubRuleIds.delete(id);
      else state.expandedSubRuleIds.add(id);
      render();
      return;
    }
    const rule = event.target?.closest?.('[data-portfolio-rule]');
    if (rule && root.contains(rule)) {
      const id = rule.dataset.portfolioRule;
      if (state.expandedRuleIds.has(id)) state.expandedRuleIds.delete(id);
      else state.expandedRuleIds.add(id);
      render();
      return;
    }
    const action = event.target?.closest?.('[data-rebalance-item]');
    if (action && root.contains(action)) {
      const item = state.currentRebalance.find(row => row.id === action.dataset.rebalanceItem);
      if (item) {
        releaseDetails();
        const body = context.showDetails(item.title, rebalanceDetailHtml(item));
        const anchor = body?.firstElementChild;
        releaseDetails = () => {
          if (anchor && body.isConnected && body.contains(anchor)) context.closeDetails?.();
          releaseDetails = () => {};
        };
      }
    }
  }, {signal: session.signal});
  win.addEventListener('pagehide', controller.dispose, {once: true, signal: session.signal});
  controller.ready = session.reload();
  return controller;
}
