import {dashboardView, riskCategoriesHtml, riskErrorHtml} from './risk_view.js';
import {loadRiskDisclosure} from './requests.js';
import {createReadSession} from './read_session.js';

const mounted = new WeakMap();

export function mount(root, context = {}) {
  if (!root?.ownerDocument || typeof context.get !== 'function') throw new TypeError('Risk panel requires a root and reader');
  if (mounted.has(root)) return mounted.get(root);
  const win = root.ownerDocument.defaultView;
  const state = {
    payload: null,
    scope: context.state?.scope || 'all',
    currentCategories: [],
    expandedRiskCategoryId: context.state?.expandedRiskCategoryId ?? null,
  };
  const byId = id => root.querySelector(`[id="${id}"]`);
  const renderDashboard = () => {
    const dashboard = byId('portfolioRiskDashboard');
    if (dashboard && state.payload) dashboard.innerHTML = dashboardView(state.payload, state.scope);
  };
  const renderCategories = () => {
    const node = byId('portfolioRiskCategories');
    if (node) node.innerHTML = riskCategoriesHtml(state.currentCategories, state.expandedRiskCategoryId);
  };
  const session = createReadSession({
    win, tab: 'risk', afterStartup: context.afterStartup || (task => task()),
    read: signal => loadRiskDisclosure(context.get, signal, context.loadPanels),
    apply(payload) {
      state.payload = payload;
      state.currentCategories = payload?.categories || [];
      if (!state.currentCategories.some(row => row.id === state.expandedRiskCategoryId)) state.expandedRiskCategoryId = null;
      const asOf = byId('portfolioRiskAsOf');
      if (asOf) asOf.textContent = payload?.as_of || '--';
      renderDashboard();
      renderCategories();
    },
    onError(error) {
      const html = riskErrorHtml(error);
      const dashboard = byId('portfolioRiskDashboard');
      const categories = byId('portfolioRiskCategories');
      if (dashboard) dashboard.innerHTML = html.dashboard;
      if (categories) categories.innerHTML = html.categories;
    },
  });
  const controller = {
    ready: null,
    reload: session.reload,
    snapshot: () => ({expandedRiskCategoryId: state.expandedRiskCategoryId}),
    dispose() {
      session.dispose();
      if (mounted.get(root) === controller) mounted.delete(root);
    },
    get disposed() { return session.disposed; },
  };
  mounted.set(root, controller);
  const heading = root.querySelector('[data-risk-checklist-title]');
  if (heading) heading.textContent = '风险检查清单';
  root.addEventListener('click', event => {
    const scopeBtn = event.target?.closest?.('[data-risk-scope]');
    if (scopeBtn && root.contains(scopeBtn)) {
      const nextScope = scopeBtn.dataset.riskScope === 'ex_esop' ? 'ex_esop' : 'all';
      if (state.scope !== nextScope) {
        state.scope = nextScope;
        renderDashboard();
        win.dispatchEvent(new win.CustomEvent('pams:risk-scope-change', { detail: { scope: nextScope } }));
      }
      return;
    }
    const button = event.target?.closest?.('[data-risk-category]');
    if (!button || !root.contains(button)) return;
    const categoryId = button.dataset.riskCategory;
    if (!state.currentCategories.some(item => item.id === categoryId)) return;
    state.expandedRiskCategoryId = state.expandedRiskCategoryId === categoryId ? null : categoryId;
    renderCategories();
    if (state.expandedRiskCategoryId) {
      [...root.querySelectorAll('[data-risk-category]')]
        .find(node => node.dataset.riskCategory === state.expandedRiskCategoryId)
        ?.closest('.portfolioRiskChecklistCard')
        ?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
  }, {signal: session.signal});
  win.addEventListener('pagehide', controller.dispose, {once: true, signal: session.signal});
  controller.ready = session.reload();
  return controller;
}
